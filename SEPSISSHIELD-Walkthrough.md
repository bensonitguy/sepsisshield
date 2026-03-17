# SepsisShield: Real-Time ICU Sepsis Detection & Autonomous Clinical Response

![SepsisShield Architecture](./assets/sepsisshield/architecture.png)

Sepsis kills **270,000 Americans per year** — and is treatable if caught within the first hour. Nurses monitoring 5–8 ICU patients simultaneously can miss the subtle early signs. **SepsisShield** watches every patient continuously and acts the moment vital signs begin to deteriorate.

Built on **Confluent Cloud for Apache Flink**, SepsisShield chains together:

1. **Anomaly Detection** — `ML_DETECT_ANOMALIES` watches heart rate, SpO2, and respiratory rate for each patient across 5-minute windows, catching statistical deviations before they become clinical emergencies.
2. **Contextual Understanding** — Vector search over Sepsis-3 guidelines, NEWS2 scoring, Hour-1 Bundle, and vasopressor protocols surfaces the exact clinical protocol relevant to each patient's deterioration pattern.
3. **Autonomous Clinical Action** — A streaming agent calculates the qSOFA score, determines severity, identifies the appropriate protocol, and pages the right care team — all within seconds of the vital sign change.

---

## Architecture

```
patient_vitals (Kafka)
      │
      ├── ML_DETECT_ANOMALIES per patient        → vitals_anomalies
      │        (5-min windows, HR / SpO2 / RR)
      │
      ├── VECTOR_SEARCH_AGG over clinical_docs   → anomalies_enriched
      │        (Sepsis-3, NEWS2, Hour-1 Bundle)
      │
      └── AI_RUN_AGENT (sepsis_response_agent)   → clinical_alerts
               ├── Computes qSOFA score
               ├── Classifies severity (WATCH / CONCERN / SEPSIS / SHOCK)
               ├── Retrieves protocol steps
               └── Pages care team via Zapier MCP (email + webhook)
```

---

## Prerequisites

- Lab3 already deployed (`uv run deploy` → Lab3)
- The `zapier-mcp-connection`, `llm_textgen_model`, and `llm_embedding_model` already exist in your environment

---

## Step 0 — Publish Clinical Knowledge Base

The clinical knowledge base (Sepsis-3, NEWS2, Hour-1 Bundle, vasopressor protocols) needs to be published to Kafka, embedded, and stored in the vector database. This reuses the existing Lab3 MongoDB vector DB infrastructure.

```bash
uv run publish_docs --docs-dir assets/sepsisshield/clinical_docs --topic documents
```

> This publishes 4 clinical protocol documents. They will be embedded by the same pipeline that powers Lab3's vector search. The MITRE documents are semantically distinct enough that they will not appear in Lab3 results.

---

## Step 1 — Start Data Generation

Run the vital signs generator. It streams readings every 15 seconds for all 20 ICU patients:

```bash
# Full run: 6-minute warm-up, then 15-minute crisis phase
uv run sepsisshield_datagen

# For quick demos: skip warm-up, inject crisis immediately
uv run sepsisshield_datagen --crisis-now
```

**What the data generator produces:**

| Patient Group | Beds | Count | Pattern |
|---|---|---|---|
| Stable | B01–B07, S01–S05, N01–N03 | 15 | Normal vitals, natural variation |
| Early Sepsis | B08–B09, S06 | 3 | Gradual deterioration over 15 min |
| Septic Shock | B10, S07 | 2 | Sudden critical decompensation |

---

## Step 2 — Set Up the `patient_vitals` Flink Table

Once the data generator is running, Confluent Cloud Flink **automatically discovers** the `patient_vitals` table from the Kafka topic and Schema Registry — no `CREATE TABLE` needed.

Verify the table is discoverable and data is flowing:

```sql
SELECT patient_id, patient_name, heart_rate, spo2, respiratory_rate, temperature_c
FROM patient_vitals
LIMIT 20;
```

Then add the watermark so Flink can use `measurement_ts` as an event-time attribute for windowed queries:

```sql
ALTER TABLE patient_vitals
MODIFY (WATERMARK FOR measurement_ts AS measurement_ts - INTERVAL '5' SECOND);
```

> If you see `The window function requires the timecol is a time attribute type` later, this ALTER TABLE is the fix — it teaches Flink to treat `measurement_ts` as a proper watermarked event-time column.

