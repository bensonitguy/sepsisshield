#!/usr/bin/env python3
"""
SepsisShield - Real-Time ICU Patient Deterioration & Clinical Response Agent
Data Generator

Streams continuous patient vital signs to Kafka simulating a 20-bed ICU:
  - 15 patients with stable normal vitals (baseline)
  -  3 patients with slowly deteriorating vitals (early sepsis pattern)
  -  2 patients with sudden critical deterioration

The anomaly detection algorithm needs a baseline window (~5 minutes) before it
can detect the deteriorating patients.

Usage:
    uv run sepsisshield_datagen                    # Auto-detect cloud provider
    uv run sepsisshield_datagen aws                # Target AWS environment
    uv run sepsisshield_datagen --crisis-now       # Skip warm-up, trigger crisis immediately
    uv run sepsisshield_datagen --duration 1800    # Run for 30 minutes total
    uv run sepsisshield_datagen --dry-run          # Validate setup only
"""

import argparse
import logging
import math
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from confluent_kafka import SerializingProducer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import StringSerializer
    CONFLUENT_KAFKA_AVAILABLE = True
except ImportError:
    CONFLUENT_KAFKA_AVAILABLE = False

from .common.cloud_detection import auto_detect_cloud_provider, suggest_cloud_provider
from .common.terraform import extract_kafka_credentials, validate_terraform_state, get_project_root
from .common.logging_utils import setup_logging


TOPIC_NAME = "patient_vitals"

AVRO_SCHEMA = """{
  "type": "record",
  "name": "patient_vitals_value",
  "namespace": "org.apache.flink.avro.generated.record",
  "fields": [
    {"name": "reading_id",         "type": "string"},
    {"name": "patient_id",         "type": "string"},
    {"name": "patient_name",       "type": "string"},
    {"name": "unit",               "type": "string"},
    {"name": "bed_number",         "type": "string"},
    {"name": "age",                "type": "int"},
    {"name": "heart_rate",         "type": "int"},
    {"name": "systolic_bp",        "type": "int"},
    {"name": "diastolic_bp",       "type": "int"},
    {"name": "respiratory_rate",   "type": "int"},
    {"name": "spo2",               "type": "float"},
    {"name": "temperature_c",      "type": "float"},
    {"name": "consciousness",      "type": "string"},
    {"name": "lactate_mmol",       "type": "float"},
    {"name": "wbc_count",          "type": "float"},
    {"name": "diagnosis",          "type": "string"},
    {"name": "measurement_ts",     "type": {"type": "long", "logicalType": "timestamp-millis"}}
  ]
}"""

