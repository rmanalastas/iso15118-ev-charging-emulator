"""
EV · EVCC — ISO 15118-2 Vehicle Emulator
Run: python ev_main.py
Browser UI: http://localhost:5000
Enter the EVSE IP address in the browser to connect.
"""
import asyncio
import json
import os
import threading

from flask import Flask, render_template
from flask_socketio import SocketIO
import websockets

app = Flask(__name__, template_folder="templates")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Session state ──────────────────────────────────────────────────────────────
class EVSession:
    def __init__(self):
        self.stop_event = threading.Event()
        self.running    = False

_session = EVSession()

class SessionAborted(Exception):
    pass

# ── Emit helpers ───────────────────────────────────────────────────────────────
def emit_msg(action, direction, layer, description, details=None):
    socketio.emit("v2g_msg", {
        "action": action, "direction": direction,
        "layer": layer, "description": description,
        "details": details or {},
    })

def emit_status(phase, state, note=""):
    socketio.emit("status", {"phase": phase, "state": state, "note": note})

# ── Mode parameters ────────────────────────────────────────────────────────────
MODE_PARAMS = {
    "AC_L1":    {"power": 1.9,   "current": 16,  "voltage": 120,  "cap": 30.0,  "dc": False},
    "AC_L2_1P": {"power": 7.4,   "current": 32,  "voltage": 240,  "cap": 60.0,  "dc": False},
    "AC_L2_3P": {"power": 22.0,  "current": 32,  "voltage": 230,  "cap": 60.0,  "dc": False},
    "DC_50":    {"power": 50.0,  "current": 125, "voltage": 400,  "cap": 100.0, "dc": True},
    "DC_150":   {"power": 160.0, "current": 200, "voltage": 800,  "cap": 100.0, "dc": True},
    "DC_350":   {"power": 350.0, "current": 350, "voltage": 1000, "cap": 100.0, "dc": True},
}