---

## Step 3 — Visualize Baseline vs Deteriorating Patients

Before running anomaly detection, confirm the data patterns. Run this in a separate cell — you should see the stable patients holding steady while the deteriorating patients creep upward.

**Heart rate trend by patient (5-minute averages):**

```sql
SELECT
    patient_id,
    patient_name,
    window_start,
    ROUND(AVG(heart_rate), 1)       AS avg_hr,
    ROUND(AVG(spo2), 1)             AS avg_spo2,
    ROUND(AVG(respiratory_rate), 1) AS avg_rr,
    ROUND(AVG(temperature_c), 1)    AS avg_temp,
    COUNT(*)                        AS readings
FROM TABLE(
    TUMBLE(TABLE patient_vitals, DESCRIPTOR(measurement_ts), INTERVAL '5' MINUTE)
)
GROUP BY patient_id, patient_name, window_start, window_end, window_time;
```

Click **Switch to Time Series** and group by `patient_id` — you will see PT-016, PT-017, PT-018 rising while the other 17 patients remain flat.

---

## Step 4 — Anomaly Detection with `ML_DETECT_ANOMALIES`

> **Why three sub-steps?** Flink's `ML_DETECT_ANOMALIES OVER (ORDER BY window_time)` requires `window_time` to be a declared event-time attribute. When `window_time` travels through CTEs it loses that metadata. The fix is to materialize the windowed aggregations as a real Kafka-backed table, add a watermark to it explicitly, then run anomaly detection on top — giving Flink a solid time attribute to sort by.

### 4a. Materialize 5-minute windowed aggregations

This creates a persistent table of per-patient per-window averages. **Run once and leave running.**

```sql
CREATE TABLE patient_vitals_windowed
WITH ('changelog.mode' = 'append')
AS SELECT
    window_start,
    window_end,
    window_time,
    patient_id,
    patient_name,
    unit,
    bed_number,
    age,
    diagnosis,
    consciousness,
    ROUND(AVG(heart_rate), 1)       AS avg_hr,
    ROUND(AVG(spo2), 1)             AS avg_spo2,
    ROUND(AVG(respiratory_rate), 1) AS avg_rr,
    ROUND(AVG(temperature_c), 1)    AS avg_temp,
    ROUND(AVG(lactate_mmol), 2)     AS avg_lactate,
    ROUND(MIN(systolic_bp), 0)      AS min_sbp,
    COUNT(*)                        AS readings_in_window
FROM TABLE(
    TUMBLE(TABLE patient_vitals, DESCRIPTOR(measurement_ts), INTERVAL '5' MINUTE)
)
GROUP BY window_start, window_end, window_time, patient_id, patient_name,
         unit, bed_number, age, diagnosis, consciousness;
```

### 4b. Declare the event-time watermark

This tells Flink that `window_time` is a proper time attribute — required before `ML_DETECT_ANOMALIES` can use it in an OVER clause.

```sql
ALTER TABLE patient_vitals_windowed
MODIFY (WATERMARK FOR window_time AS window_time - INTERVAL '0' SECOND);
```

### 4c. Visualize anomalies (interactive check)

Verify the watermark is working and anomalies are firing before creating the persistent table:

```sql
SELECT
    patient_id,
    patient_name,
    window_time,
    avg_hr,
    avg_rr,
    ML_DETECT_ANOMALIES(
        CAST(avg_hr AS DOUBLE),
        window_time,
        JSON_OBJECT(
            'minTrainingSize' VALUE 3,
            'maxTrainingSize' VALUE 100,
            'confidencePercentage' VALUE 95.0,
            'enableStl' VALUE FALSE
        )
    ) OVER (
        PARTITION BY patient_id
        ORDER BY window_time
        RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS hr_anomaly
FROM patient_vitals_windowed;
```

Click the **hr_anomaly** column graph to see statistical upper bounds being breached by PT-016, PT-017, PT-018.

### 4d. Create the continuous `vitals_anomalies` table

**Run once and leave running** — it continuously detects and emits anomalies as new windows close.

The anomaly expressions are computed in an inner subquery first, then filtered in the outer `WHERE` clause — this is required because Flink does not allow windowed aggregate expressions directly in `WHERE`.

