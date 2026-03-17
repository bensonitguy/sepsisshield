#!/usr/bin/env python3
"""
SepsisShield – Local Demo Dashboard
====================================
Serves a real-time ICU monitoring web UI at http://localhost:8765

Reads from two Kafka topics (pulled via Confluent Cloud credentials):
  • patient_vitals   — raw vital signs (every 15 s per patient)
  • clinical_alerts  — AI agent outputs (severity, qSOFA, actions)

Falls back to a built-in simulator if Kafka is unavailable, so the
demo works offline / before data is flowing.

Usage:
    uv run sepsisshield_ui                  # auto-detect cloud
    uv run sepsisshield_ui aws              # explicit AWS env
    uv run sepsisshield_ui --simulate       # force offline simulation
    uv run sepsisshield_ui --port 8080      # custom port
"""

import argparse
import asyncio
import json
import logging
import math
import random
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ── FastAPI / Uvicorn ──────────────────────────────────────────────────────────
try:
    import uvicorn
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

# ── Confluent Kafka ────────────────────────────────────────────────────────────
try:
    from confluent_kafka import DeserializingConsumer, KafkaError
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroDeserializer
    from confluent_kafka.serialization import StringDeserializer
    CONFLUENT_KAFKA_AVAILABLE = True
except ImportError:
    CONFLUENT_KAFKA_AVAILABLE = False

import os
import subprocess


def get_project_root() -> Path:
    """Find project root by walking up from cwd looking for pyproject.toml."""
    for parent in [Path.cwd()] + list(Path.cwd().parents):
        if (parent / "pyproject.toml").exists():
            return parent
    for parent in [Path(__file__).resolve()] + list(Path(__file__).resolve().parents):
        if (parent / "pyproject.toml").exists():
            return parent
    raise FileNotFoundError("Could not find project root (no pyproject.toml found)")


def _run_terraform_output(state_path: Path) -> dict:
    result = subprocess.run(
        ["terraform", "output", "-json", f"-state={state_path}"],
        capture_output=True, text=True, check=True,
    )
    raw = json.loads(result.stdout)
    return {k: v["value"] for k, v in raw.items()}


def extract_kafka_credentials(cloud_provider: str, project_root: Optional[Path] = None) -> dict:
    if project_root is None:
        project_root = get_project_root()
    tf_dir = project_root / "terraform"
    core_state  = tf_dir / "core" / "terraform.tfstate"
    local_state = tf_dir / "lab2-vector-search" / "terraform.tfstate"
    if not core_state.exists():
        raise FileNotFoundError(f"Core terraform state not found: {core_state}")
    core_out  = _run_terraform_output(core_state)
    local_out = _run_terraform_output(local_state) if local_state.exists() else {}
    merged = {**local_out, **core_out}
    mapping = {
        "confluent_kafka_cluster_bootstrap_endpoint":  "bootstrap_servers",
        "app_manager_kafka_api_key":                   "kafka_api_key",
        "app_manager_kafka_api_secret":                "kafka_api_secret",
        "confluent_schema_registry_rest_endpoint":     "schema_registry_url",
        "app_manager_schema_registry_api_key":         "schema_registry_api_key",
        "app_manager_schema_registry_api_secret":      "schema_registry_api_secret",
        "confluent_environment_display_name":          "environment_name",
        "confluent_kafka_cluster_display_name":        "cluster_name",
        "confluent_environment_id":                    "environment_id",
        "confluent_kafka_cluster_id":                  "cluster_id",
    }
    creds = {}
    missing = []
    for tf_key, cred_key in mapping.items():
        if tf_key in merged:
            creds[cred_key] = merged[tf_key]
        else:
            missing.append(tf_key)
    if missing:
        raise KeyError(f"Missing terraform outputs: {missing}")
    return creds


def auto_detect_cloud_provider(project_root: Optional[Path] = None) -> str:
    """Detect cloud provider from terraform state files or env vars."""
    if project_root is None:
        project_root = get_project_root()
    hint = os.environ.get("CLOUD_PROVIDER", "").lower()
    if hint in ("aws", "azure"):
        return hint
    tf_dir = project_root / "terraform"
    if (tf_dir / "core" / "terraform.tfstate").exists():
        try:
            out = _run_terraform_output(tf_dir / "core" / "terraform.tfstate")
            if any("aws" in k for k in out):
                return "aws"
            if any("azure" in k for k in out):
                return "azure"
        except Exception:
            pass
    return "aws"


