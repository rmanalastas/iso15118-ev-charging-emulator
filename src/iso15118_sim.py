"""
ISO 15118 Charging Protocol Simulator
Simulates both AC and DC V2G communication stacks between EV (EVCC) and EVSE (SECC).
"""

import threading
import time
import uuid
import math
from datetime import datetime

# ── CP Signal States (IEC 61851-1) ────────────────────────────────────────────

CP_STATES = {
    "A": {"voltage": 12.0, "label": "A — No Vehicle",        "color": "#64748b"},
    "B": {"voltage":  9.0, "label": "B — Vehicle Connected", "color": "#f59e0b"},
    "C": {"voltage":  6.0, "label": "C — Charging",          "color": "#10b981"},
    "D": {"voltage":  3.0, "label": "D — Vent Required",     "color": "#a78bfa"},
    "E": {"voltage":  0.0, "label": "E — Error",             "color": "#ef4444"},
    "F": {"voltage":-12.0, "label": "F — EVSE Fault",        "color": "#ef4444"},
}

# ── Mode Configurations ────────────────────────────────────────────────────────

AC_MODES = {
    "AC_L1":    {"voltage": 120.0, "phases": 1, "label": "AC Level 1 (120V/1Φ)",  "transfer_mode": "AC_single_phase_core"},
    "AC_L2_1P": {"voltage": 240.0, "phases": 1, "label": "AC Level 2 (240V/1Φ)",  "transfer_mode": "AC_single_phase_core"},
    "AC_L2_3P": {"voltage": 230.0, "phases": 3, "label": "AC Level 2 (230V/3Φ)",  "transfer_mode": "AC_three_phase_core"},
}

DC_MODES = {
    "DC_50":  {"voltage": 400,  "current": 125, "power_kw":  50,  "label": "DC CCS — 400V/125A (50kW)",     "transfer_mode": "DC_core"},
    "DC_150": {"voltage": 800,  "current": 200, "power_kw":  160, "label": "DC DCFC — 800V/200A (160kW)",   "transfer_mode": "DC_extended"},
    "DC_350": {"voltage": 1000, "current": 350, "power_kw":  350, "label": "DC HPC — 1000V/350A (350kW)",   "transfer_mode": "DC_extended"},
}

def _duty_to_current(duty_pct):
    if duty_pct == 5:            return None
    if 10 <= duty_pct <= 85:     return round(duty_pct * 0.6, 1)
    if 85 < duty_pct <= 96:      return round(duty_pct * 2.5 - 64, 1)
    return None

def _is_dc(mode_key):
    return mode_key.startswith("DC_")


# ══ AC V2G Message Catalog ════════════════════════════════════════════════════

