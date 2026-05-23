"""
EVSE · SECC — ISO 15118-2 Charging Station Emulator
Run: python evse_main.py
Browser UI: http://localhost:5001
EV connects to: <your-ip>:15118
"""
import asyncio
import json
import os
import socket
import threading
import time
import uuid

from flask import Flask, render_template
from flask_socketio import SocketIO
import websockets

app = Flask(__name__, template_folder="templates")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Local IP ───────────────────────────────────────────────────────────────────
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

LOCAL_IP = get_local_ip()

# ── EVSE session state ─────────────────────────────────────────────────────────
class EVSEState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.session_id    = None
        self.energy_kwh    = 0.0
        self.power_kw      = 0.0
        self.current_a     = 0.0
        self.voltage_v     = 0.0
        self.last_meter_t  = None
        self.is_charging   = False

evse = EVSEState()

# ── Emit helpers ───────────────────────────────────────────────────────────────
def emit_msg(action, direction, layer, description, details=None):
    socketio.emit("v2g_msg", {
        "action": action, "direction": direction,
        "layer": layer, "description": description,
        "details": details or {},
    })

def emit_status(phase, state, note=""):
    socketio.emit("status", {"phase": phase, "state": state, "note": note})

# ── Mode lookup ────────────────────────────────────────────────────────────────
MODE_PARAMS = {
    "AC_L1":    {"power": 1.9,   "current": 16,  "voltage": 120,  "mode_str": "AC 1Φ 120V/16A"},
    "AC_L2_1P": {"power": 7.4,   "current": 32,  "voltage": 240,  "mode_str": "AC 1Φ 240V/32A"},
    "AC_L2_3P": {"power": 22.0,  "current": 32,  "voltage": 230,  "mode_str": "AC 3Φ 230V/32A"},
    "DC_50":    {"power": 50.0,  "current": 125, "voltage": 400,  "mode_str": "DC CCS 400V/125A"},
    "DC_150":   {"power": 160.0, "current": 200, "voltage": 800,  "mode_str": "DC DCFC 800V/200A"},
    "DC_350":   {"power": 350.0, "current": 350, "voltage": 1000, "mode_str": "DC HPC 1000V/350A"},
}