# ICU patient roster — 20 beds
PATIENTS: List[Dict[str, Any]] = [
    # Stable patients (beds 1-15)
    {"id": "PT-001", "name": "Margaret Chen",     "unit": "MICU", "bed": "B01", "age": 68, "dx": "Post-op CABG"},
    {"id": "PT-002", "name": "Robert Williams",   "unit": "MICU", "bed": "B02", "age": 74, "dx": "COPD exacerbation"},
    {"id": "PT-003", "name": "Linda Martinez",    "unit": "SICU", "bed": "S01", "age": 55, "dx": "Post-op bowel resection"},
    {"id": "PT-004", "name": "James Thompson",    "unit": "SICU", "bed": "S02", "age": 82, "dx": "Hip fracture repair"},
    {"id": "PT-005", "name": "Barbara Johnson",   "unit": "MICU", "bed": "B03", "age": 61, "dx": "Acute MI - recovering"},
    {"id": "PT-006", "name": "Michael Davis",     "unit": "NEURO", "bed": "N01", "age": 49, "dx": "Ischemic stroke"},
    {"id": "PT-007", "name": "Patricia Wilson",   "unit": "NEURO", "bed": "N02", "age": 77, "dx": "SAH post-clipping"},
    {"id": "PT-008", "name": "William Anderson",  "unit": "MICU", "bed": "B04", "age": 63, "dx": "Diabetic ketoacidosis"},
    {"id": "PT-009", "name": "Jennifer Taylor",   "unit": "SICU", "bed": "S03", "age": 42, "dx": "Trauma - MVA"},
    {"id": "PT-010", "name": "David Moore",       "unit": "MICU", "bed": "B05", "age": 58, "dx": "CHF exacerbation"},
    {"id": "PT-011", "name": "Mary Jackson",      "unit": "NEURO", "bed": "N03", "age": 71, "dx": "GBS - improving"},
    {"id": "PT-012", "name": "Richard Harris",    "unit": "SICU", "bed": "S04", "age": 66, "dx": "Pancreatitis"},
    {"id": "PT-013", "name": "Susan White",       "unit": "MICU", "bed": "B06", "age": 53, "dx": "Asthma - severe"},
    {"id": "PT-014", "name": "Joseph Lewis",      "unit": "SICU", "bed": "S05", "age": 79, "dx": "AAA repair"},
    {"id": "PT-015", "name": "Karen Walker",      "unit": "MICU", "bed": "B07", "age": 47, "dx": "PE - anticoagulated"},
    # Deteriorating patients (beds 16-18) — early sepsis pattern
    {"id": "PT-016", "name": "Thomas Robinson",   "unit": "MICU", "bed": "B08", "age": 72, "dx": "Post-op colectomy"},
    {"id": "PT-017", "name": "Nancy Hall",        "unit": "SICU", "bed": "S06", "age": 67, "dx": "Biliary sepsis suspect"},
    {"id": "PT-018", "name": "Charles Young",     "unit": "MICU", "bed": "B09", "age": 84, "dx": "Pneumonia"},
    # Critical sudden deterioration (beds 19-20)
    {"id": "PT-019", "name": "Dorothy Allen",     "unit": "MICU", "bed": "B10", "age": 58, "dx": "Post-liver transplant"},
    {"id": "PT-020", "name": "Paul King",         "unit": "SICU", "bed": "S07", "age": 76, "dx": "Ruptured AAA repair"},
]

STABLE_IDS   = {p["id"] for p in PATIENTS[:15]}
EARLY_SEP    = {p["id"] for p in PATIENTS[15:18]}
CRITICAL_IDS = {p["id"] for p in PATIENTS[18:]}


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def jitter(value: float, pct: float = 0.05) -> float:
    """Add small natural random jitter to a value (±pct)."""
    return value * (1.0 + random.uniform(-pct, pct))


def stable_vitals(patient: Dict[str, Any]) -> Dict[str, Any]:
    """Normal, healthy ICU vitals with natural variation."""
    return {
        "reading_id":       str(uuid.uuid4()),
        "patient_id":       patient["id"],
        "patient_name":     patient["name"],
        "unit":             patient["unit"],
        "bed_number":       patient["bed"],
        "age":              patient["age"],
        "heart_rate":       int(jitter(72, 0.15)),
        "systolic_bp":      int(jitter(118, 0.10)),
        "diastolic_bp":     int(jitter(76, 0.10)),
        "respiratory_rate": int(jitter(16, 0.12)),
        "spo2":             round(min(99.0, jitter(97.5, 0.01)), 1),
        "temperature_c":    round(jitter(37.0, 0.005), 1),
        "consciousness":    "Alert",
        "lactate_mmol":     round(jitter(1.1, 0.10), 2),
        "wbc_count":        round(jitter(8.5, 0.15), 1),
        "diagnosis":        patient["dx"],
        "measurement_ts":   now_ms(),
    }


