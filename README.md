# Drone Joystick Control via FastAPI + MAVLink

A virtual joystick interface to control a drone in real-time using FastAPI WebSocket and MAVLink. Supports authority arbitration, audit logging via NATS JetStream, and emergency failsafes.

## Features

- Web-based virtual joystick (2 axes each for Roll/Pitch and Yaw/Throttle)
- FastAPI WebSocket communication (20 Hz)
- MAVLink flight controller adapter
- Authority arbitration for multiple operators
- Audit logs via NATS JetStream
- Emergency stop and manual takeover

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/Sami-Elsadiq/drone-joystick-control.git
cd drone-joystick-control
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate    # Linux / macOS
venv\Scripts\activate       # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Flight Controller Adapter

By default, `MAVLinkAdapter` is used (UDP: `127.0.0.1:14551`). For testing without hardware, use `MockAdapter`:

```bash
export FC_ADAPTER=MOCK   # Linux/macOS
set FC_ADAPTER=MOCK      # Windows CMD
```

### 5. Run the application

```bash
uvicorn main:app --reload
```

This starts the FastAPI server on `http://127.0.0.1:8000`.

### 6. Open joystick

Open `joystick.html` in a browser. Ensure it points to the correct WebSocket URL (`ws://127.0.0.1:8000/ws/control`).

### 7. NATS JetStream (optional)

- Install and run [NATS server](https://nats.io/download/nats-io/nats-server/):

```bash
nats-server -js
```

- This enables audit logging of commands and sessions.

## Demo

- Move the virtual joystick; commands will be sent to the drone or mock adapter.
- The first operator to move the joystick gains control.
- Switching to manual mode is automatic if the drone is in AUTO/MISSION mode.

## Notes

- Ensure UDP ports for MAVLink are open.
- Latency monitoring is logged in console (50 Hz control loop).
- Emergency stop triggers if session timeout occurs.

## License

MIT License