# ══ SLAC Message Catalog (IEC 61851-3 / HomePlug GreenPHY) ═══════════════════
SLAC_MESSAGES = [
    {
        "id": "slac_param_req", "action": "CM_SLAC_PARAM.REQ",
        "direction": "ev→evse", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EV broadcasts CM_SLAC_PARAM.REQ over the CP pilot wire (powerline) to discover any EVSE ready for SLAC. The APPLICATION_TYPE field is always 0x00 (EV-EVSE matching). SECURITY_TYPE=0x00 selects no lower-layer security. RunID (8 bytes, random per plug event) uniquely identifies this matching attempt so that responses from other EVSEs can be filtered out.",
        "details": {"APPLICATION_TYPE": "0x00 (EV-EVSE Matching)", "SECURITY_TYPE": "0x00 (No Security)", "RunID": "A3F2C1D4E5B60789"},
    },
    {
        "id": "slac_param_cnf", "action": "CM_SLAC_PARAM.CNF",
        "direction": "evse→ev", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EVSE unicasts CM_SLAC_PARAM.CNF back to the EV's PLC MAC address confirming it will participate in SLAC. FORWARDING_STA echoes the EV's MAC. M-SOUND_TARGET_TIME (600 ms) and NUM_SOUNDS (10) tell the EV how many M-SOUND bursts to send and within what window. RESP_TYPE=0x01 means the EVSE itself (not a relay) will perform the attenuation measurement.",
        "details": {"RESP_TYPE": "0x01 (EV EVSE)", "NUM_SOUNDS": 10, "M-SOUND_TARGET_TIME": "600 ms", "Result": "0x01 (Success)"},
    },
    {
        "id": "slac_start_atten", "action": "CM_START_ATTEN_CHAR.IND",
        "direction": "ev→evse", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EV broadcasts CM_START_ATTEN_CHAR.IND to signal the EVSE(s) to begin listening for M-SOUND pulses. NUM_SOUNDS=10 and TARGET_TIME=600 ms are re-announced so the EVSE knows how many bursts to expect. The EVSE opens an attenuation measurement window and waits. Sent three times in quick succession for redundancy.",
        "details": {"NUM_SOUNDS": 10, "TARGET_TIME": "600 ms", "FORWARDING_STA": "EV MAC (broadcast)"},
    },
    {
        "id": "slac_mnbc_sound", "action": "CM_MNBC_SOUND.IND  (×10)",
        "direction": "ev→evse", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EV sends 10 M-SOUND (Multi-Network Broadcast Sound) frames spaced ~20 ms apart. Each frame carries a unique COUNTDOWN field (9→0) and is transmitted at maximum PLC power. The EVSE measures the received signal attenuation (in dB) for each of the 58 OFDM sub-carrier groups. These attenuation values are the 'fingerprint' that identifies the cable path and determines the best-matched EV↔EVSE pair in multi-socket installations.",
        "details": {"COUNTDOWN": "9 → 0", "Tx_Power": "Max", "Spacing": "~20 ms", "Carrier_Groups_Measured": 58},
    },
    {
        "id": "slac_atten_char", "action": "CM_ATTEN_CHAR.IND",
        "direction": "evse→ev", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EVSE aggregates the 10 M-SOUND measurements and sends CM_ATTEN_CHAR.IND to the EV. The AGG_GROUP payload contains the averaged attenuation per carrier group (58 × 1 byte). A low average attenuation (typically <10 dB) confirms the EV is connected to this EVSE. In multi-cable setups, the EV picks the EVSE with the lowest total attenuation for the SLAC match.",
        "details": {"NUM_SOUNDS_RCVD": 10, "AGG_GROUP": "58 values (avg ~4 dB)", "SOURCE_ADDRESS": "EVSE MAC"},
    },
    {
        "id": "slac_atten_rsp", "action": "CM_ATTEN_CHAR.RSP",
        "direction": "ev→evse", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EV acknowledges receipt of the attenuation profile. Result=0x00 (Success) tells the EVSE that the EV has accepted this as its best match. The EVSE now waits for a SLAC Match Request. If Result=0x01, the EV is still evaluating other candidates (for multi-socket). After this exchange both sides know the physical path is confirmed.",
        "details": {"Result": "0x00 (Success)", "SOURCE_ADDRESS": "EV MAC"},
    },
    {
        "id": "slac_match_req", "action": "CM_SLAC_MATCH.REQ",
        "direction": "ev→evse", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EV sends CM_SLAC_MATCH.REQ to formally request network membership. The EVCCID (EV's PLC MAC address, also used later in SessionSetupReq) and EVSEID are exchanged for binding. The RunID must match the original CM_SLAC_PARAM.REQ RunID so the EVSE can validate the chain. This is the EV saying: 'I am ready to join your HomePlug network, please provide keys.'",
        "details": {"EVCCID": "02:4A:3F:11:22:AB (EV PLC MAC)", "EVSEID": "02:1B:5C:33:44:CD (EVSE PLC MAC)", "RunID": "A3F2C1D4E5B60789"},
    },
    {
        "id": "slac_match_cnf", "action": "CM_SLAC_MATCH.CNF",
        "direction": "evse→ev", "layer": "HomePlug GreenPHY · PLC (powerline)",
        "description": "EVSE confirms the match and provides the HomePlug network credentials: NID (Network ID, 7 bytes) and NMK (Network Membership Key, 16 bytes AES). The EV uses these to join the EVSE's private HomePlug GreenPHY network. After this both nodes share a secure PLC link, enabling IPv6 communication over the CP pilot wire for V2G.",
        "details": {"NID": "B7E3A9D210F45C3 (7 bytes)", "NMK": "<16-byte AES key>", "Result": "0x01 (Match)"},
    },
    {
        "id": "slac_ipv6_ll", "action": "IPv6 Link-Local Setup",
        "direction": "ev→evse", "layer": "IPv6 / SLAAC (RFC 4862)",
        "description": "With the HomePlug network established, both nodes auto-configure link-local IPv6 addresses using SLAAC (Stateless Address Autoconfiguration). The EV derives its address from the PLC MAC via EUI-64 (fe80::/10 + 64-bit interface ID). A Neighbor Solicitation (DAD) confirms the address is unique on the link before any UDP traffic begins. This link-local address is the transport used for SDP.",
        "details": {"EV_LL_Addr": "fe80::4A:3FFF:FE11:22AB/64", "EVSE_LL_Addr": "fe80::1:2:3:4/64", "DAD": "Duplicate Address Detection — OK"},
    },
]

COMMON_SETUP_MESSAGES = [
    {
        "id": "sdp_req", "action": "SDP Request",
        "direction": "ev→evse", "layer": "UDP/IPv6 · ff02::1, port 15118",
        "description": "SECC Discovery Protocol (SDP) — The EV broadcasts a UDP multicast datagram to ff02::1 (all-nodes IPv6) on port 15118. Discovers any SECC on the local link without prior configuration. Includes the security level the EV supports (TLS/no-TLS).",
        "details": {"Protocol": "UDP multicast", "Destination": "ff02::1:15118", "SecurityType": "TLS"},
    },
    {
        "id": "sdp_res", "action": "SDP Response",
        "direction": "evse→ev", "layer": "UDP/IPv6 · unicast",
        "description": "The EVSE replies unicast with: its link-local IPv6 address, the TCP port for V2GTP, and TLS requirements. From this single packet the EV knows exactly where and how to open its V2G session.",
        "details": {"SECC_IPv6": "fe80::1:2:3:4", "V2GTP_Port": 15118, "Security": "TLS_1_2_required"},
    },
    {
        "id": "tcp_connect", "action": "TCP Connection",
        "direction": "ev→evse", "layer": "TCP · 3-way handshake",
        "description": "Standard TCP 3-way handshake (SYN→SYN-ACK→ACK) establishes a reliable, ordered connection to the EVSE V2G port. All subsequent V2G messages travel over this TCP connection, wrapped in TLS if negotiated.",
        "details": {"SYN": "EV→EVSE", "SYN-ACK": "EVSE→EV", "ACK": "EV→EVSE", "Result": "Connected"},
    },
    {
        "id": "tls_handshake", "action": "TLS 1.2 Handshake",
        "direction": "ev→evse", "layer": "TLS · certificate exchange",
        "description": "TLS ensures confidentiality and integrity. The EVSE presents its SECC leaf certificate (chain: V2G Leaf→Sub-CA→Root CA). EV validates the chain. Session keys are negotiated via ECDHE. For Plug & Charge (PnC), the EV also presents its contract certificate here.",
        "details": {"Cipher": "ECDHE-ECDSA-AES128-GCM-SHA256", "SECC_Cert": "V2G-SECC-Leaf", "Verification": "EV validates chain"},
    },
    {
        "id": "session_setup_req", "action": "SessionSetupReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "First ISO 15118-2 application message, EXI-encoded in V2GTP. The EVCCID is derived from the EV HomePlug GreenPHY MAC address. SessionID=00000000 requests a new session; non-zero attempts resumption.",
        "details": {"EVCCID": "DE-BMW-A123456789", "SessionID": "00000000 (new session)"},
    },
    {
        "id": "session_setup_res", "action": "SessionSetupRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE assigns a unique 8-byte SessionID for this session (used in ALL subsequent messages). EVSEID globally identifies this charge point. ResponseCode OK_NewSessionEstablished confirms a fresh session.",
        "details": {"ResponseCode": "OK_NewSessionEstablished", "EVSEID": "DE*CHR*E12345", "SessionID": "<assigned>"},
    },
    {
        "id": "service_disc_req", "action": "ServiceDiscoveryReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EV queries the EVSE for available services. ServiceCategory=EVCharging filters for charging. The EVSE may also advertise value-added services (internet, certificate provisioning for PnC, V2G discharging).",
        "details": {"ServiceCategory": "EVCharging"},
    },
    {
        "id": "service_disc_res", "action": "ServiceDiscoveryRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE lists available charge services (ServiceID, supported energy transfer modes: AC 1Φ/3Φ or DC) and payment options. ExternalPayment = RFID/app. Contract = Plug & Charge using ISO 15118 contract certificates.",
        "details": {"ResponseCode": "OK", "PaymentOptions": ["ExternalPayment", "Contract"]},
    },
]

# ── EIM (External Identification Means) auth messages ────────────────────────
EIM_AUTH_MESSAGES = [
    {
        "id": "payment_sel_req", "action": "PaymentServiceSelectionReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EIM mode: the driver has already authenticated externally — via RFID card tap, smartphone app, or OCPP backend pre-authorisation. SelectedPaymentOption=ExternalPayment tells the EVSE to use its normal CSMS billing flow. The EVSE forwards the session to its backend for tariff assignment.",
        "details": {"SelectedPaymentOption": "ExternalPayment", "SelectedServiceList": ["EVCharging"], "AuthMode": "EIM — driver action required (RFID/App)"},
    },
    {
        "id": "payment_sel_res", "action": "PaymentServiceSelectionRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE acknowledges ExternalPayment. At this point the EVSE has already confirmed with its CSMS that a billing account is associated with the session (e.g. RFID token whitelist, prepaid balance). A non-OK ResponseCode would abort here.",
        "details": {"ResponseCode": "OK"},
    },
    {
        "id": "auth_req", "action": "AuthorizationReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EIM AuthorizationReq: EV generates a 64-byte random GenChallenge nonce and sends it to the EVSE. The EVSE forwards this together with the RFID/session token to its CSMS (Charge Station Management System) via OCPP for a final online authorization check. If EVSEProcessing=Ongoing is returned, the EV retransmits every few seconds until the backend responds.",
        "details": {"GenChallenge": "A3F2C1D4E5B607890A1B2C3D4E5F6071 (64-byte random nonce)", "Note": "EVSE checks RFID/token against CSMS backend"},
    },
    {
        "id": "auth_res", "action": "AuthorizationRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "CSMS backend returns authorization decision. ResponseCode=OK + EVSEProcessing=Finished: RFID token/account is valid and has sufficient credit — session proceeds to charging parameter negotiation. ResponseCode=FAILED means the token is blocked or account is empty.",
        "details": {"ResponseCode": "OK", "EVSEProcessing": "Finished", "Note": "CSMS authorized — billing account confirmed"},
    },
]

# ── PnC (Plug & Charge / ISO 15118 Contract) auth messages ───────────────────
PNC_AUTH_MESSAGES = [
    {
        "id": "pnc_payment_sel_req", "action": "PaymentServiceSelectionReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "Plug & Charge mode: EV selects SelectedPaymentOption=Contract. This tells the EVSE that billing will be handled automatically using the ISO 15118 contract certificate embedded in the EV's secure element — no driver action needed. The EVSE must have advertised 'Contract' in ServiceDiscoveryRes for this to be valid.",
        "details": {"SelectedPaymentOption": "Contract", "SelectedServiceList": ["EVCharging"], "AuthMode": "PnC — fully automated, zero driver interaction"},
    },
    {
        "id": "pnc_payment_sel_res", "action": "PaymentServiceSelectionRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE confirms Contract payment mode. From this point the session is 100% automated — the vehicle authenticates itself cryptographically using its contract certificate. No RFID, no app, no PIN.",
        "details": {"ResponseCode": "OK"},
    },
    {
        "id": "pnc_payment_details_req", "action": "PaymentDetailsReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "Core PnC message — only present in Contract mode. The EV presents its complete contract certificate chain: MO Leaf Cert → MO Sub-CA 2 → MO Sub-CA 1. The eMAID (E-Mobility Account Identifier) is embedded in the leaf cert's SubjectAltName and identifies the EV's billing contract. The EVSE forwards the chain to its eMSP/CSMS backend, which validates it against the V2G Root CA trust anchor and checks the EMAID against the Certificate Revocation List (CRL) or via OCSP.",
        "details": {
            "eMAID": "DE-BMW-A12345678901-X",
            "ContractSignatureCertChain": {
                "Certificate": "MO-Leaf-Cert (ECDSA P-256 · Subject: CN=DE-BMW-A12345678901-X)",
                "SubCertificates": ["MO-Sub-CA-2 (ECDSA P-256)", "MO-Sub-CA-1 (ECDSA P-256)"],
            },
            "DHpublicKey": "03:A1:B2:C3:D4:E5:F6:... (33 bytes · ECDH P-256)",
            "Note": "[SIMULATED] Real PnC uses certs provisioned by OEM/eMSP via V2G PKI",
        },
    },
    {
        "id": "pnc_payment_details_res", "action": "PaymentDetailsRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE acknowledges the contract certificate chain and issues a GenChallenge: a 16-byte cryptographic random nonce. The EV must sign this nonce in the next AuthorizationReq using the private key from its secure element. This proves live possession of the private key — preventing replay attacks even if the certificate data were somehow intercepted.",
        "details": {
            "ResponseCode": "OK",
            "GenChallenge": "6F3A1B9C2D8E4F7A5C0B3D6E1F2A4B8C (16 bytes · CSPRNG)",
            "EVSETimeStamp": "2026-05-19T00:00:00Z",
        },
    },
    {
        "id": "pnc_auth_req", "action": "AuthorizationReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "PnC AuthorizationReq: EV signs the GenChallenge with its contract certificate's private key (ECDSA P-256 / SHA-256), stored in the vehicle's Hardware Security Module (HSM/secure element). The EVSE verifies: ① ECDSA signature is mathematically valid, ② the signing key matches the leaf certificate, ③ the cert chain is trusted by the V2G Root CA, ④ the EMAID is not on the CRL. All automated — zero driver involvement.",
        "details": {
            "Id": "ID1",
            "GenChallenge": "6F3A1B9C2D8E4F7A5C0B3D6E1F2A4B8C",
            "ContractSignature": "3045022100A3F2B1C9...E4D5 (ECDSA-P256-SHA256 · simulated)",
            "Note": "[SIMULATED] Real signature computed by EV secure element using private key",
        },
    },
    {
        "id": "pnc_auth_res", "action": "AuthorizationRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE (or its eMSP backend) returns the cryptographic verification result. ResponseCode=OK + EVSEProcessing=Finished: ECDSA signature is valid, cert chain verified to V2G Root CA, EMAID is not revoked, certificate is within validity period — session is authorized. No RFID tap, no driver confirmation, no app interaction was required at any point.",
        "details": {
            "ResponseCode": "OK",
            "EVSEProcessing": "Finished",
            "Note": "✓ ECDSA signature valid  ✓ Cert chain → V2G Root CA  ✓ EMAID not revoked",
        },
    },
]

AC_CHARGE_PARAM_MESSAGES = [
    {
        "id": "charge_param_req", "action": "ChargeParameterDiscoveryReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EV declares its AC charging capability and session preferences. The EVSE uses these to generate SASchedules (smart charging schedules). DepartureTime enables time-of-use optimization. EAmount = total energy requested. EVMinCurrent = lowest efficient operating point for the onboard charger.",
        "details": {
            "RequestedEnergyTransferMode": "<from AC mode>",
            "AC_EVChargeParameter": {"DepartureTime": "PT2H", "EAmount": "40 kWh", "EVMaxVoltage": "<V>", "EVMaxCurrent": "<A>", "EVMinCurrent": "6 A"},
        },
    },
    {
        "id": "charge_param_res", "action": "ChargeParameterDiscoveryRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE responds with its AC parameters and at least one SAScheduleTuple (smart charging plan). EVSEMinCurrent is the lowest the EVSE can supply. The EV selects a SAScheduleTupleID (used in PowerDeliveryReq) that it will follow.",
        "details": {"ResponseCode": "OK", "AC_EVSEChargeParameter": {"EVSENominalVoltage": "<V>", "EVSEMaxCurrent": "<A>", "EVSEMinCurrent": "6 A"}, "SAScheduleList": "1 schedule, full power for 2h"},
    },
    {
        "id": "power_delivery_start_req", "action": "PowerDeliveryReq  [Start]",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EV signals it is ready to receive power. ChargeProgress=Start triggers the EVSE contactor to close (AC power flows). The EV simultaneously transitions IEC 61851-1 CP from State B (+9V) to State C (+6V), physically confirming readiness. SAScheduleTupleID locks in the accepted schedule.",
        "details": {"ChargeProgress": "Start", "SAScheduleTupleID": "1"},
    },
    {
        "id": "power_delivery_start_res", "action": "PowerDeliveryRes  [Start]",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE confirms the contactor is closed and AC power is flowing to the onboard charger. AC_EVSEStatus: RCD (ground-fault detector) must be False for safe operation; EVSENotification (None/StopCharging/ReNegotiate); NotificationMaxDelay gives EV grace time to honor a stop request.",
        "details": {"ResponseCode": "OK", "AC_EVSEStatus": {"RCD": False, "EVSENotification": "None", "NotificationMaxDelay": "0 s"}},
    },
    {
        "id": "charging_status_req", "action": "ChargingStatusReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "AC charging loop — EV polls the EVSE periodically (every 10–30s) throughout the session. Detects: EVSE stop requests, EVSEMaxCurrent changes (demand response / load balancing), meter readings, EVSE faults. EV drives this loop (pull model).",
        "details": {"EVSEID": "DE*CHR*E12345"},
    },
    {
        "id": "charging_status_res", "action": "ChargingStatusRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE responds with: MeterInfo (energy in Wh, MeterID for tamper evidence), EVSEMaxCurrent (may decrease during demand response), EVSENotification=StopCharging if EVSE wants to terminate, ReceiptRequired flag. The EV must honor StopCharging within NotificationMaxDelay seconds.",
        "details": {"ResponseCode": "OK", "MeterInfo": {"MeterID": "SM-001", "MeterReading": "<Wh>"}, "EVSEMaxCurrent": "<A>", "ReceiptRequired": False},
    },
]

DC_CHARGE_PARAM_MESSAGES = [
    {
        "id": "dc_charge_param_req", "action": "ChargeParameterDiscoveryReq  [DC]",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "DC-specific charge parameter negotiation. The EV declares its full DC capability profile: maximum voltage and current limits, maximum power limit, total energy capacity (kWh), requested energy, and departure time. BulkSOC (~80%) is the target for rapid charging; FullSOC (100%) is the full charge target. The EVSE uses these to generate DC SASchedules.",
        "details": {
            "RequestedEnergyTransferMode": "DC_extended",
            "DC_EVChargeParameter": {
                "DepartureTime": "PT1H",
                "DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSSOC": "<current SoC>%"},
                "EVMaximumCurrentLimit": "<max A>",
                "EVMaximumPowerLimit": "<max kW>",
                "EVMaximumVoltageLimit": "<max V>",
                "EVEnergyCapacity": "100 kWh",
                "EVEnergyRequest": "60 kWh",
                "FullSOC": "100%",
                "BulkSOC": "80%",
            },
        },
    },
    {
        "id": "dc_charge_param_res", "action": "ChargeParameterDiscoveryRes  [DC]",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE responds with its DC capability and at least one SAScheduleTuple. DC_EVSEChargeParameter includes: EVSEMaximumCurrentLimit (hardware limit), EVSEMaximumPowerLimit, EVSEMaximumVoltageLimit, EVSEMinimumCurrentLimit (below which output is unstable), EVSEMinimumVoltageLimit, and EVSECurrentRegulationTolerance (accuracy of current regulation).",
        "details": {
            "ResponseCode": "OK",
            "DC_EVSEChargeParameter": {
                "DC_EVSEStatus": {"EVSEIsolationStatus": "Valid", "EVSEStatusCode": "EVSE_Ready"},
                "EVSEMaximumCurrentLimit": "<max A>",
                "EVSEMaximumPowerLimit": "<max kW>",
                "EVSEMaximumVoltageLimit": "<max V>",
                "EVSEMinimumCurrentLimit": "1 A",
                "EVSEMinimumVoltageLimit": "200 V",
                "EVSECurrentRegulationTolerance": "2 A",
            },
            "SAScheduleList": "1 schedule, full power available",
        },
    },
    {
        "id": "cable_check_req", "action": "CableCheckReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "DC-only safety step. Before any voltage is applied, the EV requests the EVSE to test the cable and connector for electrical isolation faults. The EVSE applies a test voltage and measures leakage current. This detects damaged insulation, ground faults, or moisture ingress in the cable that could cause a dangerous shock. IEC 62196 compliance requires this check.",
        "details": {"DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSSOC": "<SoC>%"}},
    },
    {
        "id": "cable_check_res", "action": "CableCheckRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE reports the isolation test result. EVSEProcessing=Ongoing means the test is still running (the EV retransmits CableCheckReq). EVSEIsolationStatus=Valid confirms the cable insulation resistance exceeds the safety threshold (typically >500Ω/V). If isolation fails, the session must abort for safety.",
        "details": {"ResponseCode": "OK", "EVSEProcessing": "Finished", "DC_EVSEStatus": {"EVSEIsolationStatus": "Valid", "EVSEStatusCode": "EVSE_Ready"}},
    },
    {
        "id": "precharge_req", "action": "PreChargeReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "DC-only pre-charge phase. Before closing the main high-voltage contactor inside the EV, the EVSE must ramp its output voltage to match the EV battery voltage. This prevents a large inrush current (and potential arc flash) when the contactor closes. The EV reports its current battery voltage as EVTargetVoltage and requests the EVSE to match it. EVTargetCurrent is limited during this phase (typically 2A).",
        "details": {"DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR"}, "EVTargetVoltage": "<battery V>", "EVTargetCurrent": "2 A"},
    },
    {
        "id": "precharge_res", "action": "PreChargeRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE reports its current output voltage (EVSEPresentVoltage) as it ramps toward the battery voltage. The EV compares EVSEPresentVoltage to its battery voltage — when the difference is small enough (typically <20V), the EV closes its main contactor and moves to PowerDelivery. EVSEProcessing=Ongoing while ramping.",
        "details": {"ResponseCode": "OK", "DC_EVSEStatus": {"EVSEStatusCode": "EVSE_Ready"}, "EVSEPresentVoltage": "<ramping to battery V>"},
    },
    {
        "id": "dc_power_delivery_start_req", "action": "PowerDeliveryReq  [Start]",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EV has closed its main contactor and is ready for full power delivery. ChargeProgress=Start authorizes the EVSE to enter current control mode. The EVSE will now respond to CurrentDemandReq setpoints. The CP signal remains at State C (+6V / 5% PWM). DC_EVPowerDeliveryParameter allows the EV to specify a charging profile for this session.",
        "details": {"ChargeProgress": "Start", "SAScheduleTupleID": "1", "DC_EVPowerDeliveryParameter": {"DC_EVStatus": {"EVReady": True}, "BulkChargingComplete": False, "ChargingComplete": False}},
    },
    {
        "id": "dc_power_delivery_start_res", "action": "PowerDeliveryRes  [Start]",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE confirms it has entered current control mode. DC_EVSEStatus: EVSEIsolationStatus must remain Valid throughout charging; EVSEStatusCode=EVSE_Ready. After this response, the EV immediately begins sending CurrentDemandReq to request power.",
        "details": {"ResponseCode": "OK", "DC_EVSEStatus": {"EVSEIsolationStatus": "Valid", "EVSEStatusCode": "EVSE_Ready"}},
    },
    {
        "id": "current_demand_req", "action": "CurrentDemandReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "The core DC charging loop message — sent continuously every ~250ms throughout the session. The EV's BMS (Battery Management System) drives the loop, constantly adjusting setpoints based on battery state. EVTargetCurrent: requested current (CC phase) or tapered current (CV phase). EVTargetVoltage: target pack voltage. EVMaximumCurrentLimit and EVMaximumPowerLimit protect the battery. ChargingComplete=true signals the EV wants to stop.",
        "details": {
            "DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSSOC": "<live SoC>%"},
            "EVTargetCurrent": "<A — BMS demand>",
            "EVTargetVoltage": "<V — pack voltage>",
            "EVMaximumCurrentLimit": "<max A>",
            "EVMaximumPowerLimit": "<max kW>",
            "EVMaximumVoltageLimit": "<max V>",
            "BulkChargingComplete": "<bool>",
            "ChargingComplete": "<bool>",
        },
    },
    {
        "id": "current_demand_res", "action": "CurrentDemandRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE reports its actual output every ~250ms. EVSEPresentCurrent: actual amps being delivered (may differ from request due to EVSE limits). EVSEPresentVoltage: actual output voltage. EVSECurrentLimitAchieved: true if EVSE cannot increase current further. EVSEVoltageLimitAchieved: true when output voltage reaches EVSEMaximumVoltageLimit. MeterInfo provides a running Wh count for billing. The EV's BMS uses these values to continuously adjust its next CurrentDemandReq.",
        "details": {
            "ResponseCode": "OK",
            "DC_EVSEStatus": {"EVSEIsolationStatus": "Valid", "EVSEStatusCode": "EVSE_Ready"},
            "EVSEPresentCurrent": "<actual A>",
            "EVSEPresentVoltage": "<actual V>",
            "EVSECurrentLimitAchieved": False,
            "EVSEVoltageLimitAchieved": False,
            "EVSEPowerLimitAchieved": False,
            "MeterInfo": {"MeterID": "DC-001", "MeterReading": "<Wh>"},
        },
    },
]