def deteriorating_vitals(patient: Dict[str, Any], elapsed_minutes: float) -> Dict[str, Any]:
    """
    Gradual early sepsis deterioration.
    Over ~15 minutes, vitals slide from normal into the Sepsis-3 danger zone.
    """
    severity = min(1.0, elapsed_minutes / 15.0)

    hr   = int(72  + severity * 38)   # 72 → 110 bpm
    sbp  = int(118 - severity * 28)   # 118 → 90 mmHg
    dbp  = int(76  - severity * 16)   # 76 → 60 mmHg
    rr   = int(16  + severity * 10)   # 16 → 26 breaths/min
    spo2 = round(97.5 - severity * 5.5, 1)  # 97.5 → 92%
    temp = round(37.0 + severity * 1.5, 1)  # 37.0 → 38.5°C
    lac  = round(1.1  + severity * 2.4, 2)  # 1.1 → 3.5 mmol/L
    wbc  = round(8.5  + severity * 8.5, 1)  # 8.5 → 17.0 K/uL

    # Add noise
    hr   = int(jitter(hr,   0.08))
    sbp  = int(jitter(sbp,  0.06))
    rr   = int(jitter(rr,   0.08))
    spo2 = round(min(99.0, max(70.0, jitter(spo2, 0.01))), 1)
    temp = round(jitter(temp, 0.003), 1)

    if severity > 0.7:
        consciousness = random.choice(["Alert", "Alert", "Voice"])
    else:
        consciousness = "Alert"

    return {
        "reading_id":       str(uuid.uuid4()),
        "patient_id":       patient["id"],
        "patient_name":     patient["name"],
        "unit":             patient["unit"],
        "bed_number":       patient["bed"],
        "age":              patient["age"],
        "heart_rate":       hr,
        "systolic_bp":      sbp,
        "diastolic_bp":     dbp,
        "respiratory_rate": rr,
        "spo2":             spo2,
        "temperature_c":    temp,
        "consciousness":    consciousness,
        "lactate_mmol":     lac,
        "wbc_count":        wbc,
        "diagnosis":        patient["dx"],
        "measurement_ts":   now_ms(),
    }


def critical_vitals(patient: Dict[str, Any]) -> Dict[str, Any]:
    """Sudden critical decompensation — septic shock."""
    return {
        "reading_id":       str(uuid.uuid4()),
        "patient_id":       patient["id"],
        "patient_name":     patient["name"],
        "unit":             patient["unit"],
        "bed_number":       patient["bed"],
        "age":              patient["age"],
        "heart_rate":       int(jitter(128, 0.10)),
        "systolic_bp":      int(jitter(78, 0.08)),
        "diastolic_bp":     int(jitter(48, 0.08)),
        "respiratory_rate": int(jitter(30, 0.10)),
        "spo2":             round(max(70.0, jitter(88.0, 0.02)), 1),
        "temperature_c":    round(jitter(38.9, 0.005), 1),
        "consciousness":    random.choice(["Voice", "Pain", "Pain"]),
        "lactate_mmol":     round(jitter(4.5, 0.12), 2),
        "wbc_count":        round(jitter(22.0, 0.15), 1),
        "diagnosis":        patient["dx"],
        "measurement_ts":   now_ms(),
    }