def _creds_from_env() -> Optional[dict]:
    """Load Kafka + Schema Registry credentials from environment variables.

    Set these in Render (or any hosting) dashboard:
        KAFKA_BOOTSTRAP_SERVERS
        KAFKA_API_KEY
        KAFKA_API_SECRET
        SCHEMA_REGISTRY_URL
        SCHEMA_REGISTRY_API_KEY
        SCHEMA_REGISTRY_API_SECRET
    """
    required = {
        "bootstrap_servers":        os.environ.get("KAFKA_BOOTSTRAP_SERVERS"),
        "kafka_api_key":            os.environ.get("KAFKA_API_KEY"),
        "kafka_api_secret":         os.environ.get("KAFKA_API_SECRET"),
        "schema_registry_url":      os.environ.get("SCHEMA_REGISTRY_URL"),
        "schema_registry_api_key":  os.environ.get("SCHEMA_REGISTRY_API_KEY"),
        "schema_registry_api_secret": os.environ.get("SCHEMA_REGISTRY_API_SECRET"),
    }
    if all(required.values()):
        return required
    return None

# ── Patient roster (mirrors sepsisshield_datagen.py) ──────────────────────────
PATIENTS = [
    {"id": "PT-001", "name": "Robert Smith",      "unit": "MICU",  "bed": "B01", "age": 68, "dx": "Septic shock - recovering"},
    {"id": "PT-002", "name": "Linda Martinez",    "unit": "SICU",  "bed": "S01", "age": 74, "dx": "Post-op CABG"},
    {"id": "PT-003", "name": "John Brown",        "unit": "NEURO", "bed": "N01", "age": 55, "dx": "TBI - moderate"},
    {"id": "PT-004", "name": "James Thompson",    "unit": "SICU",  "bed": "S02", "age": 82, "dx": "Hip fracture repair"},
    {"id": "PT-005", "name": "Barbara Johnson",   "unit": "MICU",  "bed": "B03", "age": 61, "dx": "Acute MI - recovering"},
    {"id": "PT-006", "name": "Michael Davis",     "unit": "NEURO", "bed": "N01", "age": 49, "dx": "Ischemic stroke"},
    {"id": "PT-007", "name": "Patricia Wilson",   "unit": "NEURO", "bed": "N02", "age": 77, "dx": "SAH post-clipping"},
    {"id": "PT-008", "name": "William Anderson",  "unit": "MICU",  "bed": "B04", "age": 63, "dx": "Diabetic ketoacidosis"},
    {"id": "PT-009", "name": "Jennifer Taylor",   "unit": "SICU",  "bed": "S03", "age": 42, "dx": "Trauma - MVA"},
    {"id": "PT-010", "name": "David Moore",       "unit": "MICU",  "bed": "B05", "age": 58, "dx": "CHF exacerbation"},
    {"id": "PT-011", "name": "Mary Jackson",      "unit": "NEURO", "bed": "N03", "age": 71, "dx": "GBS - improving"},
    {"id": "PT-012", "name": "Richard Harris",    "unit": "SICU",  "bed": "S04", "age": 66, "dx": "Pancreatitis"},
    {"id": "PT-013", "name": "Susan White",       "unit": "MICU",  "bed": "B06", "age": 53, "dx": "Asthma - severe"},
    {"id": "PT-014", "name": "Joseph Lewis",      "unit": "SICU",  "bed": "S05", "age": 79, "dx": "AAA repair"},
    {"id": "PT-015", "name": "Karen Walker",      "unit": "MICU",  "bed": "B07", "age": 47, "dx": "PE - anticoagulated"},
    {"id": "PT-016", "name": "Thomas Robinson",   "unit": "MICU",  "bed": "B08", "age": 72, "dx": "Post-op colectomy"},
    {"id": "PT-017", "name": "Nancy Hall",        "unit": "SICU",  "bed": "S06", "age": 67, "dx": "Biliary sepsis suspect"},
    {"id": "PT-018", "name": "Charles Young",     "unit": "MICU",  "bed": "B09", "age": 84, "dx": "Pneumonia"},
    {"id": "PT-019", "name": "Dorothy Allen",     "unit": "MICU",  "bed": "B10", "age": 58, "dx": "Post-liver transplant"},
    {"id": "PT-020", "name": "Paul King",         "unit": "SICU",  "bed": "S07", "age": 76, "dx": "Ruptured AAA repair"},
]
STABLE_IDS   = {p["id"] for p in PATIENTS[:15]}
EARLY_SEP    = {p["id"] for p in PATIENTS[15:18]}
CRITICAL_IDS = {p["id"] for p in PATIENTS[18:]}


# ── Shared state ───────────────────────────────────────────────────────────────
vitals_state: Dict[str, Dict] = {}   # patient_id → latest vitals dict
alerts_log:   List[Dict]      = []   # newest-first list of alert dicts (max 50)
connected_ws: Set[WebSocket]  = set()

SIM_START = time.time()


# ── Simulator (offline / demo mode) ───────────────────────────────────────────
def _jitter(v: float, pct: float = 0.05) -> float:
    return v * (1.0 + random.uniform(-pct, pct))


