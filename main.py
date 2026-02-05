from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pymavlink import mavutil
import json
import threading
import time
import uuid
import os
import logging
import asyncio
import nats

# =========================
# Logging / Audit
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =========================
# Normalized Command Model
# =========================
class ControlCommand(BaseModel):
    roll: float       # -1.0 to 1.0
    pitch: float      # -1.0 to 1.0
    yaw: float        # -1.0 to 1.0
    throttle: float   # 0.0 to 1.0
    timestamp: float = None
    sequence: int = 0
    operator_id: str = ""
    session_id: str = ""

# =========================
# Flight Controller Adapter Interface
# =========================
class FlightControllerAdapter:
    def send_control(self, cmd: ControlCommand):
        raise NotImplementedError
    def set_mode(self, mode: str):
        raise NotImplementedError
    def emergency_stop(self):
        raise NotImplementedError
    def heartbeat(self):
        raise NotImplementedError

# =========================
# MAVLink Adapter
# =========================
class MAVLinkAdapter(FlightControllerAdapter):
    def __init__(self, UDPort="udpin:127.0.0.1:14551"):
        self.master = mavutil.mavlink_connection(UDPort, source_system=255)
        print("Waiting for heartbeat...")
        self.master.wait_heartbeat()
        print("✅ Connected to MAVLink flight controller")

    def send_control(self, cmd: ControlCommand):
        self.master.mav.manual_control_send(
            self.master.target_system,
            int(cmd.pitch * 1000),
            int(cmd.roll * 1000),
            int(cmd.throttle * 1000),
            int(cmd.yaw * 1000),
            0
        )

    def set_mode(self, mode: str):
        if mode.lower() == "manual":
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                0,
                mavutil.mavlink.MAV_MODE_FLAG_MANUAL_INPUT_ENABLED,
                0, 0, 0, 0, 0, 0
            )

    def emergency_stop(self):
        logging.warning("🚨 Emergency Land Triggered")
        self.master.mav.command_long_send(
            self.master.target_system,
            self.master.target_component,
            mavutil.mavlink.MAV_CMD_NAV_LAND,
            0, 0, 0, 0, 0, 0, 0, 0
        )

    def heartbeat(self):
        msg = self.master.recv_match(blocking=False)
        return msg

# =========================
# Mock Adapter for Testing
# =========================
class MockAdapter(FlightControllerAdapter):
    def send_control(self, cmd: ControlCommand):
        logging.info(f"MOCK: Command sent: {cmd.model_dump()}")

    def set_mode(self, mode: str):
        logging.info(f"MOCK: Set mode to {mode}")

    def emergency_stop(self):
        logging.warning("MOCK: Emergency stop triggered")

    def heartbeat(self):
        return None

# =========================
# Adapter Selection
# =========================
ADAPTER_TYPE = os.getenv("FC_ADAPTER", "MAVLINK").upper()
if ADAPTER_TYPE == "MOCK":
    fc = MockAdapter()
else:
    fc = MAVLinkAdapter()

# =========================
# Globals
# =========================
JOYSTICK_THRESHOLD = 0.05
sessions = {}              # session_id -> session data
control_owner = None       # session_id
authority_lock = threading.Lock()
current_mode = None

# =========================
# JetStream Setup
# =========================
nc = None   # NATS client
js = None   # JetStream context
MAIN_LOOP = None  # store main event loop for threads

async def init_jetstream():
    global nc, js
    nc = await nats.connect("nats://127.0.0.1:4222")
    js = nc.jetstream()

    # Create audit log stream if it doesn't exist
    try:
        await js.add_stream(
            name="audit_logs",
            subjects=["audit.logs"],  # ensure this matches publisher & subscriber
            storage=nats.js.api.StorageType.FILE,
            retention=nats.js.api.RetentionPolicy.LIMITS
        )
        logging.info("✅ JetStream stream 'audit.logs' ready")
    except nats.errors.Error as e:
        if "stream already exists" in str(e).lower():
            logging.info("✅ JetStream stream 'audit.logs' already exists")
        else:
            logging.error(f"Failed to create stream: {e}")

def publish_audit_from_thread(event: dict):
    """Thread-safe publishing of audit events from non-async code."""
    if js and MAIN_LOOP:
        asyncio.run_coroutine_threadsafe(
            js.publish("audit.logs", json.dumps(event).encode()),
            MAIN_LOOP
        )
    else:
        logging.warning("MAIN_LOOP not ready, skipping audit publish")

async def publish_audit(event: dict):
    """Async publishing from FastAPI endpoints."""
    if js:
        try:
            await js.publish("audit.logs", json.dumps(event).encode())
        except Exception as e:
            logging.error(f"Failed to publish audit event: {e}")

# =========================
# MAVLink RX Thread
# =========================
def mavlink_rx_loop(master):
    global current_mode
    while True:
        try:
            msg = master.recv_match(blocking=True, timeout=1)
            if msg and msg.get_type() == "HEARTBEAT":
                current_mode = master.flightmode
                
        except Exception as e:
            logging.warning(f"MAVLink RX loop error: {e}")
            time.sleep(0.05)

if isinstance(fc, MAVLinkAdapter):
    threading.Thread(target=mavlink_rx_loop, args=(fc.master,), daemon=True).start()