class SepsisShieldGenerator:
    """Streams patient vital sign events to Kafka."""

    def __init__(
        self,
        bootstrap_servers: str,
        kafka_api_key: str,
        kafka_api_secret: str,
        schema_registry_url: str,
        schema_registry_api_key: str,
        schema_registry_api_secret: str,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        self.producer: Optional[SerializingProducer] = None

        if not dry_run:
            sr_conf = {
                "url": schema_registry_url,
                "basic.auth.user.info": f"{schema_registry_api_key}:{schema_registry_api_secret}",
            }
            sr_client = SchemaRegistryClient(sr_conf)

            def to_avro(event: Dict[str, Any], _ctx):
                return event

            avro_serializer = AvroSerializer(sr_client, AVRO_SCHEMA, to_avro)
            string_serializer = StringSerializer("utf_8")

            self.producer = SerializingProducer({
                "bootstrap.servers":  bootstrap_servers,
                "sasl.mechanisms":    "PLAIN",
                "security.protocol":  "SASL_SSL",
                "sasl.username":      kafka_api_key,
                "sasl.password":      kafka_api_secret,
                "linger.ms":          5,
                "batch.size":         16384,
                "compression.type":   "snappy",
                "key.serializer":     string_serializer,
                "value.serializer":   avro_serializer,
            })

    def _delivery_cb(self, err, msg):
        if err:
            self.logger.error(f"Delivery failed: {err}")

    def publish(self, event: Dict[str, Any]) -> bool:
        try:
            if self.dry_run:
                self.logger.debug(
                    f"[DRY RUN] {event['patient_id']} HR={event['heart_rate']} "
                    f"SpO2={event['spo2']} RR={event['respiratory_rate']}"
                )
                return True
            self.producer.produce(
                topic=TOPIC_NAME,
                key=event["patient_id"],
                value=event,
                on_delivery=self._delivery_cb,
            )
            return True
        except Exception as e:
            self.logger.error(f"Publish error: {e}")
            return False

    def flush(self):
        if self.producer:
            self.producer.flush()

    def run(
        self,
        warm_up_seconds: int = 360,
        crisis_duration_seconds: int = 900,
        readings_interval_seconds: float = 15.0,
        crisis_now: bool = False,
    ) -> None:
        """
        Stream vital signs continuously.

        Phase 1 (warm-up):  All 20 patients stable — builds ML baseline.
        Phase 2 (crisis):   15 stable + 3 early sepsis + 2 sudden critical.
        """
        total_sent = 0

        def log_status(phase: str):
            self.logger.info(
                f"[{phase}] {total_sent} readings sent — "
                f"{total_sent // len(PATIENTS)} full rounds"
            )

        # ── Phase 1: Warm-up (all stable) ────────────────────────────────────
        if not crisis_now:
            print(f"\n{'=' * 65}")
            print("PHASE 1 — BASELINE: ALL 20 PATIENTS STABLE")
            print(f"{'=' * 65}")
            print(f"Streaming vitals every {readings_interval_seconds:.0f}s for {warm_up_seconds}s")
            print("ML_DETECT_ANOMALIES needs this window to learn normal patterns.\n")

            phase_end = time.time() + warm_up_seconds
            while time.time() < phase_end:
                for patient in PATIENTS:
                    self.publish(stable_vitals(patient))
                    total_sent += 1
                if total_sent % (len(PATIENTS) * 5) == 0:
                    log_status("BASELINE")
                    if self.producer:
                        self.producer.poll(0)
                time.sleep(readings_interval_seconds)

            self.flush()

        # ── Phase 2: Crisis injection ─────────────────────────────────────────
        print(f"\n{'=' * 65}")
        print("PHASE 2 — CRISIS: SEPSIS EVENTS BEGINNING")
        print(f"{'=' * 65}")
        print("3 patients deteriorating (early sepsis — gradual):")
        print("  PT-016 Thomas Robinson   MICU B08")
        print("  PT-017 Nancy Hall        SICU S06")
        print("  PT-018 Charles Young     MICU B09")
        print("\n2 patients in sudden critical decompensation:")
        print("  PT-019 Dorothy Allen     MICU B10")
        print("  PT-020 Paul King         SICU S07\n")

        crisis_start = time.time()
        phase_end = crisis_start + crisis_duration_seconds
        crisis_patients = {p["id"]: p for p in PATIENTS if p["id"] in EARLY_SEP | CRITICAL_IDS}

        while time.time() < phase_end:
            elapsed_minutes = (time.time() - crisis_start) / 60.0

            for patient in PATIENTS:
                pid = patient["id"]
                if pid in STABLE_IDS:
                    event = stable_vitals(patient)
                elif pid in EARLY_SEP:
                    event = deteriorating_vitals(patient, elapsed_minutes)
                else:
                    event = critical_vitals(patient)

                self.publish(event)
                total_sent += 1

            if total_sent % (len(PATIENTS) * 5) == 0:
                log_status("CRISIS")
                if self.producer:
                    self.producer.poll(0)

            time.sleep(readings_interval_seconds)

        self.flush()

        print(f"\n{'=' * 65}")
        print("SEPSISSHIELD DATA GENERATION COMPLETE")
        print(f"{'=' * 65}")
        print(f"Total readings published: {total_sent:,}")
        print(f"Topic:                    {TOPIC_NAME}")
        print(f"\nNow run the Flink SQL queries in SEPSISSHIELD-Walkthrough.md")
        print(f"{'=' * 65}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sepsisshield_datagen",
        description="Stream ICU patient vital signs to Kafka for SepsisShield demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run sepsisshield_datagen                    # Auto-detect cloud provider
  uv run sepsisshield_datagen aws                # Target AWS environment
  uv run sepsisshield_datagen --crisis-now       # Skip warm-up, inject crisis immediately
  uv run sepsisshield_datagen --duration 900     # Run crisis phase for 15 minutes
  uv run sepsisshield_datagen --dry-run          # Validate setup without publishing
        """.strip(),
    )

    parser.add_argument(
        "cloud_provider", nargs="?", choices=["aws", "azure"],
        help="Target cloud provider (auto-detected if not specified)",
    )
    parser.add_argument(
        "--warm-up", type=int, default=360,
        help="Baseline warm-up in seconds before crisis injection (default: 360)",
    )
    parser.add_argument(
        "--duration", type=int, default=900,
        help="Crisis phase duration in seconds (default: 900)",
    )
    parser.add_argument(
        "--interval", type=float, default=15.0,
        help="Seconds between vital sign rounds (default: 15 — one reading per patient per 15s)",
    )
    parser.add_argument(
        "--crisis-now", action="store_true",
        help="Skip warm-up and begin crisis immediately (useful for quick demos)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate setup without publishing any messages",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()
    logger = setup_logging(args.verbose)
    logger.info("SepsisShield — ICU Patient Deterioration Data Generator")

    if not CONFLUENT_KAFKA_AVAILABLE:
        logger.error("confluent-kafka not installed. Run: uv pip install confluent-kafka")
        sys.exit(1)

    try:
        project_root = get_project_root()
    except Exception as e:
        logger.error(f"Could not find project root: {e}")
        sys.exit(1)

    cloud_provider = args.cloud_provider
    if not cloud_provider:
        cloud_provider = auto_detect_cloud_provider()
        if not cloud_provider:
            suggest_cloud_provider(project_root)
            sys.exit(1)

    logger.info(f"Target cloud provider: {cloud_provider.upper()}")

    if not validate_terraform_state(cloud_provider, project_root):
        logger.error("Terraform state validation failed. Run 'uv run deploy' first.")
        sys.exit(1)

    try:
        creds = extract_kafka_credentials(cloud_provider, project_root)
    except Exception as e:
        logger.error(f"Failed to extract Kafka credentials: {e}")
        sys.exit(1)

    print(f"\n{'=' * 65}")
    print("SEPSISSHIELD — Real-Time ICU Sepsis Detection & Response")
    print(f"{'=' * 65}")
    print(f"Environment:   {creds.get('environment_name', 'unknown')}")
    print(f"Cluster:       {creds.get('cluster_name', 'unknown')}")
    print(f"Topic:         {TOPIC_NAME}")
    print(f"ICU Patients:  {len(PATIENTS)} (15 stable / 3 early sepsis / 2 critical)")
    if args.crisis_now:
        print(f"Mode:          CRISIS IMMEDIATE (no warm-up)")
    else:
        print(f"Warm-up:       {args.warm_up}s, then {args.duration}s crisis phase")
    print(f"{'=' * 65}\n")

    if args.dry_run:
        logger.info("[DRY RUN] Setup validated. No messages will be published.")
        print("✓ Dry run complete — setup is valid.")
        sys.exit(0)

    try:
        generator = SepsisShieldGenerator(
            bootstrap_servers=creds["bootstrap_servers"],
            kafka_api_key=creds["kafka_api_key"],
            kafka_api_secret=creds["kafka_api_secret"],
            schema_registry_url=creds["schema_registry_url"],
            schema_registry_api_key=creds["schema_registry_api_key"],
            schema_registry_api_secret=creds["schema_registry_api_secret"],
            dry_run=args.dry_run,
        )
        generator.run(
            warm_up_seconds=args.warm_up,
            crisis_duration_seconds=args.duration,
            readings_interval_seconds=args.interval,
            crisis_now=args.crisis_now,
        )
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Data generation failed: {e}")
        if args.verbose:
            import traceback
            logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