# ── Protocol response map ──────────────────────────────────────────────────────
def make_response(action, payload):
    L = "V2G Application · EXI/V2GTP"

    if action == "SessionSetupReq":
        evse.reset()
        evse.session_id = uuid.uuid4().hex[:16].upper()
        emit_status("app", "Session Active", f"SessionID {evse.session_id}")
        return {
            "action": "SessionSetupRes",
            "layer": L,
            "description": "EVSE assigns unique 8-byte SessionID used in all subsequent messages. EVSEID identifies this charge point globally.",
            "payload": {"ResponseCode": "OK_NewSessionEstablished", "EVSEID": "DE*CHR*E12345", "SessionID": evse.session_id},
        }

    if action == "ServiceDiscoveryReq":
        return {
            "action": "ServiceDiscoveryRes",
            "layer": L,
            "description": "EVSE advertises available charge services and payment options. ExternalPayment = RFID/App. Contract = Plug & Charge with ISO 15118 certificate.",
            "payload": {"ResponseCode": "OK", "PaymentOptions": ["ExternalPayment", "Contract"],
                        "ChargeService": {"ServiceID": 1, "EnergyTransferMode": ["AC_single_phase_core", "AC_three_phase_core", "DC_extended"]}},
        }

    if action == "PaymentServiceSelectionReq":
        mode = payload.get("SelectedPaymentOption", "ExternalPayment")
        return {
            "action": "PaymentServiceSelectionRes",
            "layer": L,
            "description": f"EVSE acknowledges {'Contract (Plug & Charge)' if mode == 'Contract' else 'ExternalPayment (RFID/App)'} billing mode.",
            "payload": {"ResponseCode": "OK"},
        }

    if action == "PaymentDetailsReq":
        emaid = payload.get("eMAID", "DE-BMW-A12345678901-X")
        return {
            "action": "PaymentDetailsRes",
            "layer": L,
            "description": "EVSE acknowledges contract cert chain and issues GenChallenge nonce. EV must sign this nonce in AuthorizationReq to prove private key possession.",
            "payload": {"ResponseCode": "OK", "GenChallenge": "6F3A1B9C2D8E4F7A5C0B3D6E1F2A4B8C", "eMAID": emaid, "EVSETimeStamp": "2026-05-19T00:00:00Z"},
        }

    if action == "AuthorizationReq":
        sig = payload.get("ContractSignature")
        desc = ("EVSE verifies ECDSA signature against contract certificate. Chain validated to V2G Root CA. EMAID not revoked. Authorization granted automatically — no driver action."
                if sig else
                "EVSE forwards nonce + session token to CSMS backend. RFID/app token verified. Authorization granted.")
        return {
            "action": "AuthorizationRes",
            "layer": L,
            "description": desc,
            "payload": {"ResponseCode": "OK", "EVSEProcessing": "Finished"},
        }

    if action == "ChargeParameterDiscoveryReq":
        mode = payload.get("RequestedEnergyTransferMode", "AC_L2_1P")
        p = MODE_PARAMS.get(mode, MODE_PARAMS["AC_L2_1P"])
        evse.power_kw  = p["power"]
        evse.current_a = p["current"]
        evse.voltage_v = p["voltage"]
        is_dc = mode.startswith("DC")
        if is_dc:
            charge_p = {"EVSEMaximumCurrentLimit": f"{p['current']} A", "EVSEMaximumPowerLimit": f"{p['power']} kW",
                        "EVSEMaximumVoltageLimit": f"{p['voltage']} V", "EVSEMinimumCurrentLimit": "1 A", "EVSEMinimumVoltageLimit": "200 V"}
        else:
            charge_p = {"EVSENominalVoltage": f"{p['voltage']} V", "EVSEMaxCurrent": f"{p['current']} A",
                        "EVSEMinCurrent": "6 A", "SAScheduleList": "1 schedule — full power 2h"}
        return {
            "action": "ChargeParameterDiscoveryRes",
            "layer": L,
            "description": f"EVSE reports {p['mode_str']} capability and provides SASchedule for smart charging.",
            "payload": {"ResponseCode": "OK", **charge_p},
        }

    if action == "CableCheckReq":
        return {
            "action": "CableCheckRes",
            "layer": L,
            "description": "Cable insulation resistance test complete. No ground fault detected. Safe to proceed to pre-charge.",
            "payload": {"ResponseCode": "OK", "DC_EVSEStatus": {"EVSENotification": "None", "EVSEIsolationStatus": "Valid"}},
        }

    if action == "PreChargeReq":
        target_v = payload.get("EVTargetVoltage", f"{evse.voltage_v} V")
        return {
            "action": "PreChargeRes",
            "layer": L,
            "description": "EVSE ramps output voltage to match EV battery voltage before closing main contactor. Prevents inrush current spike.",
            "payload": {"ResponseCode": "OK", "EVSEPresentVoltage": target_v, "DC_EVSEStatus": {"EVSENotification": "None"}},
        }

    if action == "PowerDeliveryReq":
        progress = payload.get("ChargeProgress", "Start")
        if progress == "Start":
            evse.is_charging   = True
            evse.energy_kwh    = 0.0
            evse.last_meter_t  = time.time()
            emit_status("charging", f"Charging — {evse.power_kw:.0f} kW", "Contactor closed — power flowing")
            socketio.emit("charging_start", {"power_kw": evse.power_kw, "current_a": evse.current_a, "voltage_v": evse.voltage_v})
        else:
            evse.is_charging  = False
            evse.last_meter_t = None
            emit_status("stopping", "Contactor Opening", "Power ramp-down initiated")
            socketio.emit("charging_stop", {})
        return {
            "action": "PowerDeliveryRes",
            "layer": L,
            "description": f"EVSE {'closes' if progress == 'Start' else 'opens'} main contactor. CP pilot transitions State {'B→C' if progress == 'Start' else 'C→B'}.",
            "payload": {"ResponseCode": "OK", "AC_EVSEStatus": {"RCD": False, "NotificationMaxDelay": 0, "EVSENotification": "None"}},
        }

    if action in ("ChargingStatusReq", "CurrentDemandReq"):
        now = time.time()
        if evse.last_meter_t is not None:
            dt = now - evse.last_meter_t
            evse.energy_kwh += evse.power_kw * dt / 3600.0
            evse.last_meter_t = now

        # DC: taper current based on EV reported SoC
        if action == "CurrentDemandReq":
            soc_str = payload.get("DC_EVStatus", {}).get("EVRESSOC", "0%").replace("%", "")
            try:
                soc = float(soc_str)
            except ValueError:
                soc = 0.0
            p = MODE_PARAMS.get("DC_50", MODE_PARAMS["DC_50"])  # will be overridden by actual mode
            max_a = evse.current_a
            if soc >= 80.0:
                taper = 1.0 - (soc - 80.0) / 20.0
                act_a = round(max(max_a * taper, max_a * 0.05), 1)
                act_kw = round(evse.voltage_v * act_a / 1000.0, 2)
            else:
                act_a  = evse.current_a
                act_kw = evse.power_kw
        else:
            act_a  = evse.current_a
            act_kw = evse.power_kw

        socketio.emit("meter_update", {
            "power_kw":   round(act_kw, 2),
            "current_a":  round(act_a, 1),
            "voltage_v":  round(evse.voltage_v, 1),
            "energy_kwh": round(evse.energy_kwh, 3),
        })

        if action == "ChargingStatusReq":
            return {
                "action": "ChargingStatusRes",
                "layer": L,
                "description": "EVSE reports meter reading, max current offer, and status. EV uses this to confirm session is still active.",
                "payload": {"ResponseCode": "OK", "EVSEID": "DE*CHR*E12345",
                            "EVSEMaxCurrent": f"{act_a} A",
                            "MeterInfo": {"MeterID": "AC-001", "MeterReading": f"{round(evse.energy_kwh * 1000)} Wh"},
                            "energy_kwh": round(evse.energy_kwh, 3), "power_kw": round(act_kw, 2),
                            "current_a": round(act_a, 1), "voltage_v": round(evse.voltage_v, 1)},
            }
        else:
            return {
                "action": "CurrentDemandRes",
                "layer": L,
                "description": "EVSE reports present voltage and current. EV uses these to verify targets are being met and compute SoC.",
                "payload": {"ResponseCode": "OK",
                            "EVSEPresentVoltage": f"{evse.voltage_v:.0f} V",
                            "EVSEPresentCurrent": f"{act_a:.1f} A",
                            "EVSECurrentLimitAchieved": False, "EVSEPowerLimitAchieved": False,
                            "MeterInfo": {"MeterID": "DC-001", "MeterReading": f"{round(evse.energy_kwh * 1000)} Wh"},
                            "energy_kwh": round(evse.energy_kwh, 3), "power_kw": round(act_kw, 2),
                            "current_a": round(act_a, 1), "voltage_v": round(evse.voltage_v, 1)},
            }

    if action in ("WeldingDetectionReq", "SessionStopReq"):
        if action == "SessionStopReq":
            evse.reset()
            emit_status("idle", "Waiting for EV", f"Ready — listening on {LOCAL_IP}:15118")
            socketio.emit("session_ended", {})
        return {
            "action": action.replace("Req", "Res"),
            "layer": L,
            "description": ("DC welding detection: EVSE checks contactor is fully open with no arcing before releasing cable lock."
                            if action == "WeldingDetectionReq" else
                            "EVSE acknowledges session end. Session state cleared. Ready for next EV."),
            "payload": {"ResponseCode": "OK"},
        }

    return {"action": action.replace("Req", "Res"), "layer": L, "description": "", "payload": {"ResponseCode": "OK"}}