COMMON_STOP_MESSAGES = [
    {
        "id": "power_delivery_stop_req", "action": "PowerDeliveryReq  [Stop]",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "EV requests end of charging. ChargeProgress=Stop causes the EVSE to open its output contactor (stopping power). For AC: CP signal simultaneously returns to State B (+9V). For DC: the EV first opens its main contactor. Triggered by: target SoC reached, driver press Stop, or EVSE notification received.",
        "details": {"ChargeProgress": "Stop"},
    },
    {
        "id": "power_delivery_stop_res", "action": "PowerDeliveryRes  [Stop]",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE confirms its output contactor is open and power delivery has stopped. No more energy flows after this point. If ReceiptRequired was true in the last status message, the EV would request a MeteringReceipt before SessionStop.",
        "details": {"ResponseCode": "OK"},
    },
    {
        "id": "session_stop_req", "action": "SessionStopReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "Final application-layer message. ChargingSession=Terminate permanently closes the session and releases all EVSE resources. ChargingSession=Pause would allow later resumption (inductive/opportunity charging). After this, no more V2G messages can be exchanged.",
        "details": {"ChargingSession": "Terminate"},
    },
    {
        "id": "session_stop_res", "action": "SessionStopRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE acknowledges termination. Both sides close the TCP connection. EVSE CP returns to State B (awaiting unplug), then State A once cable is removed. All session resources freed. EVSE is now ready for the next vehicle.",
        "details": {"ResponseCode": "OK"},
    },
]