def _sim_vitals(patient: Dict, elapsed_min: float) -> Dict:
    pid = patient["id"]
    if pid in CRITICAL_IDS:
        return {
            "patient_id": pid, "patient_name": patient["name"],
            "unit": patient["unit"], "bed_number": patient["bed"],
            "age": patient["age"], "diagnosis": patient["dx"],
            "heart_rate":       int(_jitter(128, 0.10)),
            "systolic_bp":      int(_jitter(78,  0.08)),
            "diastolic_bp":     int(_jitter(48,  0.08)),
            "respiratory_rate": int(_jitter(30,  0.10)),
            "spo2":             round(max(70.0, _jitter(88.0, 0.02)), 1),
            "temperature_c":    round(_jitter(38.9, 0.005), 1),
            "consciousness":    random.choice(["Voice", "Pain", "Pain"]),
            "lactate_mmol":     round(_jitter(4.5, 0.12), 2),
            "wbc_count":        round(_jitter(22.0, 0.15), 1),
            "measurement_ts":   int(datetime.now(timezone.utc).timestamp() * 1000),
        }
    elif pid in EARLY_SEP:
        sev = min(1.0, elapsed_min / 15.0)
        return {
            "patient_id": pid, "patient_name": patient["name"],
            "unit": patient["unit"], "bed_number": patient["bed"],
            "age": patient["age"], "diagnosis": patient["dx"],
            "heart_rate":       int(_jitter(72  + sev * 38,  0.08)),
            "systolic_bp":      int(_jitter(118 - sev * 28,  0.06)),
            "diastolic_bp":     int(_jitter(76  - sev * 16,  0.06)),
            "respiratory_rate": int(_jitter(16  + sev * 10,  0.08)),
            "spo2":             round(max(70.0, _jitter(97.5 - sev * 5.5, 0.01)), 1),
            "temperature_c":    round(_jitter(37.0 + sev * 1.5, 0.003), 1),
            "consciousness":    random.choice(["Alert", "Alert", "Voice"]) if sev > 0.7 else "Alert",
            "lactate_mmol":     round(_jitter(1.1  + sev * 2.4, 0.10), 2),
            "wbc_count":        round(_jitter(8.5  + sev * 8.5, 0.15), 1),
            "measurement_ts":   int(datetime.now(timezone.utc).timestamp() * 1000),
        }
    else:
        return {
            "patient_id": pid, "patient_name": patient["name"],
            "unit": patient["unit"], "bed_number": patient["bed"],
            "age": patient["age"], "diagnosis": patient["dx"],
            "heart_rate":       int(_jitter(72,   0.15)),
            "systolic_bp":      int(_jitter(118,  0.10)),
            "diastolic_bp":     int(_jitter(76,   0.10)),
            "respiratory_rate": int(_jitter(16,   0.12)),
            "spo2":             round(min(99.0, _jitter(97.5, 0.01)), 1),
            "temperature_c":    round(_jitter(37.0, 0.005), 1),
            "consciousness":    "Alert",
            "lactate_mmol":     round(_jitter(1.1, 0.10), 2),
            "wbc_count":        round(_jitter(8.5, 0.15), 1),
            "measurement_ts":   int(datetime.now(timezone.utc).timestamp() * 1000),
        }