```sql
CREATE TABLE vitals_anomalies
WITH ('changelog.mode' = 'append')
AS SELECT
    patient_id,
    patient_name,
    unit,
    bed_number,
    age,
    diagnosis,
    window_time,
    avg_hr,
    avg_spo2,
    avg_rr,
    avg_temp,
    avg_lactate,
    min_sbp,
    consciousness,
    expected_hr,
    hr_is_anomaly,
    rr_is_anomaly
FROM (
    SELECT
        patient_id,
        patient_name,
        unit,
        bed_number,
        age,
        diagnosis,
        window_time,
        avg_hr,
        avg_spo2,
        avg_rr,
        avg_temp,
        avg_lactate,
        min_sbp,
        consciousness,
        CAST(ROUND(
            ML_DETECT_ANOMALIES(
                CAST(avg_hr AS DOUBLE),
                window_time,
                JSON_OBJECT(
                    'minTrainingSize' VALUE 3,
                    'maxTrainingSize' VALUE 100,
                    'confidencePercentage' VALUE 95.0,
                    'enableStl' VALUE FALSE
                )
            ) OVER (
                PARTITION BY patient_id
                ORDER BY window_time
                RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ).forecast_value, 1) AS DOUBLE)     AS expected_hr,
        ML_DETECT_ANOMALIES(
            CAST(avg_hr AS DOUBLE),
            window_time,
            JSON_OBJECT(
                'minTrainingSize' VALUE 3,
                'maxTrainingSize' VALUE 100,
                'confidencePercentage' VALUE 95.0,
                'enableStl' VALUE FALSE
            )
        ) OVER (
            PARTITION BY patient_id
            ORDER BY window_time
            RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ).is_anomaly                             AS hr_is_anomaly,
        ML_DETECT_ANOMALIES(
            CAST(avg_rr AS DOUBLE),
            window_time,
            JSON_OBJECT(
                'minTrainingSize' VALUE 3,
                'maxTrainingSize' VALUE 100,
                'confidencePercentage' VALUE 95.0,
                'enableStl' VALUE FALSE
            )
        ) OVER (
            PARTITION BY patient_id
            ORDER BY window_time
            RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ).is_anomaly                             AS rr_is_anomaly
    FROM patient_vitals_windowed
) t
WHERE hr_is_anomaly = TRUE
   OR rr_is_anomaly = TRUE
   OR avg_spo2 < 93.0
   OR min_sbp < 95;
```

View the anomalies in a new cell:

```sql
SELECT * FROM vitals_anomalies;
```

> **Timing:** Results appear after the **first 5-minute tumbling window closes**. PT-019 and PT-020 (critical — SpO2 ~88%, SBP ~78) will appear first via the hard-coded thresholds (`avg_spo2 < 93` / `min_sbp < 95`). PT-016, PT-017, PT-018 (early sepsis) require `minTrainingSize=3` windows (~15 min) before `ML_DETECT_ANOMALIES` fires. If you used `--crisis-now`, wait at least 5–6 minutes from when you started the data generator.

---

## Step 5 — Enrich with Clinical Protocol via Vector Search

Now that we know **which patients** are deteriorating, we need to know **what to do**. This step embeds each anomaly as a query, searches the clinical knowledge base for the most relevant protocols, then uses the LLM to summarize the finding.

First, add a watermark to `vitals_anomalies` so Flink can process it as an event-time stream:

```sql
ALTER TABLE vitals_anomalies
MODIFY (WATERMARK FOR window_time AS window_time - INTERVAL '0' SECOND);
```

> If you previously ran this step and got a schema conflict error, drop the old table first:
> ```sql
> DROP TABLE IF EXISTS anomalies_enriched;
> ```

Leave this running continuously:

```sql
CREATE TABLE anomalies_enriched
WITH ('changelog.mode' = 'append')
AS SELECT
    a_emb.patient_id,
    a_emb.patient_name,
    a_emb.unit,
    a_emb.bed_number,
    a_emb.age,
    a_emb.diagnosis,
    a_emb.window_time,
    a_emb.avg_hr,
    a_emb.avg_spo2,
    a_emb.avg_rr,
    a_emb.avg_temp,
    a_emb.avg_lactate,
    a_emb.min_sbp,
    a_emb.consciousness,
    a_emb.expected_hr,
    a_emb.hr_is_anomaly,
    a_emb.rr_is_anomaly,
    TRIM(llm_summary.response)       AS clinical_summary,
    vs.search_results[1].chunk       AS protocol_chunk_1,
    vs.search_results[1].document_id AS protocol_source_1,
    vs.search_results[2].chunk       AS protocol_chunk_2,
    vs.search_results[2].document_id AS protocol_source_2,
    vs.search_results[3].chunk       AS protocol_chunk_3,
    vs.search_results[3].document_id AS protocol_source_3
FROM (
    SELECT
        a.*,
        emb.embedding AS query_embedding
    FROM vitals_anomalies a,
    LATERAL TABLE(ML_PREDICT('llm_embedding_model',
        CONCAT(
            'ICU patient deterioration: ',
            a.patient_name, ', age ', CAST(a.age AS STRING),
            ', diagnosis: ', a.diagnosis,
            '. Vitals: HR=', CAST(a.avg_hr AS STRING),
            ' RR=', CAST(a.avg_rr AS STRING),
            ' SpO2=', CAST(a.avg_spo2 AS STRING),
            ' SBP=', CAST(a.min_sbp AS STRING),
            ' Temp=', CAST(a.avg_temp AS STRING),
            ' Lactate=', CAST(a.avg_lactate AS STRING),
            ' Consciousness=', a.consciousness,
            '. What sepsis protocol and interventions apply?'
        )
    )) AS emb
) AS a_emb,
LATERAL TABLE(
    VECTOR_SEARCH_AGG(
        documents_vectordb_lab3,
        DESCRIPTOR(embedding),
        a_emb.query_embedding,
        3
    )
) AS vs,
LATERAL TABLE(
    ML_PREDICT(
        'llm_textgen_model',
        CONCAT(
            'Summarize the key clinical concern for this deteriorating ICU patient in 1-2 sentences, ',
            'naming the most likely diagnosis and urgency level.\n\n',
            'Patient: ', a_emb.patient_name,
            ', Age: ', CAST(a_emb.age AS STRING),
            ', Dx: ', a_emb.diagnosis, '\n',
            'HR: ', CAST(a_emb.avg_hr AS STRING), ' bpm',
            ', SpO2: ', CAST(a_emb.avg_spo2 AS STRING), '%',
            ', RR: ', CAST(a_emb.avg_rr AS STRING), '/min',
            ', SBP: ', CAST(a_emb.min_sbp AS STRING), ' mmHg',
            ', Temp: ', CAST(a_emb.avg_temp AS STRING), 'C',
            ', Lactate: ', CAST(a_emb.avg_lactate AS STRING), ' mmol/L',
            ', Consciousness: ', a_emb.consciousness
        )
    )
) AS llm_summary;
```

---

## Step 6 — Define the Sepsis Response Agent

> **Note on MCP:** If your Zapier MCP connection is not active or times out (error: `Failed to initialize MCP client: TimeoutException`), use the version below which runs on `llm_textgen_model` directly. The agent still performs full qSOFA scoring, severity classification, and structured alert output — it just won't push to Zapier webhooks.

First drop any previous version:

```sql
DROP AGENT IF EXISTS `sepsis_response_agent`;
```

Then create the agent:

```sql
CREATE AGENT `sepsis_response_agent`
USING MODEL `llm_textgen_model`
USING PROMPT 'OUTPUT RULES — read before anything else:
1. Respond with ONLY these four labeled sections in this exact order:
   Severity:
   qSOFA Score:
   Immediate Actions:
   Alert Message:
2. Plain text only. No markdown, no asterisks, no bold.
3. Severity must be exactly one of: WATCH, CONCERN, SEPSIS, SEPTIC_SHOCK.
4. qSOFA Score must be a number 0-3 with brief explanation.

Correct format example:
Severity: SEPSIS

qSOFA Score: 2 (RR>=22: YES, SBP<=100: YES, Altered mentation: NO)

Immediate Actions:
- Draw stat lactate and blood cultures x2 NOW
- Start broad-spectrum antibiotics within 45 minutes
- IV fluid bolus 500mL normal saline immediately
- Page attending physician STAT
- Continuous monitoring — reassess in 30 minutes

Alert Message:
SEPSIS ALERT — PT-016 Thomas Robinson, MICU B08, Post-op colectomy.
HR 108, RR 23, SBP 93, SpO2 94%, Temp 38.4C, Lactate 2.1.
qSOFA 2. Hour-1 Bundle initiated. Attending paged.

---

You are a clinical decision support agent for an ICU monitoring system.

Using the Sepsis-3 definition, NEWS2 scoring, and Hour-1 Bundle guidelines provided,
evaluate the deteriorating patient and provide a structured clinical response.

SEVERITY CLASSIFICATION:
- WATCH:       NEWS2-equivalent score 1-4. Increase monitoring to every 1 hour.
- CONCERN:     qSOFA=1 OR NEWS2 5-6 OR single vital sign red flag. Senior nurse review.
- SEPSIS:      qSOFA>=2 OR SOFA increase >=2. Activate Hour-1 Bundle NOW. Page attending.
- SEPTIC_SHOCK: SBP<90 despite fluids OR lactate>=4 mmol/L. Emergency response. ICU team NOW.

qSOFA CRITERIA (1 point each — compute from actual vitals):
- Respiratory Rate >= 22 breaths/min
- Systolic BP <= 100 mmHg
- Altered mentation (consciousness != Alert)

IMMEDIATE ACTIONS by severity:
- WATCH:        Increase vital sign frequency; notify bedside nurse.
- CONCERN:      Urgent senior nurse + physician review; draw lactate and CBC stat.
- SEPSIS:       Hour-1 Bundle: lactate, blood cultures x2, broad-spectrum antibiotics within 45 min,
                30 mL/kg IV fluid bolus, vasopressors if MAP<65, page attending STAT.
- SEPTIC_SHOCK: Emergency response: call code team, initiate vasopressors NOW (norepinephrine),
                activate rapid response, prepare for ICU escalation, notify charge nurse and attending.

Always end with a concise one-line Alert Message summarizing the patient, severity, and top action.'
WITH (
  'max_iterations' = '5'
);
```

---

## Step 7 — Run the Agent on All Anomalies

This final query wires everything together. For every enriched anomaly, the agent receives the full patient context + clinical protocols, reasons through the severity, and acts — all in real time.

```sql
CREATE TABLE clinical_alerts (
    PRIMARY KEY (patient_id) NOT ENFORCED
)
WITH ('changelog.mode' = 'append')
AS SELECT
    patient_id,
    patient_name,
    unit,
    bed_number,
    diagnosis,
    window_time,
    avg_hr,
    avg_spo2,
    avg_rr,
    min_sbp,
    avg_lactate,
    clinical_summary,
    TRIM(REGEXP_EXTRACT(CAST(response AS STRING), 'Severity:\s*([A-Z_]+)', 1))                                            AS severity,
    TRIM(REGEXP_EXTRACT(CAST(response AS STRING), 'qSOFA Score:\s*(\d[^\n]*)', 1))                                        AS qsofa_score,
    TRIM(REGEXP_EXTRACT(CAST(response AS STRING), 'Immediate Actions:\n([\s\S]+?)(?=\n\nAlert Message:)', 1))              AS immediate_actions,
    TRIM(REGEXP_EXTRACT(CAST(response AS STRING), 'Alert Message:\n([\s\S]+?)$', 1))                                      AS alert_message
FROM anomalies_enriched,
LATERAL TABLE(AI_RUN_AGENT(
    `sepsis_response_agent`,
    CONCAT(
        'PATIENT REQUIRING CLINICAL ASSESSMENT\n',
        '=====================================\n',
        'Patient ID:    ', COALESCE(patient_id, 'unknown'), '\n',
        'Name:          ', COALESCE(patient_name, 'unknown'), '\n',
        'Age:           ', CAST(age AS STRING), ' years\n',
        'Unit / Bed:    ', COALESCE(unit, ''), ' / ', COALESCE(bed_number, ''), '\n',
        'Diagnosis:     ', COALESCE(diagnosis, 'unknown'), '\n',
        '\nCURRENT VITALS (5-min window ending ', CAST(window_time AS STRING), '):\n',
        'Heart Rate:         ', CAST(avg_hr AS STRING), ' bpm  (expected baseline: ', COALESCE(CAST(expected_hr AS STRING), 'N/A'), ')\n',
        'Systolic BP:        ', CAST(min_sbp AS STRING), ' mmHg\n',
        'Respiratory Rate:   ', CAST(avg_rr AS STRING), ' breaths/min\n',
        'SpO2:               ', CAST(avg_spo2 AS STRING), '%\n',
        'Temperature:        ', CAST(avg_temp AS STRING), ' C\n',
        'Lactate:            ', CAST(avg_lactate AS STRING), ' mmol/L\n',
        'Consciousness:      ', COALESCE(consciousness, 'unknown'), '\n',
        '\nANOMALY FLAGS:\n',
        '- Heart Rate anomaly:  ', COALESCE(CAST(hr_is_anomaly AS STRING), 'false'), '\n',
        '- Resp Rate anomaly:   ', COALESCE(CAST(rr_is_anomaly AS STRING), 'false'), '\n',
        '\nCLINICAL SUMMARY FROM AI:\n', COALESCE(clinical_summary, 'No summary available.'), '\n',
        '\nRETRIEVED CLINICAL PROTOCOLS:\n',
        'Protocol 1 (', COALESCE(protocol_source_1, 'N/A'), '):\n', COALESCE(protocol_chunk_1, ''), '\n\n',
        'Protocol 2 (', COALESCE(protocol_source_2, 'N/A'), '):\n', COALESCE(protocol_chunk_2, ''), '\n\n',
        'Protocol 3 (', COALESCE(protocol_source_3, 'N/A'), '):\n', COALESCE(protocol_chunk_3, '')
    )
));
```