DC_WELDING_DETECTION = [
    {
        "id": "welding_detection_req", "action": "WeldingDetectionReq",
        "direction": "ev→evse", "layer": "V2G Application · EXI/V2GTP",
        "description": "DC-only post-charge safety check. High DC currents can cause the EVSE output relay contacts to weld shut (fuse together from arcing). Before the EV opens its main contactor and allows the driver to unplug, it requests the EVSE to verify its relay opened correctly. The EVSE attempts to open its relay and measures for residual voltage/current.",
        "details": {"DC_EVStatus": {"EVReady": False, "EVErrorCode": "NO_ERROR"}},
    },
    {
        "id": "welding_detection_res", "action": "WeldingDetectionRes",
        "direction": "evse→ev", "layer": "V2G Application · EXI/V2GTP",
        "description": "EVSE reports its present output voltage after attempting to open its relay. If EVSEPresentVoltage drops to near zero, the relay opened successfully — safe to unplug. If voltage persists, the relay is welded closed — the EV must not unplug (electrical shock hazard) and must alert the driver. EVSEProcessing=Ongoing if still checking.",
        "details": {"ResponseCode": "OK", "DC_EVSEStatus": {"EVSEIsolationStatus": "Valid", "EVSEStatusCode": "EVSE_Ready"}, "EVSEPresentVoltage": "0 V (relay open — safe to unplug)"},
    },
]