# =========================
# Control Dispatcher
# =========================
def control_dispatcher():
    global control_owner
    while True:
        with authority_lock:
            if control_owner and control_owner in sessions:
                cmd = sessions[control_owner]["last_command"]
                if cmd:
                    send_time = time.time()  # mark the time command is sent
                    fc.send_control(cmd)

                    latency_ms = (send_time - cmd.timestamp) * 1000  # in milliseconds
                    logging.info(f"Latency: {latency_ms:.2f} ms | session: {control_owner} | seq: {cmd.sequence}")

                    # Thread-safe audit log
                    publish_audit_from_thread({
                        "event": "command_sent",
                        "session": control_owner,
                        "command": cmd.dict(),
                        "latency_ms": latency_ms
                    })
        # Failsafe: timeout check not really working right now
        now = time.time()
        to_remove = []
        with authority_lock:
            for sid, s in sessions.items():
                if now - s["last_input"] > 2.0:
                    logging.warning(f"🚨 Session {sid} timed out")
                    if control_owner == sid:
                        fc.emergency_stop()
                        control_owner = None
                    to_remove.append(sid)
            for sid in to_remove:
                sessions.pop(sid, None)
        time.sleep(0.02)  # 50 Hz

threading.Thread(target=control_dispatcher, daemon=True).start()

# =========================
# FastAPI App
# =========================
app = FastAPI()
with open("joystick.html") as f:
    html = f.read()

@app.on_event("startup")
async def startup_event():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()  # store main event loop for threads
    await init_jetstream()

@app.get("/")
async def get():
    return HTMLResponse(html)

# =========================
# WebSocket Endpoint
# =========================
@app.websocket("/ws/control")
async def websocket_endpoint(websocket: WebSocket):
    global control_owner
    await websocket.accept()
    session_id = str(uuid.uuid4())
    operator_id = f"op_{session_id[:8]}"
    sessions[session_id] = {
        "last_command": None,
        "last_input": time.time(),
        "has_control": False,
        "operator_id": operator_id,
        "sequence": 0
    }
    logging.info(f"🧩 Session connected: {session_id}")
    await publish_audit({"event": "session_connected", "session": session_id, "operator_id": operator_id})

    try:
        while True:
            data = await websocket.receive_text()
            js_data = json.loads(data)
            cmd = ControlCommand(
                roll=float(js_data.get("roll", 0)),
                pitch=float(js_data.get("pitch", 0)),
                yaw=float(js_data.get("yaw", 0)),
                throttle=float(js_data.get("throttle", 0)),
                timestamp=time.time(),
                sequence=sessions[session_id]["sequence"] + 1,
                operator_id=operator_id,
                session_id=session_id
            )
            sessions[session_id]["last_command"] = cmd
            sessions[session_id]["last_input"] = time.time()
            sessions[session_id]["sequence"] = cmd.sequence

            # Authority arbitration
            joystick_moved = (
                abs(cmd.roll) > JOYSTICK_THRESHOLD or
                abs(cmd.pitch) > JOYSTICK_THRESHOLD or
                abs(cmd.yaw) > JOYSTICK_THRESHOLD
            )
            if joystick_moved:
                with authority_lock:
                    if control_owner is None:
                        control_owner = session_id
                        sessions[session_id]["has_control"] = True
                        logging.info(f"CONTROL GRANTED → {session_id}")
                        await publish_audit({"event": "control_granted", "session": session_id})

            # Only owner can trigger manual mode
            if joystick_moved and current_mode in ("AUTO", "MISSION") and control_owner == session_id:
                fc.set_mode("manual")
                await publish_audit({"event": "manual_takeover", "session": session_id})

    except Exception as e:
        logging.error(f"❌ Session disconnected: {session_id} ({e})")
        await publish_audit({"event": "session_disconnected", "session": session_id})
        with authority_lock:
            if control_owner == session_id:
                fc.emergency_stop()
                control_owner = None
        sessions.pop(session_id, None)

# =========================
# HTTP Endpoints
# =========================
@app.post("/sessions/start")
async def start_session(operator_id: str):
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "last_command": None,
        "last_input": time.time(),
        "has_control": False,
        "operator_id": operator_id,
        "sequence": 0
    }
    logging.info(f"Session {session_id} started by operator {operator_id}")
    await publish_audit({"event": "session_started", "session": session_id, "operator_id": operator_id})
    return {"session_id": session_id}

@app.post("/sessions/{session_id}/input")
async def session_input(session_id: str, cmd: ControlCommand):
    if session_id not in sessions:
        return {"error": "invalid session"}
    sessions[session_id]["last_command"] = cmd
    sessions[session_id]["last_input"] = time.time()
    sessions[session_id]["sequence"] += 1
    logging.info(f"Command received for session {session_id}: {cmd.model_dump()}")
    await publish_audit({"event": "command", "session": session_id, "command": cmd.model_dump()})
    return {"status": "ok"}

@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    global control_owner
    if session_id in sessions:
        if control_owner == session_id:
            fc.emergency_stop()
            control_owner = None
        sessions.pop(session_id)
        logging.info(f"Session {session_id} stopped")
        await publish_audit({"event": "session_stopped", "session": session_id})
    return {"status": "stopped"}

@app.get("/health")
async def health():
    return {"status": "ok", "connected_sessions": len(sessions)}

@app.get("/metrics")
async def metrics():
    return {
        "active_sessions": len(sessions),
        "current_owner": control_owner,
        "commands_sent": sum(s.get("sequence", 0) for s in sessions.values())
    }
