# ISO 15118 EV/EVSE Charging Protocol Emulator

A browser-based interactive simulator for the **ISO 15118** Vehicle-to-Grid (V2G) communication protocol stack used in modern EV charging.

Built as a companion tool for the book **"EV Charging Systems — From Connector to Cloud"**.

---

## What it simulates

The full ISO 15118-2 handshake from physical plug-in to energy delivery:

| Phase | Protocol Layer | What you see |
|-------|---------------|--------------|
| 1 · Physical | IEC 61851-1 CP Signal | PWM duty cycle oscilloscope (State A→B→C) |
| 2 · SLAC | HomePlug GreenPHY / PLC | 9-message powerline matching sequence |
| 3 · HLC Setup | IPv6 · UDP · TCP · TLS | SDP discovery, TCP handshake, TLS cert exchange |
| 4 · Application | ISO 15118-2 EXI/V2GTP | SessionSetup → ServiceDiscovery → Auth → ChargeParams |
| 5 · Charging | AC or DC power delivery | Live oscilloscope: power, current, voltage, energy, SoC |
| 6 · Stop | Session teardown | PowerDelivery → SessionStop → cable disconnect |

### Supported modes
- **AC Level 1** — 1Φ 120V / 16A (1.9 kW)
- **AC Level 2 · 1Φ** — 1Φ 240V / 32A (7.7 kW)
- **AC Level 2 · 3Φ** — 3Φ 230V / 32A (22 kW)
- **DC CCS** — 400V / 125A (50 kW)
- **DC DCFC** — 800V / 200A (160 kW)
- **DC HPC** — 1000V / 350A (350 kW)

### Authentication modes
- **EIM** (External Identification Means) — RFID / smartphone app flow
- **PnC** (Plug & Charge) — automated ISO 15118 contract certificate flow with simulated ECDSA signature

---

## Requirements

- Python 3.9 or later
- A modern web browser (Chrome, Firefox, Edge, Safari)

---

## Quick start

**1. Clone the repository**
```bash
git clone https://github.com/<your-username>/iso15118-emulator.git
cd iso15118-emulator
```

**2. Install dependencies** (one time)
```bash
pip install -r requirements.txt
```

**3. Run the server**
```bash
python web_main.py
```

**4. Open your browser**
```
http://localhost:5000
```

The simulator opens automatically. No account, no internet connection, no external services required.

---

## File structure

```
iso15118-emulator/
├── web_main.py          # Entry point — Flask server + Socket.IO
├── requirements.txt     # Python dependencies
├── src/
│   └── iso15118_sim.py  # Protocol simulation engine
│                        #   · SLAC_MESSAGES        — HomePlug GreenPHY PLC
│                        #   · COMMON_SETUP_MESSAGES — SDP/TCP/TLS/SessionSetup
│                        #   · EIM_AUTH_MESSAGES     — RFID authorization flow
│                        #   · PNC_AUTH_MESSAGES     — Plug & Charge cert flow
│                        #   · AC_CHARGE_PARAM_MESSAGES
│                        #   · DC_*_MESSAGES         — CablCheck/PreCharge/DC loop
│                        #   · STOP_MESSAGES
└── templates/
    └── index.html       # Single-page UI (Chart.js oscilloscope, flow panel, controls)
```

---

## How to use

1. **Select charging mode** — AC Level 1/2 or DC CCS/DCFC/HPC
2. **Set Start SoC and Target SoC** — simulates a realistic battery state
3. **Choose Auth mode** — EIM (RFID) or PnC (Plug & Charge)
4. **Toggle TLS** — on by default (PnC always uses mutual TLS)
5. **Click ▶ Start Session** — watch the full ISO 15118 handshake unfold in real time
6. **Click any message row** to expand its description and payload fields
7. **Use the Speed buttons** (0.5×, 1×, 2×, 5×) to control playback speed

---

## Learning path

If you are reading the book alongside this simulator, the chapters map to the phases:

- **Chapter 3** → Phase 1 (IEC 61851-1 CP signal, duty cycle, State A/B/C)
- **Chapter 4** → Phase 2 (SLAC, HomePlug GreenPHY, PLC attenuation matching)
- **Chapter 5** → Phase 3 (SDP, IPv6, TCP, TLS, V2G PKI)
- **Chapter 6** → Phase 4 (ISO 15118-2 application messages, EXI encoding)
- **Chapter 7** → EIM vs Plug & Charge authentication, V2G certificate hierarchy
- **Chapter 8** → Phase 5 (AC/DC charging profiles, CC-CV, smart charging schedules)

---

## Modifying the simulation

All protocol messages live in `src/iso15118_sim.py` as plain Python dictionaries — no binary parsing, no external protocol libraries. Each message has:

```python
{
    "id":          "unique_id",
    "action":      "MessageName",
    "direction":   "ev→evse",          # or "evse→ev"
    "layer":       "Protocol layer",
    "description": "Plain-English explanation",
    "details":     {"Field": "Value"},  # shown in expanded view
}
```

To add a message, copy an existing entry, change the fields, and insert it into the appropriate list. The UI picks it up automatically.

---

## License

MIT — free to use, modify, and include in educational materials with attribution.