# ══ Simulator ═════════════════════════════════════════════════════════════════

class ISO15118Simulator:
    def __init__(self, emit_cb):
        self.emit = emit_cb
        self.running = False
        self.charging = False
        self.stop_requested = False
        self.thread = None
        self.meter_thread = None

        self.session_id = None
        self.cp_state = "A"
        self.duty_cycle = 100
        self.soc_pct = 20.0
        self.target_soc = 80.0
        self.power_kw = 0.0
        self.energy_kwh = 0.0
        self.voltage_v = 240.0
        self.phases = 1
        self.current_a = 0.0
        self.use_tls = True
        self.max_current_a = 32
        self.evse_max_current_a = 32
        self.msg_step_delay = 0.8
        self.ac_mode = "AC_L2_1P"
        self.session_start_time = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _elapsed(self):
        return round(time.time() - self.session_start_time) if self.session_start_time else 0

    def _emit_cp(self):
        s = CP_STATES[self.cp_state]
        self.emit("cp_update", {
            "state": self.cp_state, "label": s["label"], "color": s["color"],
            "voltage": s["voltage"], "duty_cycle": self.duty_cycle,
            "max_current_a": _duty_to_current(self.duty_cycle), "ts": self._ts(),
        })

    def _emit_msg(self, msg_def, extra_details=None):
        details = dict(msg_def.get("details", {}))
        if extra_details:
            details.update(extra_details)
        self.emit("v2g_msg", {
            "id": msg_def["id"], "action": msg_def["action"],
            "direction": msg_def["direction"], "layer": msg_def["layer"],
            "description": msg_def.get("description", ""),
            "details": details, "ts": self._ts(),
        })

    def _emit_status(self, phase, evse_state, ev_state, note=""):
        self.emit("status_update", {
            "phase": phase, "evse_state": evse_state, "ev_state": ev_state,
            "note": note, "ts": self._ts(),
        })

    def _emit_meter(self):
        self.emit("meter_update", {
            "power_kw":   round(self.power_kw, 2),
            "energy_kwh": round(self.energy_kwh, 3),
            "soc_pct":    round(self.soc_pct, 1),
            "current_a":  round(self.current_a, 1),
            "voltage_v":  round(self.voltage_v, 1),
            "phases":     self.phases,
            "elapsed_s":  self._elapsed(),
            "ts": self._ts(),
        })

    def _set_cp(self, state, duty=None):
        self.cp_state = state
        if duty is not None:
            self.duty_cycle = duty
        elif state in ("A", "B"):
            self.duty_cycle = 100
        self._emit_cp()

    def _sleep(self, secs):
        steps = max(1, int(secs / 0.05))
        for _ in range(steps):
            if self.stop_requested:
                return False
            time.sleep(0.05)
        return True

    # ── Session entry point ───────────────────────────────────────────────────

    def start_session(self, config=None):
        if self.running:
            return
        cfg = config or {}
        self.use_tls              = cfg.get("use_tls", True)
        self.max_current_a        = int(float(cfg.get("max_current_a", 32)))
        self.evse_max_current_a   = int(float(cfg.get("evse_max_current_a", 32)))
        self.target_soc           = float(cfg.get("target_soc", 80))
        self.msg_step_delay       = float(cfg.get("step_delay", 0.8))
        self.soc_pct              = float(cfg.get("initial_soc", 20))
        self.ac_mode              = cfg.get("ac_mode", "AC_L2_1P")
        self.energy_kwh           = 0.0
        self.stop_requested       = False
        self.running              = True
        self.session_start_time   = None
        self.auth_mode            = cfg.get("auth_mode", "EIM")

        if _is_dc(self.ac_mode):
            dc_cfg = DC_MODES.get(self.ac_mode, DC_MODES["DC_50"])
            self.voltage_v  = float(dc_cfg["voltage"])
            self.phases     = 0   # N/A for DC
            self.max_current_a      = min(self.max_current_a, dc_cfg["current"])
            self.evse_max_current_a = min(self.evse_max_current_a, dc_cfg["current"])
        else:
            ac_cfg = AC_MODES.get(self.ac_mode, AC_MODES["AC_L2_1P"])
            self.voltage_v = ac_cfg["voltage"]
            self.phases    = ac_cfg["phases"]

        self.thread = threading.Thread(target=self._run_session, daemon=True)
        self.thread.start()

    def stop_session(self):
        self.stop_requested = True

    def _run_session(self):
        try:
            self._do_session()
        except Exception as e:
            self.emit("session_error", {"error": str(e)})
        finally:
            self.running = False
            self.charging = False
            self.power_kw = 0.0
            self.current_a = 0.0
            self._set_cp("A")
            self._emit_meter()
            self._emit_status("idle", "Idle", "Idle")
            self.emit("session_ended", {})

    def _do_session(self):
        if _is_dc(self.ac_mode):
            self._do_dc_session()
        else:
            self._do_ac_session()

    # ── Common physical + setup phase ─────────────────────────────────────────

    def _do_physical_and_setup(self, d):
        """Physical connection + SDP + TCP + TLS + common V2G setup. Returns False if stopped."""
        self._emit_status("connecting", "Waiting for EV", "Plug-in…", "Proximity detection active")
        self._set_cp("A", duty=100)
        if not self._sleep(d * 0.5): return False
        self._set_cp("B", duty=100)
        self._emit_status("connecting", "EV Detected (State B)", "Connected", "IEC 61851-1 State B: +9V — EV plugged in")
        if not self._sleep(d * 0.5): return False

        # EVSE first broadcasts current offer via duty cycle (IEC 61851-1 Table A.4)
        # This is what a basic L1/L2 charger does — duty cycle encodes max available current
        if _is_dc(self.ac_mode):
            offer_duty = 5   # DC HLC always jumps straight to 5%
        elif self.ac_mode == "AC_L1":
            offer_duty = round(min(self.evse_max_current_a, 16) / 0.6)   # ≤16A → 10–27%
        else:
            # AC L2: duty encodes max current (0.6×duty for 10–85%, 2.5×duty−64 for 85–96%)
            a = min(self.evse_max_current_a, 80)
            offer_duty = round(a / 0.6) if a <= 51 else round((a + 64) / 2.5)

        self._set_cp("B", duty=offer_duty)
        self._emit_status("connecting",
                          f"Offering {min(self.evse_max_current_a,80)}A via PWM",
                          "Reading CP signal",
                          f"IEC 61851-1: {offer_duty}% PWM → EVSE offering {min(self.evse_max_current_a,80)}A max (basic AC mode)")
        if not self._sleep(d * 0.8): return False

        # EV supports ISO 15118 → EVSE switches to 5% to announce HLC capability
        self._set_cp("B", duty=5)
        self._emit_status("connecting", "HLC Mode (5% PWM)", "HLC Detected",
                          "CP switches to 5% PWM — ISO 15118 HLC signal: 'use digital comms, ignore duty-cycle current limit'")
        if not self._sleep(d * 0.8): return False

        self.phase = "hlc"

        # ── SLAC phase (IEC 61851-3 / HomePlug GreenPHY) ──────────────────────
        self._emit_status("hlc", "SLAC — PLC Matching", "CM_SLAC_PARAM.REQ",
                          "EV broadcasts CM_SLAC_PARAM.REQ over powerline — beginning SLAC matching")
        for i, msg in enumerate(SLAC_MESSAGES):
            if self.stop_requested: return False
            self._emit_msg(msg)
            # M-SOUND burst gets a longer slot (represents 10 frames × ~20 ms)
            delay = d * 1.1 if msg["id"] == "slac_mnbc_sound" else d * 0.65
            if not self._sleep(delay): return False
            # Update status after key milestones
            if msg["id"] == "slac_param_cnf":
                self._emit_status("hlc", "SLAC — Attenuation Measurement",
                                  "M-SOUND bursts", "EVSE measuring PLC attenuation across 58 carrier groups")
            elif msg["id"] == "slac_match_cnf":
                self._emit_status("hlc", "SLAC Complete — Network Joined",
                                  "HomePlug NMK set", "EV joined EVSE HomePlug GreenPHY network · IPv6 link-local forming")

        # ── SDP (SECC Discovery Protocol) over IPv6/UDP ────────────────────────
        self._emit_status("hlc", "SDP — SECC Discovery", "Broadcasting SDP",
                          "EV sends UDP multicast to ff02::1:15118 — discovering EVSE IP/port")
        for msg in COMMON_SETUP_MESSAGES[:2]:   # SDP req + res
            if self.stop_requested: return False
            self._emit_msg(msg)
            if not self._sleep(d * 0.6): return False

        # ── TCP + TLS ──────────────────────────────────────────────────────────
        self._emit_msg(COMMON_SETUP_MESSAGES[2])  # TCP
        if not self._sleep(d * 0.4): return False
        if self.use_tls:
            tls_msg = dict(COMMON_SETUP_MESSAGES[3])
            if self.auth_mode == "PnC":
                tls_msg = dict(tls_msg)
                tls_msg["description"] = (
                    "PnC Mutual TLS: both sides authenticate. EVSE presents its SECC leaf certificate "
                    "(chain: SECC Leaf → CPO Sub-CA 2 → CPO Sub-CA 1 → V2G Root CA). The EV presents "
                    "its OEM Provisioning Certificate for channel binding — the contract certificate is "
                    "exchanged later in PaymentDetailsReq. Session keys negotiated via ECDHE-ECDSA."
                )
                tls_msg["details"] = {
                    "Cipher": "ECDHE-ECDSA-AES128-GCM-SHA256",
                    "SECC_Cert": "CPO-SECC-Leaf → CPO-Sub-CA-2 → CPO-Sub-CA-1 → V2G Root CA",
                    "EV_Cert": "OEM Provisioning Cert (channel binding)",
                    "MutualTLS": "Yes — EV authenticates to EVSE",
                }
            self._emit_msg(tls_msg)
            if not self._sleep(d * 0.7): return False

        # ── SessionSetup + ServiceDiscovery (common to EIM and PnC) ───────────
        for msg in COMMON_SETUP_MESSAGES[4:]:
            if self.stop_requested: return False
            extra = {}
            if msg["id"] == "session_setup_req":
                self.session_id = uuid.uuid4().hex[:16].upper()
            if msg["id"] == "session_setup_res":
                extra = {"SessionID": self.session_id}
            self._emit_msg(msg, extra)
            if not self._sleep(d * 0.55): return False

        # ── Auth messages: EIM (RFID) or PnC (Contract Certificate) ───────────
        is_pnc = self.auth_mode == "PnC"
        auth_msgs = PNC_AUTH_MESSAGES if is_pnc else EIM_AUTH_MESSAGES
        if is_pnc:
            self._emit_status("app", "PnC — Contract Auth", "PaymentDetailsReq",
                              "EV presenting contract certificate chain (EMAID + MO cert chain)")
        else:
            self._emit_status("app", "EIM — RFID/App Auth", "AuthorizationReq",
                              "EVSE checking RFID token against CSMS backend")
        for msg in auth_msgs:
            if self.stop_requested: return False
            self._emit_msg(msg)
            delay = d * 0.8 if msg["id"] in ("pnc_payment_details_req", "pnc_auth_req") else d * 0.55
            if not self._sleep(delay): return False
        return True

    # ── AC Session ────────────────────────────────────────────────────────────

    def _do_ac_session(self):
        d = self.msg_step_delay
        ac_cfg = AC_MODES.get(self.ac_mode, AC_MODES["AC_L2_1P"])
        transfer_mode = ac_cfg["transfer_mode"]

        if not self._do_physical_and_setup(d):
            return self._do_stop_sequence()

        # ChargeParameterDiscovery
        self._emit_msg(AC_CHARGE_PARAM_MESSAGES[0], {
            "RequestedEnergyTransferMode": transfer_mode,
            "AC_EVChargeParameter": {
                "DepartureTime": "PT2H", "EAmount": "40 kWh",
                "EVMaxVoltage": f"{self.voltage_v} V",
                "EVMaxCurrent": f"{self.max_current_a} A", "EVMinCurrent": "6 A",
            },
        })
        if not self._sleep(d * 0.55): return self._do_stop_sequence()
        self._emit_msg(AC_CHARGE_PARAM_MESSAGES[1], {
            "AC_EVSEChargeParameter": {
                "EVSENominalVoltage": f"{self.voltage_v} V",
                "EVSEMaxCurrent": f"{self.evse_max_current_a} A", "EVSEMinCurrent": "6 A",
            }
        })
        if not self._sleep(d * 0.55): return self._do_stop_sequence()

        # PowerDeliveryReq [Start]
        self._emit_msg(AC_CHARGE_PARAM_MESSAGES[2])
        if not self._sleep(d * 0.4): return self._do_stop_sequence()
        self._emit_msg(AC_CHARGE_PARAM_MESSAGES[3])

        # Go to State C
        if not self._sleep(d * 0.3): return self._do_stop_sequence()
        self._set_cp("C", duty=5)
        actual_a = min(self.max_current_a, self.evse_max_current_a)
        self.current_a = actual_a
        self.power_kw = round(self.voltage_v * actual_a * self.phases / 1000.0, 2)
        self.session_start_time = time.time()
        self._emit_status("charging",
                          f"Charging — State C · {self.power_kw:.1f} kW",
                          f"Charging ⚡ {self.power_kw:.1f} kW",
                          f"CP State C: +6V / 5% PWM — {self.power_kw:.1f} kW ({self.phases}Φ × {self.voltage_v}V × {actual_a}A)")
        self.charging = True
        self._emit_meter()
        self._start_meter_thread(self._ac_meter_loop)

        # ChargingStatusReq/Res loop
        self._emit_msg(AC_CHARGE_PARAM_MESSAGES[4], {"SoC": f"{self.soc_pct:.1f}%"})
        if not self._sleep(d * 0.4): return self._do_stop_sequence()
        self._emit_msg(AC_CHARGE_PARAM_MESSAGES[5], {
            "MeterInfo": {"MeterID": "SM-001", "MeterReading": f"{round(self.energy_kwh*1000)} Wh"},
            "EVSEMaxCurrent": f"{self.evse_max_current_a} A",
        })
        if not self._sleep(d): return self._do_stop_sequence()

        while self.charging and not self.stop_requested:
            if not self._sleep(d * 1.5): break
            if self.stop_requested: break
            self._emit_msg(AC_CHARGE_PARAM_MESSAGES[4], {"SoC": f"{self.soc_pct:.1f}%"})
            if not self._sleep(d * 0.4): break
            self._emit_msg(AC_CHARGE_PARAM_MESSAGES[5], {
                "MeterInfo": {"MeterID": "SM-001", "MeterReading": f"{round(self.energy_kwh*1000)} Wh"},
                "EVSEMaxCurrent": f"{self.evse_max_current_a} A",
                "SoC": f"{self.soc_pct:.1f}%",
            })

        return self._do_stop_sequence()

    # ── DC Session ────────────────────────────────────────────────────────────

    def _do_dc_session(self):
        d = self.msg_step_delay
        dc_cfg = DC_MODES.get(self.ac_mode, DC_MODES["DC_50"])
        max_v = dc_cfg["voltage"]
        max_a = min(self.max_current_a, self.evse_max_current_a, dc_cfg["current"])
        max_kw = dc_cfg["power_kw"]
        # Battery voltage model: scales from ~75% to 100% of max_v across SoC
        battery_v = lambda soc: max_v * (0.75 + 0.25 * soc / 100.0)
        self.voltage_v = round(battery_v(self.soc_pct), 1)

        if not self._do_physical_and_setup(d):
            return self._do_stop_sequence()

        # DC ChargeParameterDiscovery
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[0], {
            "RequestedEnergyTransferMode": dc_cfg["transfer_mode"],
            "DC_EVChargeParameter": {
                "DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSSOC": f"{self.soc_pct:.0f}%"},
                "EVMaximumCurrentLimit": f"{max_a} A",
                "EVMaximumPowerLimit": f"{max_kw} kW",
                "EVMaximumVoltageLimit": f"{max_v} V",
                "EVEnergyCapacity": "100 kWh",
                "BulkSOC": "80%", "FullSOC": "100%",
            },
        })
        if not self._sleep(d * 0.55): return self._do_stop_sequence()
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[1], {
            "DC_EVSEChargeParameter": {
                "EVSEMaximumCurrentLimit": f"{max_a} A",
                "EVSEMaximumPowerLimit": f"{max_kw} kW",
                "EVSEMaximumVoltageLimit": f"{max_v} V",
                "EVSEMinimumCurrentLimit": "1 A",
                "EVSEMinimumVoltageLimit": "200 V",
            }
        })
        if not self._sleep(d * 0.55): return self._do_stop_sequence()

        # CableCheck
        self._emit_status("hlc", "Cable Check", "Cable Check", "Safety isolation test — measuring insulation resistance")
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[2], {
            "DC_EVStatus": {"EVReady": True, "EVErrorCode": "NO_ERROR", "EVRESSSOC": f"{self.soc_pct:.0f}%"}
        })
        if not self._sleep(d * 0.9): return self._do_stop_sequence()
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[3])
        if not self._sleep(d * 0.55): return self._do_stop_sequence()

        # PreCharge — ramp EVSE voltage to battery voltage
        target_v = self.voltage_v
        self._emit_status("hlc", "Pre-Charge", "Pre-Charging",
                          f"EVSE ramping output to battery voltage ({target_v:.0f}V) before contactor close")
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[4], {
            "EVTargetVoltage": f"{target_v:.0f} V", "EVTargetCurrent": "2 A",
        })

        # Ramp self.voltage_v from 30% up to battery voltage in steps so the
        # oscilloscope and meter display show the actual voltage climbing.
        ramp_start = max_v * 0.30
        self.voltage_v = round(ramp_start, 1)
        self.current_a = 2.0   # pre-charge current is limited to 2 A
        self.power_kw  = round(self.voltage_v * self.current_a / 1000.0, 3)
        self._emit_meter()

        n_steps = 8
        for step in range(1, n_steps + 1):
            frac = step / n_steps
            self.voltage_v = round(ramp_start + (target_v - ramp_start) * frac, 1)
            self.power_kw  = round(self.voltage_v * self.current_a / 1000.0, 3)
            self._emit_meter()
            # emit a PreChargeRes message at the midpoint and at the end
            if step == n_steps // 2:
                self._emit_msg(DC_CHARGE_PARAM_MESSAGES[5],
                               {"EVSEPresentVoltage": f"{self.voltage_v:.0f} V (ramping…)"})
            if not self._sleep(d * 0.25): return self._do_stop_sequence()

        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[5],
                       {"EVSEPresentVoltage": f"{target_v:.0f} V (matched — contactor closing)"})
        self.voltage_v = round(target_v, 1)
        self._emit_meter()
        if not self._sleep(d * 0.4): return self._do_stop_sequence()

        # PowerDelivery [Start]
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[6])
        if not self._sleep(d * 0.4): return self._do_stop_sequence()
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[7])
        if not self._sleep(d * 0.3): return self._do_stop_sequence()

        # Begin charging
        self._set_cp("C", duty=5)
        self.current_a = max_a
        self.voltage_v = round(battery_v(self.soc_pct), 1)
        self.power_kw  = round(self.voltage_v * self.current_a / 1000.0, 2)
        self.session_start_time = time.time()
        self._emit_status("charging",
                          f"DC Charging ⚡ {self.power_kw:.0f} kW",
                          f"Charging ⚡ {self.power_kw:.0f} kW",
                          f"DC CC phase: {self.voltage_v:.0f}V × {self.current_a}A = {self.power_kw:.1f} kW")
        self.charging = True
        self._emit_meter()
        self._start_meter_thread(self._dc_meter_loop)

        # CurrentDemand loop
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[8], {
            "DC_EVStatus": {"EVReady": True, "EVRESSSOC": f"{self.soc_pct:.1f}%"},
            "EVTargetCurrent": f"{self.current_a:.0f} A",
            "EVTargetVoltage": f"{self.voltage_v:.0f} V",
            "ChargingComplete": False,
        })
        if not self._sleep(d * 0.4): return self._do_stop_sequence()
        self._emit_msg(DC_CHARGE_PARAM_MESSAGES[9], {
            "EVSEPresentCurrent": f"{self.current_a:.0f} A",
            "EVSEPresentVoltage": f"{self.voltage_v:.0f} V",
            "MeterInfo": {"MeterID": "DC-001", "MeterReading": f"{round(self.energy_kwh*1000)} Wh"},
        })
        if not self._sleep(d): return self._do_stop_sequence()

        while self.charging and not self.stop_requested:
            if not self._sleep(d * 1.2): break
            if self.stop_requested: break
            bulk = self.soc_pct >= 80.0
            self._emit_msg(DC_CHARGE_PARAM_MESSAGES[8], {
                "DC_EVStatus": {"EVReady": True, "EVRESSSOC": f"{self.soc_pct:.1f}%"},
                "EVTargetCurrent": f"{self.current_a:.1f} A",
                "EVTargetVoltage": f"{self.voltage_v:.0f} V",
                "BulkChargingComplete": bulk,
                "ChargingComplete": False,
            })
            if not self._sleep(d * 0.4): break
            self._emit_msg(DC_CHARGE_PARAM_MESSAGES[9], {
                "EVSEPresentCurrent": f"{self.current_a:.1f} A",
                "EVSEPresentVoltage": f"{self.voltage_v:.0f} V",
                "MeterInfo": {"MeterID": "DC-001", "MeterReading": f"{round(self.energy_kwh*1000)} Wh"},
                "SoC": f"{self.soc_pct:.1f}%",
                "Phase": "CC" if not bulk else "CV (tapering)",
            })

        # Welding detection before full teardown
        self._do_stop_sequence(dc_welding=True)

    def _do_stop_sequence(self, dc_welding=False):
        self.charging = False
        self.stop_requested = False
        self._set_cp("B", duty=5)
        self._emit_status("stopping", "Session Stopping", "Stopping…", "Power ramp-down — contactor opening")
        time.sleep(0.3)

        for msg in COMMON_STOP_MESSAGES:
            self._emit_msg(msg)
            time.sleep(self.msg_step_delay * 0.55)

        # DC welding detection
        if dc_welding:
            time.sleep(self.msg_step_delay * 0.4)
            for msg in DC_WELDING_DETECTION:
                self._emit_msg(msg)
                time.sleep(self.msg_step_delay * 0.55)

        self._set_cp("B", duty=100)
        time.sleep(0.3)
        self._set_cp("A", duty=100)
        self._emit_status("idle", "Idle", "Unplugged", "Session complete — cable disconnected")
        self.power_kw  = 0.0
        self.current_a = 0.0
        self._emit_meter()

    # ── Meter loops ───────────────────────────────────────────────────────────

    def _start_meter_thread(self, loop_fn):
        self.meter_thread = threading.Thread(target=loop_fn, daemon=True)
        self.meter_thread.start()

    def _ac_meter_loop(self):
        interval = 2.0
        cap_kwh = 60.0
        while self.charging and not self.stop_requested:
            time.sleep(interval)
            if not self.charging: break
            delta = self.power_kw * (interval / 3600.0)
            self.energy_kwh += delta
            self.soc_pct = min(self.soc_pct + delta / cap_kwh * 100.0, self.target_soc)
            if self.soc_pct >= self.target_soc:
                self.stop_requested = True
            self._emit_meter()

    def _dc_meter_loop(self):
        """DC CC-CV charging profile:
        - CC phase (SoC < 80%): constant current, voltage rises with battery SoC
        - CV phase (SoC ≥ 80%): voltage held at max, current tapers as SoC → target
        """
        interval  = 2.0
        cap_kwh   = 100.0
        dc_cfg    = DC_MODES.get(self.ac_mode, DC_MODES["DC_50"])
        max_v     = dc_cfg["voltage"]
        max_a     = min(self.max_current_a, self.evse_max_current_a, dc_cfg["current"])
        cv_start  = 80.0  # SoC% where CV phase begins

        while self.charging and not self.stop_requested:
            time.sleep(interval)
            if not self.charging: break

            # Battery voltage model: rises linearly from 75% to 100% of max_v
            batt_v = max_v * (0.75 + 0.25 * self.soc_pct / 100.0)
            self.voltage_v = round(batt_v, 1)

            if self.soc_pct < cv_start:
                # CC phase — full current
                self.current_a = round(max_a, 1)
                phase_note = "CC"
            else:
                # CV phase — taper current linearly
                taper = 1.0 - (self.soc_pct - cv_start) / (self.target_soc - cv_start + 1e-6)
                self.current_a = round(max(max_a * taper, max_a * 0.05), 1)
                phase_note = "CV"

            self.power_kw = round(self.voltage_v * self.current_a / 1000.0, 2)
            delta = self.power_kw * (interval / 3600.0)
            self.energy_kwh += delta
            self.soc_pct = min(self.soc_pct + delta / cap_kwh * 100.0, self.target_soc)

            # Update status note with CC/CV phase
            if self.charging:
                self._emit_status("charging",
                                  f"DC Charging ⚡ {self.power_kw:.1f} kW ({phase_note})",
                                  f"⚡ {self.power_kw:.1f} kW — {phase_note} phase",
                                  f"{phase_note} phase: {self.voltage_v:.0f}V × {self.current_a:.0f}A = {self.power_kw:.1f} kW  SoC {self.soc_pct:.1f}%")

            if self.soc_pct >= self.target_soc:
                self.stop_requested = True

            self._emit_meter()