def _sim_alert(patient: Dict, vitals: Dict, elapsed_min: float) -> Optional[Dict]:
    pid = patient["id"]
    if pid in CRITICAL_IDS and elapsed_min > 0.5:
        return {
            "patient_id": pid, "patient_name": patient["name"],
            "unit": patient["unit"], "bed_number": patient["bed"],
            "diagnosis": patient["dx"],
            "severity": "SEPTIC_SHOCK",
            "qsofa_score": "3 (RR≥22: YES, SBP≤100: YES, Altered mentation: YES)",
            "avg_hr": vitals["heart_rate"], "avg_spo2": vitals["spo2"],
            "avg_rr": vitals["respiratory_rate"], "min_sbp": vitals["systolic_bp"],
            "avg_lactate": vitals["lactate_mmol"],
            "immediate_actions": (
                "- EMERGENCY: Call code team NOW\n"
                "- Start norepinephrine 0.1 mcg/kg/min immediately\n"
                "- 30 mL/kg crystalloid bolus STAT\n"
                "- Blood cultures x2 before antibiotics\n"
                "- Broad-spectrum antibiotics within 1 hour\n"
                "- ICU attending to bedside immediately"
            ),
            "alert_message": (
                f"SEPTIC SHOCK — {patient['name']}, {patient['unit']} {patient['bed']}. "
                f"HR {vitals['heart_rate']}, RR {vitals['respiratory_rate']}, "
                f"SBP {vitals['systolic_bp']}, SpO2 {vitals['spo2']}%, "
                f"Lactate {vitals['lactate_mmol']}. Emergency response activated."
            ),
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
    elif pid in EARLY_SEP and elapsed_min > 5.0:
        sev = min(1.0, (elapsed_min - 5.0) / 10.0)
        severity = "SEPSIS" if sev > 0.5 else "CONCERN"
        qsofa = 2 if sev > 0.5 else 1
        return {
            "patient_id": pid, "patient_name": patient["name"],
            "unit": patient["unit"], "bed_number": patient["bed"],
            "diagnosis": patient["dx"],
            "severity": severity,
            "qsofa_score": f"{qsofa} (RR≥22: {'YES' if vitals['respiratory_rate']>=22 else 'NO'}, SBP≤100: {'YES' if vitals['systolic_bp']<=100 else 'NO'}, Altered mentation: NO)",
            "avg_hr": vitals["heart_rate"], "avg_spo2": vitals["spo2"],
            "avg_rr": vitals["respiratory_rate"], "min_sbp": vitals["systolic_bp"],
            "avg_lactate": vitals["lactate_mmol"],
            "immediate_actions": (
                "- Activate Hour-1 Sepsis Bundle\n"
                "- Draw lactate and blood cultures x2 NOW\n"
                "- Start broad-spectrum antibiotics within 45 min\n"
                "- IV fluid bolus 500 mL normal saline\n"
                "- Page attending physician STAT\n"
                "- Reassess in 30 minutes"
            ),
            "alert_message": (
                f"{severity} — {patient['name']}, {patient['unit']} {patient['bed']}. "
                f"HR {vitals['heart_rate']}, RR {vitals['respiratory_rate']}, "
                f"SBP {vitals['systolic_bp']}, SpO2 {vitals['spo2']}%, "
                f"Lactate {vitals['lactate_mmol']}. Hour-1 Bundle initiated."
            ),
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
    return None


def run_simulator():
    """Background thread: update vitals_state and alerts_log from simulation."""
    alert_sent: Set[str] = set()
    while True:
        elapsed_min = (time.time() - SIM_START) / 60.0
        for p in PATIENTS:
            v = _sim_vitals(p, elapsed_min)
            vitals_state[p["id"]] = v
            alert = _sim_alert(p, v, elapsed_min)
            if alert and p["id"] not in alert_sent:
                alerts_log.insert(0, alert)
                if len(alerts_log) > 50:
                    alerts_log.pop()
                alert_sent.add(p["id"])
        time.sleep(5)


# ── Kafka consumers ────────────────────────────────────────────────────────────
def run_vitals_consumer(creds: Dict):
    if not CONFLUENT_KAFKA_AVAILABLE:
        return
    sr_conf = {
        "url": creds["schema_registry_url"],
        "basic.auth.user.info": f"{creds['schema_registry_api_key']}:{creds['schema_registry_api_secret']}",
    }
    sr_client = SchemaRegistryClient(sr_conf)
    avro_deser = AvroDeserializer(sr_client)
    consumer = DeserializingConsumer({
        "bootstrap.servers":  creds["bootstrap_servers"],
        "sasl.mechanisms":    "PLAIN",
        "security.protocol":  "SASL_SSL",
        "sasl.username":      creds["kafka_api_key"],
        "sasl.password":      creds["kafka_api_secret"],
        "group.id":           f"sepsisshield-ui-vitals-{uuid.uuid4().hex[:8]}",
        "auto.offset.reset":  "latest",
        "key.deserializer":   StringDeserializer("utf_8"),
        "value.deserializer": avro_deser,
    })
    consumer.subscribe(["patient_vitals"])
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                continue
            v = msg.value()
            if v:
                vitals_state[v["patient_id"]] = v
    finally:
        consumer.close()


def run_alerts_consumer(creds: Dict):
    if not CONFLUENT_KAFKA_AVAILABLE:
        return
    consumer = DeserializingConsumer({
        "bootstrap.servers":  creds["bootstrap_servers"],
        "sasl.mechanisms":    "PLAIN",
        "security.protocol":  "SASL_SSL",
        "sasl.username":      creds["kafka_api_key"],
        "sasl.password":      creds["kafka_api_secret"],
        "group.id":           f"sepsisshield-ui-alerts-{uuid.uuid4().hex[:8]}",
        "auto.offset.reset":  "latest",
        "key.deserializer":   StringDeserializer("utf_8"),
        "value.deserializer": StringDeserializer("utf_8"),
    })
    consumer.subscribe(["clinical_alerts"])
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                continue
            raw = msg.value()
            if raw:
                try:
                    alert = json.loads(raw)
                    alert["ts"] = datetime.now().strftime("%H:%M:%S")
                    alerts_log.insert(0, alert)
                    if len(alerts_log) > 50:
                        alerts_log.pop()
                except Exception:
                    pass
    finally:
        consumer.close()


# ── WebSocket broadcast ────────────────────────────────────────────────────────
async def broadcast_loop():
    while True:
        if connected_ws:
            payload = json.dumps({
                "type":    "update",
                "vitals":  vitals_state,
                "alerts":  alerts_log[:10],
            })
            dead = set()
            for ws in list(connected_ws):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            connected_ws.difference_update(dead)
        await asyncio.sleep(3)


# ── HTML Dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>SepsisShield – ICU Command Center</title>
<style>
  :root {
    --bg:        #0a0e1a;
    --panel:     #111827;
    --border:    #1f2d45;
    --text:      #e2e8f0;
    --muted:     #64748b;
    --green:     #10b981;
    --yellow:    #f59e0b;
    --orange:    #f97316;
    --red:       #ef4444;
    --crimson:   #991b1b;
    --blue:      #3b82f6;
    --teal:      #06b6d4;
    --font:      'Segoe UI', system-ui, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; }

  /* ── Header ── */
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 28px;
    background: linear-gradient(90deg, #0f172a 0%, #1e1b4b 100%);
    border-bottom: 1px solid var(--border);
  }
  .logo { display: flex; align-items: center; gap: 12px; }
  .logo-icon { font-size: 28px; }
  .logo-text h1 { font-size: 20px; font-weight: 700; color: #f1f5f9; letter-spacing: -.3px; }
  .logo-text p  { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .header-right { display: flex; align-items: center; gap: 20px; }
  #clock { font-size: 22px; font-weight: 300; color: var(--teal); font-variant-numeric: tabular-nums; }
  #conn-status { font-size: 12px; padding: 4px 10px; border-radius: 20px; font-weight: 600; }
  .conn-live    { background: #064e3b; color: var(--green); }
  .conn-sim     { background: #1c1917; color: var(--yellow); }

  /* ── Stats bar ── */
  .stats-bar {
    display: grid; grid-template-columns: repeat(5, 1fr);
    gap: 1px; background: var(--border);
    border-bottom: 1px solid var(--border);
  }
  .stat-cell {
    background: var(--panel); padding: 14px 20px;
    display: flex; flex-direction: column; align-items: center;
  }
  .stat-num  { font-size: 32px; font-weight: 700; line-height: 1; }
  .stat-lbl  { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; margin-top: 4px; }
  .num-green  { color: var(--green);  }
  .num-yellow { color: var(--yellow); }
  .num-orange { color: var(--orange); }
  .num-red    { color: var(--red);    }
  .num-white  { color: #f1f5f9;       }

  /* ── Main grid ── */
  .main { display: grid; grid-template-columns: 1fr 360px; height: calc(100vh - 120px); }

  /* ── Patient grid ── */
  .patient-grid {
    padding: 16px; overflow-y: auto;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
    gap: 12px; align-content: start;
  }
  .patient-card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px;
    transition: border-color .3s, box-shadow .3s;
    position: relative; overflow: hidden;
  }
  .patient-card::before {
    content: ''; position: absolute; left: 0; top: 0; bottom: 0;
    width: 4px; border-radius: 10px 0 0 10px;
  }
  .card-stable   { border-color: #1f2d45; }
  .card-stable::before { background: var(--green); }
  .card-watch    { border-color: #422006; }
  .card-watch::before  { background: var(--yellow); }
  .card-concern  { border-color: #431407; }
  .card-concern::before { background: var(--orange); }
  .card-sepsis   { border-color: #450a0a; box-shadow: 0 0 12px rgba(239,68,68,.2); }
  .card-sepsis::before  { background: var(--red); }
  .card-shock    { border-color: #450a0a; animation: pulse-red 1.5s ease-in-out infinite; }
  .card-shock::before   { background: var(--crimson); }
  @keyframes pulse-red {
    0%,100% { box-shadow: 0 0 8px rgba(239,68,68,.3); }
    50%      { box-shadow: 0 0 20px rgba(239,68,68,.7); }
  }

  .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
  .patient-name { font-size: 14px; font-weight: 600; color: #f1f5f9; }
  .patient-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .severity-badge {
    font-size: 10px; font-weight: 700; padding: 3px 8px;
    border-radius: 4px; text-transform: uppercase; letter-spacing: .5px;
    white-space: nowrap;
  }
  .badge-stable  { background: #064e3b; color: var(--green);  }
  .badge-watch   { background: #451a03; color: var(--yellow); }
  .badge-concern { background: #431407; color: var(--orange); }
  .badge-sepsis  { background: #450a0a; color: #fca5a5;       }
  .badge-shock   { background: var(--crimson); color: #fff;   }
  .badge-na      { background: #1e293b; color: var(--muted);  }

  .vitals-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
  .vital {
    background: #0f172a; border-radius: 6px; padding: 6px 8px;
    display: flex; flex-direction: column;
  }
  .vital-val { font-size: 18px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
  .vital-lbl { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .val-green  { color: var(--green);  }
  .val-yellow { color: var(--yellow); }
  .val-orange { color: var(--orange); }
  .val-red    { color: var(--red);    }
  .val-white  { color: #e2e8f0;       }

  .consciousness-row {
    margin-top: 8px; display: flex; justify-content: space-between; align-items: center;
    font-size: 11px; color: var(--muted);
  }
  .consciousness-val { font-weight: 600; }
  .alert-indicator { font-size: 10px; color: var(--red); font-weight: 700; animation: blink 1s step-end infinite; }
  @keyframes blink { 50% { opacity: 0; } }

  /* ── Alert sidebar ── */
  .alert-sidebar {
    background: #0d1525; border-left: 1px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .sidebar-header {
    padding: 14px 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
  }
  .sidebar-title { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; color: var(--teal); }
  .alert-count { background: var(--red); color: #fff; border-radius: 10px; font-size: 11px; font-weight: 700; padding: 2px 7px; }
  .alerts-list { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 10px; }

  .alert-card {
    background: var(--panel); border-radius: 8px; padding: 12px;
    border-left: 3px solid transparent;
    animation: slideIn .3s ease;
  }
  @keyframes slideIn { from { opacity:0; transform: translateX(20px); } to { opacity:1; transform: none; } }
  .alert-shock   { border-left-color: var(--crimson); background: #1a0a0a; }
  .alert-sepsis  { border-left-color: var(--red);     background: #160808; }
  .alert-concern { border-left-color: var(--orange);  background: #150c00; }
  .alert-watch   { border-left-color: var(--yellow);  background: #130f00; }

  .alert-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .alert-patient { font-size: 13px; font-weight: 700; color: #f1f5f9; }
  .alert-time    { font-size: 11px; color: var(--muted); }
  .alert-severity { font-size: 11px; font-weight: 700; margin-bottom: 4px; }
  .sev-shock   { color: #fca5a5; }
  .sev-sepsis  { color: #fca5a5; }
  .sev-concern { color: var(--orange); }
  .sev-watch   { color: var(--yellow); }
  .alert-vitals { font-size: 11px; color: var(--muted); margin-bottom: 6px; font-variant-numeric: tabular-nums; }
  .alert-qsofa  { font-size: 11px; color: var(--teal); margin-bottom: 6px; }
  .alert-actions { font-size: 11px; color: #94a3b8; line-height: 1.6; white-space: pre-line; }

  .no-alerts { color: var(--muted); font-size: 13px; text-align: center; padding: 40px 20px; line-height: 1.6; }

  /* scrollbar */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">🛡️</div>
    <div class="logo-text">
      <h1>SepsisShield</h1>
      <p>Real-Time ICU Monitoring &amp; Clinical Response</p>
    </div>
  </div>
  <div class="header-right">
    <div id="clock">--:--:--</div>
    <div id="conn-status" class="conn-sim">● SIMULATING</div>
  </div>
</header>

<div class="stats-bar">
  <div class="stat-cell"><div class="stat-num num-white" id="s-total">20</div><div class="stat-lbl">Total Patients</div></div>
  <div class="stat-cell"><div class="stat-num num-green"  id="s-stable">—</div><div class="stat-lbl">Stable</div></div>
  <div class="stat-cell"><div class="stat-num num-yellow" id="s-watch">—</div><div class="stat-lbl">Watch / Concern</div></div>
  <div class="stat-cell"><div class="stat-num num-red"    id="s-sepsis">—</div><div class="stat-lbl">Sepsis / Shock</div></div>
  <div class="stat-cell"><div class="stat-num num-orange" id="s-alerts">—</div><div class="stat-lbl">Alerts Fired</div></div>
</div>

<div class="main">
  <div class="patient-grid" id="patient-grid"></div>
  <div class="alert-sidebar">
    <div class="sidebar-header">
      <span class="sidebar-title">⚡ Live Alerts</span>
      <span class="alert-count" id="alert-count">0</span>
    </div>
    <div class="alerts-list" id="alerts-list">
      <div class="no-alerts">
        🕐 Waiting for AI agent alerts…<br><br>
        <small>Alerts appear when the streaming agent detects deterioration and classifies severity</small>
      </div>
    </div>
  </div>
</div>

<script>
const PATIENTS = """ + json.dumps(PATIENTS) + r""";
const patientMap = {};
PATIENTS.forEach(p => patientMap[p.id] = p);

let vitalsData  = {};
let alertsData  = [];
let isLive      = false;

// ── Clock ──────────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent = now.toLocaleTimeString('en-US', {hour12: false});
}
setInterval(updateClock, 1000);
updateClock();

// ── WebSocket ──────────────────────────────────────────────────────────────────
function connect() {
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${wsProto}://${location.host}/ws`);
  ws.onopen  = () => {
    isLive = true;
    document.getElementById('conn-status').className = 'conn-live';
    document.getElementById('conn-status').textContent = '● LIVE';
  };
  ws.onclose = () => {
    isLive = false;
    document.getElementById('conn-status').className = 'conn-sim';
    document.getElementById('conn-status').textContent = '● RECONNECTING';
    setTimeout(connect, 2000);
  };
  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.type === 'update') {
      vitalsData = d.vitals;
      alertsData = d.alerts;
      render();
    }
  };
}
connect();

// ── Severity helpers ───────────────────────────────────────────────────────────
function cardClass(sev)  {
  if (!sev) return 'card-stable';
  const s = sev.toUpperCase();
  if (s.includes('SHOCK'))   return 'card-shock';
  if (s.includes('SEPSIS'))  return 'card-sepsis';
  if (s.includes('CONCERN')) return 'card-concern';
  if (s.includes('WATCH'))   return 'card-watch';
  return 'card-stable';
}
function badgeClass(sev) {
  if (!sev) return 'badge-stable';
  const s = sev.toUpperCase();
  if (s.includes('SHOCK'))   return 'badge-shock';
  if (s.includes('SEPSIS'))  return 'badge-sepsis';
  if (s.includes('CONCERN')) return 'badge-concern';
  if (s.includes('WATCH'))   return 'badge-watch';
  return 'badge-stable';
}
function alertClass(sev) {
  if (!sev) return '';
  const s = sev.toUpperCase();
  if (s.includes('SHOCK'))   return 'alert-shock';
  if (s.includes('SEPSIS'))  return 'alert-sepsis';
  if (s.includes('CONCERN')) return 'alert-concern';
  return 'alert-watch';
}
function sevClass(sev) {
  if (!sev) return '';
  const s = sev.toUpperCase();
  if (s.includes('SHOCK'))   return 'sev-shock';
  if (s.includes('SEPSIS'))  return 'sev-sepsis';
  if (s.includes('CONCERN')) return 'sev-concern';
  return 'sev-watch';
}

// ── Vital colour coding ────────────────────────────────────────────────────────
function hrColor(v)  { return v>110||v<50 ? 'val-red' : v>95||v<55 ? 'val-orange' : 'val-green'; }
function sbpColor(v) { return v<85  ? 'val-red' : v<95  ? 'val-orange' : v>160 ? 'val-yellow' : 'val-green'; }
function rrColor(v)  { return v>=25 ? 'val-red' : v>=22 ? 'val-orange' : 'val-green'; }
function spo2Color(v){ return v<90  ? 'val-red' : v<93  ? 'val-orange' : 'val-green'; }
function tempColor(v){ return v>38.5? 'val-orange': v>38.0? 'val-yellow' : 'val-green'; }
function lacColor(v) { return v>=4.0? 'val-red' : v>=2.0? 'val-orange' : 'val-green'; }

// ── Patient severity from alert log ───────────────────────────────────────────
function patientSeverity(pid) {
  const a = alertsData.find(x => x.patient_id === pid);
  return a ? a.severity : null;
}

// ── Render ─────────────────────────────────────────────────────────────────────
function render() {
  renderGrid();
  renderAlerts();
  renderStats();
}

function renderGrid() {
  const grid = document.getElementById('patient-grid');
  const severityOrder = {'SEPTIC_SHOCK':0,'SHOCK':0,'SEPSIS':1,'CONCERN':2,'WATCH':3,null:4};
  const sorted = [...PATIENTS].sort((a,b) => {
    const sa = severityOrder[patientSeverity(a.id)] ?? 4;
    const sb = severityOrder[patientSeverity(b.id)] ?? 4;
    return sa - sb;
  });

  grid.innerHTML = sorted.map(p => {
    const v   = vitalsData[p.id];
    const sev = patientSeverity(p.id);
    const hasAlert = !!sev && sev !== 'STABLE';
    if (!v) return `
      <div class="patient-card card-stable">
        <div class="card-header">
          <div>
            <div class="patient-name">${p.name}</div>
            <div class="patient-meta">${p.id} · ${p.unit} ${p.bed} · ${p.age}y</div>
            <div class="patient-meta" style="color:#475569">${p.dx}</div>
          </div>
          <span class="severity-badge badge-na">AWAITING</span>
        </div>
        <div style="text-align:center;padding:16px;color:var(--muted);font-size:12px">Waiting for vitals…</div>
      </div>`;

    return `
      <div class="patient-card ${cardClass(sev)}">
        <div class="card-header">
          <div>
            <div class="patient-name">${p.name}</div>
            <div class="patient-meta">${p.id} · ${p.unit} ${p.bed} · ${p.age}y</div>
            <div class="patient-meta" style="color:#475569;font-size:10px">${p.dx}</div>
          </div>
          <span class="severity-badge ${badgeClass(sev)}">${sev || 'STABLE'}</span>
        </div>
        <div class="vitals-grid">
          <div class="vital"><span class="vital-val ${hrColor(v.heart_rate)}">${v.heart_rate}</span><span class="vital-lbl">HR bpm</span></div>
          <div class="vital"><span class="vital-val ${sbpColor(v.systolic_bp)}">${v.systolic_bp}/${v.diastolic_bp}</span><span class="vital-lbl">BP mmHg</span></div>
          <div class="vital"><span class="vital-val ${rrColor(v.respiratory_rate)}">${v.respiratory_rate}</span><span class="vital-lbl">RR /min</span></div>
          <div class="vital"><span class="vital-val ${spo2Color(v.spo2)}">${v.spo2}%</span><span class="vital-lbl">SpO₂</span></div>
          <div class="vital"><span class="vital-val ${tempColor(v.temperature_c)}">${v.temperature_c}°</span><span class="vital-lbl">Temp °C</span></div>
          <div class="vital"><span class="vital-val ${lacColor(v.lactate_mmol)}">${v.lactate_mmol}</span><span class="vital-lbl">Lactate</span></div>
        </div>
        <div class="consciousness-row">
          <span>Consciousness: <span class="consciousness-val" style="color:${v.consciousness==='Alert'?'var(--green)':'var(--red)'}">${v.consciousness}</span></span>
          ${hasAlert ? '<span class="alert-indicator">⚠ ALERT ACTIVE</span>' : ''}
        </div>
      </div>`;
  }).join('');
}

function renderAlerts() {
  const list  = document.getElementById('alerts-list');
  const count = document.getElementById('alert-count');
  count.textContent = alertsData.length;
  if (alertsData.length === 0) {
    list.innerHTML = `<div class="no-alerts">🕐 Waiting for AI agent alerts…<br><br><small>Alerts appear when the streaming agent detects deterioration and classifies severity</small></div>`;
    return;
  }
  list.innerHTML = alertsData.map(a => `
    <div class="alert-card ${alertClass(a.severity)}">
      <div class="alert-header">
        <span class="alert-patient">${a.patient_name || a.patient_id}</span>
        <span class="alert-time">${a.ts || ''}</span>
      </div>
      <div class="alert-severity ${sevClass(a.severity)}">▲ ${a.severity || 'ALERT'} · ${a.unit || ''} ${a.bed_number || ''}</div>
      ${a.qsofa_score ? `<div class="alert-qsofa">qSOFA: ${a.qsofa_score}</div>` : ''}
      <div class="alert-vitals">
        HR ${a.avg_hr}  ·  RR ${a.avg_rr}  ·  BP ${a.min_sbp}  ·  SpO₂ ${a.avg_spo2}%  ·  Lac ${a.avg_lactate}
      </div>
      ${a.immediate_actions ? `<div class="alert-actions">${a.immediate_actions}</div>` : ''}
    </div>`).join('');
}

function renderStats() {
  let stable=0, watch=0, sepsis=0;
  PATIENTS.forEach(p => {
    const sev = patientSeverity(p.id);
    if (!sev) { stable++; return; }
    const s = sev.toUpperCase();
    if (s.includes('SHOCK')||s.includes('SEPSIS')) sepsis++;
    else watch++;
  });
  document.getElementById('s-stable').textContent = stable;
  document.getElementById('s-watch').textContent  = watch;
  document.getElementById('s-sepsis').textContent = sepsis;
  document.getElementById('s-alerts').textContent = alertsData.length;
}
</script>
</body>
</html>
"""


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="SepsisShield Dashboard")


@app.get("/", response_class=HTMLResponse)
async def index():
    return DASHBOARD_HTML


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_ws.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        connected_ws.discard(ws)
    except Exception:
        connected_ws.discard(ws)


@app.on_event("startup")
async def startup():
    asyncio.create_task(broadcast_loop())


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    if not FASTAPI_AVAILABLE:
        print("ERROR: fastapi and uvicorn are required.")
        print("Run:  uv add fastapi uvicorn")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="SepsisShield Local Dashboard")
    parser.add_argument("cloud", nargs="?", help="Cloud provider (aws/azure)")
    parser.add_argument("--simulate", action="store_true", help="Force offline simulation mode")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)), help="HTTP port (default: 8765 or $PORT)")
    args = parser.parse_args()

    simulate = args.simulate
    creds    = None

    if not simulate:
        # First try env vars (used on Render / any cloud hosting)
        env_creds = _creds_from_env()
        if env_creds:
            creds = env_creds
            print("✓ Loaded Confluent Cloud credentials from environment variables")
        else:
            try:
                cloud = args.cloud or auto_detect_cloud_provider(get_project_root())
                creds = extract_kafka_credentials(cloud, get_project_root())
                print(f"✓ Connected to Confluent Cloud ({cloud})")
            except Exception as e:
                print(f"⚠  Could not load Kafka credentials: {e}")
                print("   Falling back to simulation mode.")
                simulate = True

    if simulate or not creds:
        print("▶  Running in SIMULATION mode (no Kafka required)")
        t = threading.Thread(target=run_simulator, daemon=True)
        t.start()
    else:
        print("▶  Starting Kafka consumers…")
        tv = threading.Thread(target=run_vitals_consumer, args=(creds,), daemon=True)
        ta = threading.Thread(target=run_alerts_consumer, args=(creds,), daemon=True)
        tv.start()
        ta.start()
        print("   Subscribed to: patient_vitals, clinical_alerts")
        # Pre-seed vitals_state with simulated data so the UI isn't blank
        # while waiting for the first Kafka messages to arrive
        t_seed = threading.Thread(target=run_simulator, daemon=True)
        t_seed.start()

    print(f"\n🛡️  SepsisShield Dashboard → http://localhost:{args.port}\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