View results as they stream in:

```sql
SELECT
    patient_name,
    bed_number,
    severity,
    qsofa_score,
    avg_hr,
    avg_spo2,
    avg_rr,
    min_sbp,
    avg_lactate,
    immediate_actions,
    alert_message
FROM clinical_alerts
ORDER BY window_time DESC;
```

---

## Expected Output

| Patient | Bed | Severity | qSOFA | Key Finding |
|---|---|---|---|---|
| Thomas Robinson | B08 | SEPSIS | 2 | HR 110, RR 26, SBP 92 — Hour-1 Bundle triggered |
| Nancy Hall | S06 | CONCERN | 1 | RR 24, Temp 38.5°C — Senior review requested |
| Charles Young | B09 | SEPSIS | 2 | HR 108, SpO2 93%, Lactate 2.8 |
| Dorothy Allen | B10 | SEPTIC_SHOCK | 3 | HR 128, SBP 78, Lactate 4.5 — Emergency response |
| Paul King | S07 | SEPTIC_SHOCK | 3 | HR 130, SBP 76, SpO2 88% — Code team alerted |

---

## Conclusion

SepsisShield demonstrates a fully autonomous, always-on clinical decision support pipeline that:

1. **Continuously monitors** all ICU patients at the vital-sign level in real time
2. **Detects deviations** statistically before they cross hard-coded thresholds using ML
3. **Grounds its reasoning** in peer-reviewed clinical guidelines (Sepsis-3, NEWS2, Hour-1 Bundle) via vector search
4. **Acts autonomously** — pages the right team, sends structured alerts, and documents the response

At scale, a system like this could save thousands of lives per year by closing the gap between early physiological warning signs and clinical action.

---

## Troubleshooting

<details>
<summary>Click to expand</summary>

**No anomalies detected?**
- Ensure data generation is running (`uv run sepsisshield_datagen`)
- If using `--crisis-now`, anomalies appear in the first 5-minute window
- Decrease `confidencePercentage` to 90.0 if anomalies are still not appearing
- Check `SELECT COUNT(*) FROM patient_vitals` — should be growing

**`vitals_anomalies` returns 0 rows?**
- Wait for the first 5-minute tumbling window to close — Flink batches by window
- `ML_DETECT_ANOMALIES` needs `minTrainingSize=3` windows (15 minutes) before detecting

**403 error on LLM calls?**
- Your Bedrock credentials in the connection have expired — see [refresh instructions](./LAB3-Walkthrough.md#troubleshooting)

**Vector search returns no results?**
- Confirm the clinical docs were published: `uv run publish_docs --docs-dir assets/sepsisshield/clinical_docs --topic documents`
- Check that the Lab3 document embedding pipeline is running

</details>

---

## Clean-up

When done, run:

```bash
uv run destroy
```

---

## Navigation

- **← Back to Overview**: [Main README](./README.md)
- **← Lab3**: [Agentic Fleet Management](./LAB3-Walkthrough.md)
