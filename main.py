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
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logging.getLogger("nats").setLevel(logging.CRITICAL)

# =========================
# Normalized Command Model
# =========================
class ControlCommand(BaseModel):
    roll: float
    pitch: float
    yaw: float
    throttle: float
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
        logging.info("Waiting for MAVLink heartbeat...")
        self.master.wait_heartbeat()
        logging.info("✅ Connected to MAVLink flight controller")

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
        logging.warning("🚨 Emergency LAND triggered")
        self.master.mav.command_long_send(
            self.master.target_system,
            self.master.target_component,
            mavutil.mavlink.MAV_CMD_NAV_LAND,
            0, 0, 0, 0, 0, 0, 0, 0
        )

    def heartbeat(self):
        return self.master.recv_match(blocking=False)

# =========================
# Mock Adapter
# =========================
class MockAdapter(FlightControllerAdapter):
    def send_control(self, cmd: ControlCommand):
        logging.info(f"MOCK SEND: {cmd.model_dump()}")
    def set_mode(self, mode: str):
        logging.info(f"MOCK MODE: {mode}")
    def emergency_stop(self):
        logging.warning("🚨 MOCK EMERGENCY LAND")
    def heartbeat(self):
        return None

# =========================
# Adapter Selection
# =========================
ADAPTER_TYPE = os.getenv("FC_ADAPTER", "MAVLINK").upper()
fc = MockAdapter() if ADAPTER_TYPE == "MOCK" else MAVLinkAdapter()

# =========================
# Globals
# =========================
JOYSTICK_THRESHOLD = 0.05
sessions = {}
control_owner = None
authority_lock = threading.Lock()
current_mode = None

# =========================
# NATS / JetStream Globals
# =========================
nc = None
js = None
MAIN_LOOP = None
nats_warned = False

# =========================
# JetStream Background Task
# =========================
async def jetstream_background_task():
    """
    - Emits warning immediately if NATS is down at startup
    - Retries silently
    - Emits reconnect message once
    """
    global nc, js, nats_warned

    # INITIAL STATE CHECK 
    if not nats_warned:
        logging.warning("⚠️ NATS unavailable — audit logging paused")
        nats_warned = True

    async def disconnected_cb():
        global nats_warned
        if not nats_warned:
            logging.warning("⚠️ NATS unavailable — audit logging paused")
            nats_warned = True

    async def reconnected_cb():
        global nats_warned
        logging.info("✅ NATS reconnected — audit logging resumed")
        nats_warned = False

    async def error_cb(e):
        pass  

    while True:
        try:
            nc = await nats.connect(
                "nats://127.0.0.1:4222",
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
                disconnected_cb=disconnected_cb,
                reconnected_cb=reconnected_cb,
                error_cb=error_cb,
            )

            js = nc.jetstream()

            try:
                await js.add_stream(
                    name="audit_logs",
                    subjects=["audit.logs"],
                    storage=nats.js.api.StorageType.FILE,
                )
                logging.info("✅ JetStream ready")
            except Exception:
                logging.info("ℹ️ JetStream stream already exists")

            nats_warned = False
            return

        except Exception:
            await asyncio.sleep(2)

# =========================
# Audit Publishing
# =========================
async def publish_audit(event: dict):
    if not js:
        return
    try:
        await js.publish("audit.logs", json.dumps(event).encode())
    except Exception:
        pass

def publish_audit_from_thread(event: dict):
    if js and MAIN_LOOP:
        asyncio.run_coroutine_threadsafe(
            js.publish("audit.logs", json.dumps(event).encode()),
            MAIN_LOOP
        )

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
        except Exception:
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
                    fc.send_control(cmd)

        # timeout failsafe , TODO: find a way to measure difference for inputs to create working logic
        now = time.time()
        expired = []
        with authority_lock:
            for sid, s in sessions.items():
                if now - s["last_input"] > 10.0:
                    logging.warning(f"🚨 Session timeout: {sid}")
                    if control_owner == sid:
                        fc.emergency_stop()
                        control_owner = None
                    expired.append(sid)
            for sid in expired:
                sessions.pop(sid, None)

        time.sleep(0.02)

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
    MAIN_LOOP = asyncio.get_running_loop()
    asyncio.create_task(jetstream_background_task())

@app.get("/")
async def root():
    return HTMLResponse(html)

# =========================
# WebSocket Control
# =========================
@app.websocket("/ws/control")
async def websocket_control(ws: WebSocket):
    global control_owner
    await ws.accept()


    client_info = ws.client  # this is a tuple (host, port) or None
    if client_info:
        client_host, client_port = client_info
        addr_str = f"{client_host}:{client_port}"
    else:
        addr_str = "unknown"

    session_id = str(uuid.uuid4())
    operator_id = f"op_{session_id[:8]}"
    logging.info(f"🟢 Session connected: {session_id} from {client_host}:{client_port}")

    sessions[session_id] = {
        "last_command": None,
        "last_input": time.time(),
        "has_control": False,
        "sequence": 0,
        "operator_id": operator_id
    }

    await publish_audit({"event": "session_connected", "session": session_id})

    try:
        while True:
            data = json.loads(await ws.receive_text())

            cmd = ControlCommand(
                roll=float(data.get("roll", 0)),
                pitch=float(data.get("pitch", 0)),
                yaw=float(data.get("yaw", 0)),
                throttle=float(data.get("throttle", 0)),
                timestamp=time.time(),
                sequence=sessions[session_id]["sequence"] + 1,
                operator_id=operator_id,
                session_id=session_id
            )

            sessions[session_id]["last_command"] = cmd
            sessions[session_id]["last_input"] = time.time()
            sessions[session_id]["sequence"] = cmd.sequence

            joystick_moved = any(
                abs(v) > JOYSTICK_THRESHOLD
                for v in [cmd.roll, cmd.pitch, cmd.yaw]
            )

            if joystick_moved:
                with authority_lock:
                    if control_owner is None:
                        control_owner = session_id
                        sessions[session_id]["has_control"] = True
                        logging.info(f"Operator {operator_id} now has control")
                        await publish_audit({"event": "control_granted", "session": session_id})

            if joystick_moved and current_mode in ("AUTO", "MISSION") and control_owner == session_id:
                fc.set_mode("manual")
                logging.info(f"✋ Operator {operator_id} performed manual takeover")
                await publish_audit({"event": "manual_takeover", "session": session_id})

    except Exception:
        await publish_audit({"event": "session_disconnected", "session": session_id})
        with authority_lock:
            if control_owner == session_id:
                fc.emergency_stop()
                control_owner = None
        sessions.pop(session_id, None)

# =========================
# Health & Metrics
# =========================
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(sessions),
        "nats_connected": js is not None
    }

@app.get("/metrics")
async def metrics():
    return {
        "active_sessions": len(sessions),
        "current_owner": control_owner
    }