# ── EV session ─────────────────────────────────────────────────────────────────
async def ev_session(evse_ip, auth_mode, initial_soc, target_soc, ac_mode):
    soc       = float(initial_soc)
    energy    = 0.0
    p         = MODE_PARAMS.get(ac_mode, MODE_PARAMS["AC_L2_1P"])
    is_dc     = p["dc"]
    power_kw  = p["power"]
    current_a = p["current"]
    voltage_v = p["voltage"]
    cap_kwh   = p["cap"]
    L         = "V2G Application · EXI/V2GTP"

    def check_stop():
        if _session.stop_event.is_set():
            raise SessionAborted()

    # ── Phase 1: SLAC (local animation) ───────────────────────────────────────
    emit_status("slac", "SLAC — PLC Matching", "HomePlug GreenPHY attenuation matching...")
    emit_msg("CM_SLAC_PARAM.REQ", "ev→evse", "HomePlug GreenPHY · PLC (powerline)",
             "EV starts SLAC to identify which EVSE it is physically plugged into via CP pilot attenuation fingerprinting.",
             {"RunID": "A3F2C1D4E5B60789", "ApplicationType": "0x00 (EV-EVSE Matching)"})
    await asyncio.sleep(0.8)
    check_stop()

    emit_msg("CM_SLAC_PARAM.CNF", "evse→ev", "HomePlug GreenPHY · PLC (powerline)",
             "EVSE acknowledges SLAC. Will respond to M-SOUND probes for attenuation measurement.",
             {"RunID": "A3F2C1D4E5B60789", "M-SOUND_TARGET": 10, "FORWARDING_STA": "EVSE-PLC-MAC"})
    await asyncio.sleep(0.5)
    check_stop()

    emit_msg("CM_ATTEN_CHAR.IND", "evse→ev", "HomePlug GreenPHY · PLC (powerline)",
             "EVSE reports measured attenuation profile across all 58 frequency carriers. Low attenuation confirms this EV is physically connected to this EVSE.",
             {"NumSounds": 10, "ATTEN_PROFILE": "low (< 10 dB — physically connected)", "EV_MAC": "02:4A:3F:11:22:AB"})
    await asyncio.sleep(0.5)
    check_stop()

    emit_msg("CM_SLAC_MATCH.REQ", "ev→evse", "HomePlug GreenPHY · PLC (powerline)",
             "EV confirms match and requests HomePlug network membership. EVCCID and EVSEID are exchanged for binding.",
             {"EVCCID": "02:4A:3F:11:22:AB", "EVSEID": "02:1B:5C:33:44:CD", "RunID": "A3F2C1D4E5B60789"})
    await asyncio.sleep(0.5)
    check_stop()

    emit_msg("CM_SLAC_MATCH.CNF", "evse→ev", "HomePlug GreenPHY · PLC (powerline)",
             "EVSE provides HomePlug network credentials. EV joins the private PLC network — secure powerline link established for V2G traffic.",
             {"NID": "B7E3A9D210F45C3 (7 bytes)", "NMK": "<16-byte AES key>", "Result": "0x01 Match"})
    await asyncio.sleep(0.4)

    # ── Phase 2: IPv6 + SDP ───────────────────────────────────────────────────
    emit_status("slac", "IPv6 Link-Local", "SLAAC address autoconfiguration...")
    emit_msg("IPv6 Link-Local Setup", "ev→evse", "IPv6 / SLAAC (RFC 4862)",
             "Both nodes auto-configure link-local IPv6 addresses via EUI-64 SLAAC. Neighbor Solicitation (DAD) confirms addresses are unique on the PLC link.",
             {"EV_LL_Addr": "fe80::4A:3FFF:FE11:22AB/64", "EVSE_LL_Addr": "fe80::1:2:3:4/64", "DAD": "OK"})
    await asyncio.sleep(0.5)
    check_stop()

    emit_status("hlc", "SDP — SECC Discovery", f"Broadcasting UDP to ff02::1:15118...")
    emit_msg("SDP Request", "ev→evse", "UDP/IPv6 · ff02::1, port 15118",
             "SECC Discovery Protocol: EV broadcasts UDP multicast to discover the EVSE on the local PLC link. Discovers IP and port without prior configuration.",
             {"Protocol": "UDP multicast", "Destination": "ff02::1:15118", "SecurityType": "TLS"})
    await asyncio.sleep(0.5)
    check_stop()

    emit_msg("SDP Response", "evse→ev", "UDP/IPv6 · unicast",
             f"EVSE replies with its IPv6 address and V2GTP TCP port. EV now knows exactly where to open the V2G session.",
             {"SECC_IPv6": evse_ip, "V2GTP_Port": 15118, "Security": "TLS_1_2_required"})
    await asyncio.sleep(0.4)
    check_stop()

    # ── Phase 3: TCP + TLS ────────────────────────────────────────────────────
    emit_status("hlc", "Connecting", f"TCP → {evse_ip}:15118...")
    emit_msg("TCP Connection", "ev→evse", "TCP · 3-way handshake",
             "SYN → SYN-ACK → ACK establishes reliable ordered connection. All V2GTP messages travel over this connection.",
             {"SYN": "EV→EVSE", "SYN-ACK": "EVSE→EV", "ACK": "EV→EVSE", "Result": "Connected"})

    try:
        async with websockets.connect(f"ws://{evse_ip}:15118", ping_interval=None, open_timeout=10) as ws:
            check_stop()

            tls_desc = ("PnC Mutual TLS: EV presents OEM Provisioning Certificate. EVSE presents SECC leaf cert. "
                        "Both sides authenticated before any application messages." if auth_mode == "PnC" else
                        "TLS 1.2: EVSE presents SECC leaf certificate (chain → V2G Root CA). EV validates chain. Session keys via ECDHE-ECDSA.")
            emit_msg("TLS 1.2 Handshake", "ev→evse", "TLS · certificate exchange", tls_desc,
                     {"Cipher": "ECDHE-ECDSA-AES128-GCM-SHA256",
                      "SECC_Cert": "CPO-SECC-Leaf → CPO-Sub-CA → V2G Root CA",
                      "MutualTLS": "Yes (OEM Prov. Cert)" if auth_mode == "PnC" else "No"})
            await asyncio.sleep(0.4)
            check_stop()

            emit_status("app", "Application Layer", "ISO 15118-2 V2GTP — exchanging messages")
            socketio.emit("connected", {})

            # ── exchange helper ────────────────────────────────────────────────
            async def exchange(action, payload, desc):
                check_stop()
                emit_msg(action, "ev→evse", L, desc, payload)
                await ws.send(json.dumps({"action": action, "payload": payload, "layer": L, "description": desc}))
                raw  = await asyncio.wait_for(ws.recv(), timeout=30.0)
                data = json.loads(raw)
                emit_msg(data["action"], "evse→ev",
                         data.get("layer", L), data.get("description", ""), data.get("payload", {}))
                return data

            # ── SessionSetup ───────────────────────────────────────────────────
            resp = await exchange("SessionSetupReq",
                {"EVCCID": "DE-BMW-A123456789", "SessionID": "00000000"},
                "First ISO 15118-2 application message. EVCCID from EV HomePlug PLC MAC. SessionID=00000000 requests a new session.")
            session_id = resp.get("payload", {}).get("SessionID", "N/A")
            socketio.emit("session_id", {"session_id": session_id})
            await asyncio.sleep(0.4)

            # ── ServiceDiscovery ───────────────────────────────────────────────
            await exchange("ServiceDiscoveryReq",
                {"ServiceCategory": "EVCharging"},
                "EV queries EVSE for available services and payment options (ExternalPayment and/or Contract).")
            await asyncio.sleep(0.4)

            # ── PaymentServiceSelection ────────────────────────────────────────
            popt = "Contract" if auth_mode == "PnC" else "ExternalPayment"
            await exchange("PaymentServiceSelectionReq",
                {"SelectedPaymentOption": popt, "SelectedServiceList": ["EVCharging"]},
                f"EV selects {'Contract (automated Plug & Charge billing)' if popt == 'Contract' else 'ExternalPayment (RFID/App billing)'}. EVSE must have advertised this option.")
            await asyncio.sleep(0.4)

            # ── PnC: PaymentDetails + signed Authorization ────────────────────
            if auth_mode == "PnC":
                resp = await exchange("PaymentDetailsReq",
                    {"eMAID": "DE-BMW-A12345678901-X",
                     "ContractSignatureCertChain": {
                         "Certificate": "MO-Leaf-Cert (ECDSA P-256 · Subject: eMAID=DE-BMW-A12345678901-X)",
                         "SubCertificates": ["MO-Sub-CA-2", "MO-Sub-CA-1"]},
                     "DHpublicKey": "03:A1:B2:C3:D4:... (33 bytes, ECDH P-256)"},
                    "PnC: EV presents EMAID and MO certificate chain. EVSE validates chain to V2G Root CA and checks EMAID against CRL.")
                gen_challenge = resp.get("payload", {}).get("GenChallenge", "6F3A1B9C2D8E4F7A...")
                await asyncio.sleep(0.5)
                await exchange("AuthorizationReq",
                    {"Id": "ID1", "GenChallenge": gen_challenge,
                     "ContractSignature": "3045022100A3F2B1C9...E4D5 (ECDSA-P256-SHA256 · simulated)"},
                    "PnC: EV signs GenChallenge with contract cert private key (secure element). Proves live possession — prevents replay attacks. Zero driver interaction.")
            else:
                await exchange("AuthorizationReq",
                    {"GenChallenge": "A3F2C1D4E5B607890A1B2C3D4E5F6071 (64-byte random nonce)"},
                    "EIM: EV sends random nonce. EVSE forwards to CSMS backend to verify RFID token/app authorization.")
            await asyncio.sleep(0.4)

            # ── ChargeParameterDiscovery ───────────────────────────────────────
            await exchange("ChargeParameterDiscoveryReq",
                {"RequestedEnergyTransferMode": ac_mode,
                 "EVChargeParameter": {"DepartureTime": "PT2H", "EAmount": "40 kWh",
                                       "EVMaxVoltage": f"{voltage_v} V", "EVMaxCurrent": f"{current_a} A"}},
                "EV declares charging capabilities. EVSE uses these to generate SASchedule (smart charging plan).")
            await asyncio.sleep(0.4)

            # ── DC: CableCheck + PreCharge ─────────────────────────────────────
            if is_dc:
                emit_status("hlc", "Cable Check", "Safety isolation test...")
                await exchange("CableCheckReq",
                    {"DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSOC": f"{soc:.0f}%"}},
                    "DC safety: EV measures insulation resistance between DC conductors and chassis ground. Must pass before contactor can close.")
                await asyncio.sleep(0.6)
                check_stop()

                emit_status("hlc", "Pre-Charge", f"EVSE ramping to {voltage_v:.0f} V...")
                await exchange("PreChargeReq",
                    {"EVTargetVoltage": f"{voltage_v * 0.75:.0f} V", "EVTargetCurrent": "2 A",
                     "DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSOC": f"{soc:.0f}%"}},
                    "DC pre-charge: EVSE ramps output to match battery voltage before closing main contactor. Prevents inrush current that would damage contactors.")
                await asyncio.sleep(0.8)
                check_stop()

            # ── PowerDelivery Start ────────────────────────────────────────────
            await exchange("PowerDeliveryReq",
                {"ChargeProgress": "Start", "SAScheduleTupleID": 1, "ChargingProfile": "max"},
                "EV signals ready. EVSE closes main contactor — power flows. IEC 61851-1 CP pilot transitions State B (+9V) → State C (+6V).")
            await asyncio.sleep(0.3)
            check_stop()

            emit_status("charging", f"Charging — {power_kw:.0f} kW", f"{ac_mode} — power flowing")
            socketio.emit("charging_start", {"soc": soc, "mode": ac_mode,
                                              "power_kw": power_kw, "current_a": current_a, "voltage_v": voltage_v})

            # ── Charging loop ──────────────────────────────────────────────────
            req_action = "CurrentDemandReq" if is_dc else "ChargingStatusReq"
            req_desc   = ("DC: EV requests target V/A. EVSE reports present measurements. EV adjusts targets based on battery state." if is_dc else
                          "AC: EV polls charging status. EVSE reports meter reading and confirms session is active.")

            while not _session.stop_event.is_set() and soc < float(target_soc):
                if is_dc:
                    # CC-CV profile: taper current above 80% SoC
                    if soc >= 80.0:
                        taper     = 1.0 - (soc - 80.0) / max(float(target_soc) - 80.0, 0.1)
                        current_a = round(max(p["current"] * taper, p["current"] * 0.05), 1)
                        power_kw  = round(voltage_v * current_a / 1000.0, 2)
                    poll_payload = {
                        "EVTargetVoltage": f"{voltage_v:.0f} V",
                        "EVTargetCurrent": f"{current_a:.1f} A",
                        "DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSOC": f"{soc:.1f}%"},
                        "ChargingComplete": False,
                    }
                else:
                    poll_payload = {"EVSEID": "DE*CHR*E12345", "SAScheduleTupleID": 1}

                resp = await exchange(req_action, poll_payload, req_desc)
                rp   = resp.get("payload", {})

                # Update energy and SoC
                energy = rp.get("energy_kwh", energy + power_kw * 2.0 / 3600.0)
                soc    = min(soc + (power_kw * 2.0 / 3600.0) / cap_kwh * 100.0, float(target_soc))

                socketio.emit("meter_update", {
                    "soc":       round(soc, 1),
                    "power_kw":  round(power_kw, 2),
                    "energy_kwh": round(energy, 3),
                    "current_a": round(current_a, 1),
                    "voltage_v": round(voltage_v, 1),
                })

                await asyncio.sleep(2.0)

            # ── PowerDelivery Stop ─────────────────────────────────────────────
            emit_status("stopping", "Stopping", "Power ramp-down...")
            await exchange("PowerDeliveryReq",
                {"ChargeProgress": "Stop", "SAScheduleTupleID": 1},
                "EV signals stop charging. EVSE opens contactor — DC/AC power ceases. CP pilot transitions State C → State B.")
            await asyncio.sleep(0.3)

            # ── DC: Welding detection ──────────────────────────────────────────
            if is_dc:
                await exchange("WeldingDetectionReq",
                    {"DC_EVStatus": {"EVReady": False, "EVErrorCode": "NO_ERROR", "EVRESSOC": f"{soc:.1f}%"}},
                    "DC safety: EVSE checks contactor is fully open with no contact welding before releasing cable lock.")
                await asyncio.sleep(0.3)

            # ── SessionStop ────────────────────────────────────────────────────
            await exchange("SessionStopReq",
                {"Action": "Terminate"},
                "EV terminates the ISO 15118 session. EVSE clears session state and returns to standby. Cable can now be safely unplugged.")

            emit_status("idle", "Session Complete", f"Charged {energy:.2f} kWh · Final SoC {soc:.1f}%")
            socketio.emit("session_ended", {"energy_kwh": round(energy, 3), "final_soc": round(soc, 1)})

    except SessionAborted:
        emit_status("idle", "Session Stopped", "User requested stop")
        socketio.emit("session_ended", {})
    except asyncio.TimeoutError:
        emit_status("error", "Timeout", "No response from EVSE — check IP and connection")
        socketio.emit("session_ended", {})
    except ConnectionRefusedError:
        emit_status("error", "Connection Refused", f"{evse_ip}:15118 — is the EVSE app running?")
        socketio.emit("session_ended", {})
    except OSError as e:
        emit_status("error", "Network Error", str(e))
        socketio.emit("session_ended", {})
    except Exception as e:
        emit_status("error", "Error", str(e))
        socketio.emit("session_ended", {})
    finally:
        _session.running = False
        _session.stop_event.clear()


def run_session(*args):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ev_session(*args))
    loop.close()


# ── Socket.IO events ───────────────────────────────────────────────────────────
@socketio.on("start_session")
def handle_start(data):
    if _session.running:
        return
    _session.running = True
    _session.stop_event.clear()
    evse_ip = data["evse_ip"].split(":")[0].strip()
    threading.Thread(
        target=run_session,
        args=(evse_ip, data.get("auth_mode", "EIM"),
              data.get("initial_soc", 20), data.get("target_soc", 80),
              data.get("ac_mode", "AC_L2_1P")),
        daemon=True,
    ).start()

@socketio.on("stop_session")
def handle_stop():
    _session.stop_event.set()

@app.route("/")
def index():
    return render_template("ev.html")

if __name__ == "__main__":
    port = 5000
    print(f"\n  EV Client")
    print(f"  Browser UI  →  http://localhost:{port}\n")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