# ── WebSocket protocol server ──────────────────────────────────────────────────
async def evse_handler(websocket):
    remote = websocket.remote_address[0] if hasattr(websocket, "remote_address") else "EV"
    emit_status("connecting", "EV Connected", f"ISO 15118 WebSocket from {remote}")
    try:
        async for raw in websocket:
            data    = json.loads(raw)
            action  = data.get("action", "")
            payload = data.get("payload", {})
            layer   = data.get("layer", "V2G Application · EXI/V2GTP")
            desc    = data.get("description", "")

            emit_msg(action, "ev→evse", layer, desc, payload)
            await asyncio.sleep(0.25)

            response = make_response(action, payload)
            await websocket.send(json.dumps(response))
            emit_msg(response["action"], "evse→ev",
                     response.get("layer", layer),
                     response.get("description", ""),
                     response.get("payload", {}))
    except websockets.exceptions.ConnectionClosed:
        emit_status("idle", "EV Disconnected", f"Waiting for next connection on {LOCAL_IP}:15118")
    except Exception as e:
        emit_status("idle", "Error", str(e))

def run_ws_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def serve():
        async with websockets.serve(evse_handler, "0.0.0.0", 15118):
            await asyncio.Future()

    loop.run_until_complete(serve())

threading.Thread(target=run_ws_server, daemon=True).start()

# ── Flask ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("evse.html", local_ip=LOCAL_IP)

@socketio.on("connect")
def on_connect():
    emit_status("idle", "Waiting for EV", f"Listening on {LOCAL_IP}:15118")
    socketio.emit("local_ip", {"ip": LOCAL_IP})

if __name__ == "__main__":
    raw_port = os.environ.get("PORT", "5001")
    port = int(raw_port.split(":")[0])
    print(f"\n  EVSE Server")
    print(f"  Browser UI  →  http://localhost:{port}")
    print(f"  Tell EV operator: IP = {LOCAL_IP}, port 15118\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
