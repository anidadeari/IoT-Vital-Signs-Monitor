from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import json
import requests
import asyncio
import os
import re
import time
from pathlib import Path
from datetime import datetime

try:
    import numpy as np
except Exception:
    np = None

try:
    from analysis.compare_datasets import validate_latest_reading
    DATASETS_OK = True
except Exception as e:
    DATASETS_OK = False
    print(f"[WARNING] Dataset module not loaded: {e}")

    def validate_latest_reading():
        return {
            "status": "error",
            "message": "Dataset module unavailable. Check analysis/compare_datasets.py and dataset files."
        }


# ==========================================================
# APP CONFIG
# ==========================================================
app = FastAPI(title="IoT Vital Signs Monitoring System")


@app.middleware("http")
async def disable_live_api_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path == "/dashboard":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_esp32_requests(request: Request, call_next):
    """Make every ESP32 delivery attempt visible, including validation errors."""
    if request.url.path != "/api/data":
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    try:
        response = await call_next(request)
        print(
            f"[ESP32] {request.method} /api/data from {client_ip} "
            f"-> HTTP {response.status_code}",
            flush=True,
        )
        return response
    except Exception as exc:
        print(
            f"[ESP32] {request.method} /api/data from {client_ip} "
            f"-> ERROR {exc}",
            flush=True,
        )
        raise

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("VITALS_DB_PATH", str(BASE_DIR / "vitals.db")))
DASHBOARD_PATH = BASE_DIR / "dashboard.html"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# IMPORTANT:
# Mos e shkruaj API key direkt në kod.
# Në terminal përdor:
# Windows PowerShell:
#   $env:GROQ_API_KEY="your_key_here"
# CMD:
#   set GROQ_API_KEY=your_key_here
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "" if os.getenv("RENDER") else "http://localhost:11434",
).rstrip("/")

connected_clients = []
LIVE_READING_TIMEOUT_SECONDS = int(
    os.getenv("LIVE_READING_TIMEOUT_SECONDS", "25")
)


def with_live_status(row, stream_event="snapshot"):
    """Attach freshness metadata so stored data is not presented as live data."""
    if not row:
        return row

    payload = dict(row)
    try:
        reading_time = datetime.strptime(
            payload["timestamp"],
            "%Y-%m-%d %H:%M:%S",
        )
        age_seconds = max(
            0,
            int((datetime.now() - reading_time).total_seconds()),
        )
    except (KeyError, TypeError, ValueError):
        age_seconds = LIVE_READING_TIMEOUT_SECONDS + 1

    payload["data_age_seconds"] = age_seconds
    payload["is_live"] = age_seconds <= LIVE_READING_TIMEOUT_SECONDS
    payload["stream_event"] = stream_event
    return payload


async def broadcast_sensor_data(message):
    """Broadcast without ever delaying the ESP32 HTTP response."""
    for client in connected_clients[:]:
        try:
            await asyncio.wait_for(client.send_text(message), timeout=0.5)
        except Exception:
            if client in connected_clients:
                connected_clients.remove(client)


# ==========================================================
# JSON SAFE HELPER
# ==========================================================
def make_json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): make_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]

    if isinstance(obj, tuple):
        return [make_json_safe(v) for v in obj]

    if np is not None:
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()

    return obj


# ==========================================================
# DATABASE
# ==========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                heart_rate INTEGER DEFAULT 0,
                spo2 INTEGER DEFAULT 0,
                spo2_status TEXT DEFAULT '---',
                temperature REAL DEFAULT 0,
                tremor_amplitude REAL DEFAULT 0,
                tremor_frequency REAL DEFAULT 0,
                tremor_severity INTEGER DEFAULT 0,
                cardiac_status TEXT DEFAULT '---',
                temp_status TEXT DEFAULT '---',
                tremor_status TEXT DEFAULT '---'
            )
            """
        )

        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(readings)").fetchall()
        ]

        needed_cols = {
            "spo2_status": "TEXT DEFAULT '---'",
            "tremor_frequency": "REAL DEFAULT 0",
            "tremor_severity": "INTEGER DEFAULT 0",
            "cardiac_status": "TEXT DEFAULT '---'",
            "temp_status": "TEXT DEFAULT '---'",
            "tremor_status": "TEXT DEFAULT '---'",
        }

        for col, col_type in needed_cols.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {col_type}")

        conn.commit()
    finally:
        conn.close()


init_db()


# ==========================================================
# NORMALIZE SENSOR DATA
# ==========================================================
def normalize(data: dict):
    if not isinstance(data, dict):
        data = {}

    def to_float(key, default=0.0):
        try:
            value = data.get(key, default)
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def to_int(key, default=0):
        try:
            return int(round(to_float(key, default)))
        except Exception:
            return default

    heart_rate = to_int("heart_rate", 0)
    spo2 = to_int("spo2", 0)
    temperature = round(to_float("temperature", 0.0), 1)
    tremor_amplitude = round(to_float("tremor_amplitude", 0.0), 4)
    tremor_frequency = round(to_float("tremor_frequency", 0.0), 2)
    tremor_severity = to_int("tremor_severity", 0)

    # Clean impossible values
    if heart_rate < 0 or heart_rate > 220:
        heart_rate = 0

    if spo2 < 0 or spo2 > 100:
        spo2 = 0

    if temperature < -20 or temperature > 80:
        temperature = 0.0

    if tremor_amplitude < 0:
        tremor_amplitude = 0.0

    if tremor_frequency < 0:
        tremor_frequency = 0.0

    if tremor_severity < 0:
        tremor_severity = 0

    return {
        "heart_rate": heart_rate,
        "spo2": spo2,
        "spo2_status": str(data.get("spo2_status", "---")),
        "temperature": temperature,
        "tremor_amplitude": tremor_amplitude,
        "tremor_frequency": tremor_frequency,
        "tremor_severity": tremor_severity,
        "cardiac_status": str(data.get("cardiac_status", "---")),
        "temp_status": str(data.get("temp_status", "---")),
        "tremor_status": str(data.get("tremor_status", "---")),
    }


def get_latest_row():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ==========================================================
# DETERMINISTIC MONITORING ASSESSMENT
# ==========================================================
def build_monitoring_assessment(data, validation=None):
    """Build the factual baseline that all LLMs must explain consistently."""
    hr = data.get("heart_rate", 0)
    spo2 = data.get("spo2", 0)
    temperature = data.get("temperature", 0)
    tremor_severity = data.get("tremor_severity", 0)

    priority = {"stable": 0, "sensor_check": 1, "watch": 2, "urgent": 3}
    level = "stable"
    findings = []
    home_steps = []
    urgent_signs = []
    data_quality = []
    care_by_vital = {}

    def elevate(new_level):
        nonlocal level
        if priority[new_level] > priority[level]:
            level = new_level

    def add_step(text):
        if text not in home_steps:
            home_steps.append(text)

    def add_urgent(text):
        if text not in urgent_signs:
            urgent_signs.append(text)

    if hr <= 0:
        elevate("sensor_check")
        data_quality.append("Heart-rate reading is missing.")
        findings.append(
            {"parameter": "Heart rate", "status": "No valid reading", "tone": "off"}
        )
        add_step("Keep the finger still and fully cover the MAX30102 sensor, then repeat.")
        care_by_vital["heart_rate"] = (
            "No valid pulse was captured. Sit still, warm the hand, cover the "
            "sensor fully, and repeat before drawing a conclusion."
        )
    elif hr > 100:
        elevate("watch")
        findings.append(
            {
                "parameter": "Heart rate",
                "status": f"{hr} bpm — above the project resting range",
                "tone": "warn",
            }
        )
        add_step("Sit quietly, rest for 5–10 minutes, drink water, and repeat the heart-rate reading.")
        add_step("Avoid caffeine, energy drinks, nicotine, and strenuous activity while rechecking.")
        care_by_vital["heart_rate"] = (
            "Rest seated for 5–10 minutes, breathe slowly, drink water, avoid "
            "caffeine/energy drinks/nicotine, and repeat the pulse measurement."
        )
        add_urgent(
            "Seek emergency help if a fast heartbeat occurs with chest pain, difficulty breathing, fainting, or severe dizziness."
        )
    elif hr < 60:
        elevate("watch")
        findings.append(
            {
                "parameter": "Heart rate",
                "status": f"{hr} bpm — below the project resting range",
                "tone": "warn",
            }
        )
        add_step("Rest and repeat the reading; consider whether the person is athletic or taking heart-rate-altering medicine.")
        care_by_vital["heart_rate"] = (
            "Sit or lie down if dizzy, rest, and repeat the pulse. Do not drive "
            "or stand suddenly if weakness or light-headedness is present."
        )
        add_urgent(
            "Seek urgent help if a slow heartbeat occurs with fainting, chest pain, breathing difficulty, confusion, or marked weakness."
        )
    else:
        findings.append(
            {
                "parameter": "Heart rate",
                "status": f"{hr} bpm — inside the project resting range",
                "tone": "ok",
            }
        )
        care_by_vital["heart_rate"] = (
            "The pulse is inside the project's resting range. Continue normal "
            "hydration and monitor the trend rather than one isolated value."
        )

    if spo2 <= 0:
        elevate("sensor_check")
        data_quality.append("SpO2 reading is missing.")
        findings.append(
            {"parameter": "SpO2", "status": "No valid reading", "tone": "off"}
        )
        add_step("Warm the hand, keep it still, and repeat the finger-sensor reading.")
        care_by_vital["spo2"] = (
            "No valid oxygen reading was captured. Warm the hand, remove nail "
            "polish if relevant, keep the finger still, and repeat."
        )
    elif spo2 < 90:
        elevate("urgent")
        findings.append(
            {
                "parameter": "SpO2",
                "status": f"{spo2}% — urgent low reading",
                "tone": "crit",
            }
        )
        add_step("Stop activity, sit upright, keep the hand still, and repeat immediately.")
        care_by_vital["spo2"] = (
            "Stop activity, sit upright, loosen tight clothing, warm the hand, "
            "and repeat immediately. Do not use oxygen unless it was prescribed."
        )
        add_urgent(
            "If the low reading persists, or there is breathing difficulty, blue/grey lips, confusion, or severe weakness, seek emergency medical help."
        )
    elif spo2 < 95:
        elevate("watch")
        findings.append(
            {
                "parameter": "SpO2",
                "status": f"{spo2}% — below the project target",
                "tone": "warn",
            }
        )
        add_step("Rest, warm the hand, verify finger placement, and repeat the reading.")
        care_by_vital["spo2"] = (
            "Rest seated, breathe normally, warm the hand, verify finger "
            "placement, and repeat after a few minutes."
        )
        add_urgent(
            "Contact a healthcare professional if repeated readings remain low or symptoms are present."
        )
    else:
        findings.append(
            {
                "parameter": "SpO2",
                "status": f"{spo2}% — inside the project target",
                "tone": "ok",
            }
        )
        care_by_vital["spo2"] = (
            "The oxygen reading is inside the project target. Keep monitoring "
            "if symptoms are present because symptoms matter more than one number."
        )

    if temperature <= 0 or temperature < 32:
        elevate("sensor_check")
        data_quality.append(
            f"Temperature {temperature}°C is implausible as a body reading and likely reflects sensor contact or ambient temperature."
        )
        findings.append(
            {
                "parameter": "Temperature",
                "status": f"{temperature}°C — verify sensor contact",
                "tone": "off",
            }
        )
        add_step("Place the DS18B20 firmly against the measurement site, insulate it from room air, wait, and repeat.")
        care_by_vital["temperature"] = (
            "This is probably an ambient/contact reading, not body temperature. "
            "Position and insulate the sensor correctly, wait, and repeat."
        )
        add_urgent(
            "If a reliable thermometer confirms body temperature below 35°C, treat it as an emergency: move indoors, remove wet clothing, wrap in dry blankets, and seek emergency help."
        )
    elif temperature < 35:
        elevate("urgent")
        findings.append(
            {
                "parameter": "Temperature",
                "status": f"{temperature}°C — confirmed low body temperature is urgent",
                "tone": "crit",
            }
        )
        add_step("Move indoors, remove wet clothing, and wrap in dry blankets while arranging emergency help.")
        care_by_vital["temperature"] = (
            "If confirmed with a reliable thermometer, move indoors, remove wet "
            "clothing, wrap in dry blankets, and arrange emergency help. Do not "
            "use direct intense heat or rub the limbs."
        )
        add_urgent("Do not use a hot bath, heat lamp, or rub the arms and legs.")
    elif temperature >= 39.4:
        elevate("urgent")
        findings.append(
            {
                "parameter": "Temperature",
                "status": f"{temperature}°C — high fever range",
                "tone": "crit",
            }
        )
        add_step("Rest, drink fluids if able, use light clothing, and recheck with a reliable thermometer.")
        care_by_vital["temperature"] = (
            "Rest, drink water or other non-alcoholic fluids, use light clothing, "
            "keep the room comfortable, and recheck with a reliable thermometer."
        )
        add_urgent(
            "Seek prompt medical advice; seek emergency help for confusion, difficulty breathing, seizure, blue lips, or inability to wake."
        )
    elif temperature >= 38:
        elevate("watch")
        findings.append(
            {
                "parameter": "Temperature",
                "status": f"{temperature}°C — fever range",
                "tone": "warn",
            }
        )
        add_step("Rest, drink fluids, use light clothing, keep the room comfortable, and repeat the measurement.")
        care_by_vital["temperature"] = (
            "Rest, drink more water or other non-alcoholic fluids, wear light "
            "clothing, keep the room comfortable, and repeat the temperature."
        )
        add_urgent(
            "Contact a healthcare professional if fever persists, rises, or occurs with concerning symptoms."
        )
    else:
        findings.append(
            {
                "parameter": "Temperature",
                "status": f"{temperature}°C — no fever detected",
                "tone": "ok",
            }
        )
        care_by_vital["temperature"] = (
            "No fever is detected from a valid body-temperature reading. Continue "
            "normal fluids and monitor if the person feels unwell."
        )

    if tremor_severity >= 3:
        elevate("watch")
        findings.append(
            {
                "parameter": "Tremor",
                "status": f"Severity {tremor_severity}/4 — marked sensor movement",
                "tone": "warn",
            }
        )
        add_step("Rest the arm on a stable surface and repeat the tremor measurement.")
        add_step("Reduce stress stimulation and avoid caffeine or energy drinks while rechecking.")
        care_by_vital["tremor"] = (
            "Sit safely, support the arm on a stable surface, breathe slowly, "
            "reduce stress, and avoid caffeine/energy drinks before repeating."
        )
        add_urgent(
            "Seek urgent help for sudden tremor with weakness, facial droop, speech difficulty, confusion, or loss of balance."
        )
    else:
        findings.append(
            {
                "parameter": "Tremor",
                "status": f"Severity {tremor_severity}/4",
                "tone": "ok" if tremor_severity == 0 else "warn",
            }
        )
        care_by_vital["tremor"] = (
            "Support the arm, relax the hand, breathe slowly, and monitor the "
            "trend. Avoid excess caffeine if shaking becomes noticeable."
        )

    dataset_rows = []
    if isinstance(validation, dict) and validation.get("status") == "ok":
        comparison = validation.get("comparison", {})
        specs = (
            ("Heart rate", comparison.get("heart_rate", {}), "BIDMC ICU dataset"),
            ("SpO2", comparison.get("spo2", {}), "BIDMC ICU dataset"),
            ("Tremor", comparison.get("tremor", {}), "ALAMEDA Parkinson's dataset"),
        )
        for parameter, item, source in specs:
            difference = item.get("difference_percent")
            dataset_rows.append(
                {
                    "parameter": parameter,
                    "sensor": item.get("sensor_value"),
                    "reference": item.get("reference_average"),
                    "difference_percent": difference,
                    "direction": item.get("direction"),
                    "source": source,
                    "interpretation": item.get("interpretation"),
                }
            )

    titles = {
        "stable": ("Stable monitoring snapshot", "No immediate warning detected in valid readings."),
        "sensor_check": ("Verify sensors before conclusions", "At least one reading is missing or implausible."),
        "watch": ("Repeat and monitor", "One or more readings deserve a calm repeat measurement."),
        "urgent": ("Urgent attention recommended", "A repeated valid reading may require prompt professional help."),
    }
    title, subtitle = titles[level]

    return {
        "level": level,
        "title": title,
        "subtitle": subtitle,
        "findings": findings,
        "data_quality": data_quality,
        "home_steps": home_steps[:5],
        "urgent_signs": urgent_signs[:5],
        "care_by_vital": care_by_vital,
        "dataset_rows": dataset_rows,
        "dataset_explanation": (
            "BIDMC and ALAMEDA averages answer 'how different is this signal from "
            "this research dataset?' They do not answer 'is this patient healthy?'."
        ),
        "disclaimer": (
            "Educational monitoring support only. Repeat unexpected readings and "
            "use symptoms plus professional assessment for medical decisions. "
            "This system does not measure blood pressure and must not infer it."
        ),
        "sources": [
            {"name": "NHS hypothermia guidance", "url": "https://www.nhs.uk/conditions/hypothermia/"},
            {"name": "MedlinePlus fever guidance", "url": "https://medlineplus.gov/ency/article/003090.htm"},
            {"name": "NHLBI arrhythmia symptoms", "url": "https://www.nhlbi.nih.gov/health/arrhythmias/symptoms"},
        ],
    }


# ==========================================================
# AI PROMPT
# ==========================================================
def build_prompt(data, validation=None, assessment=None):
    heart_rate = data.get("heart_rate", 0)
    spo2 = data.get("spo2", 0)
    temperature = data.get("temperature", 0)
    tremor_amplitude = data.get("tremor_amplitude", 0)
    tremor_frequency = data.get("tremor_frequency", 0)
    tremor_severity = data.get("tremor_severity", 0)

    comparison = (
        validation.get("comparison", {})
        if isinstance(validation, dict) and validation.get("status") == "ok"
        else {}
    )

    def comparison_line(key, dataset):
        item = comparison.get(key, {})
        difference = item.get("difference_percent")
        if difference is None:
            return f"unavailable vs {dataset}"
        return f"{abs(difference):.1f}% {item.get('direction', '')} {dataset} average"

    findings = {
        item.get("parameter"): item.get("status")
        for item in (assessment or {}).get("findings", [])
    }
    care = (assessment or {}).get("care_by_vital", {})
    urgent = (assessment or {}).get("urgent_signs", [])

    def prediction_labels():
        if heart_rate <= 0:
            hr_prediction = ("Missing", "Cannot assess")
        elif heart_rate > 100:
            hr_prediction = ("High", "Monitor")
        elif heart_rate < 60:
            hr_prediction = ("Low", "Monitor")
        else:
            hr_prediction = ("Normal", "Low concern")

        if spo2 <= 0:
            spo2_prediction = ("Missing", "Cannot assess")
        elif spo2 < 90:
            spo2_prediction = ("Critical", "Urgent")
        elif spo2 < 95:
            spo2_prediction = ("Low", "Monitor")
        else:
            spo2_prediction = ("Normal", "Low concern")

        if temperature < 32:
            temperature_prediction = ("Unreliable measurement", "Cannot assess")
        elif temperature < 35 or temperature >= 39.4:
            temperature_prediction = ("Critical", "Urgent")
        elif temperature >= 38:
            temperature_prediction = ("High", "Monitor")
        else:
            temperature_prediction = ("Normal", "Low concern")

        tremor_prediction = (
            ("High", "Monitor")
            if tremor_severity >= 3
            else ("Normal", "Low concern")
        )
        return (
            hr_prediction,
            spo2_prediction,
            temperature_prediction,
            tremor_prediction,
        )

    hr_prediction, spo2_prediction, temperature_prediction, tremor_prediction = (
        prediction_labels()
    )

    return f"""
Create a concise classification report. Use ONLY these verified facts.
HR {heart_rate} bpm: {findings.get('Heart rate', 'unavailable')}.
SpO2 {spo2}%: {findings.get('SpO2', 'unavailable')}.
Temperature {temperature}°C: {findings.get('Temperature', 'unavailable')}.
Tremor {tremor_amplitude} m/s², {tremor_frequency} Hz, severity {tremor_severity}/4:
{findings.get('Tremor', 'unavailable')}.
Dataset: HR {comparison_line('heart_rate', 'BIDMC')}; SpO2
{comparison_line('spo2', 'BIDMC')}; tremor {comparison_line('tremor', 'ALAMEDA')}.
Output exactly:
PREDICTIONS
Heart Rate: {heart_rate} bpm — {hr_prediction[0]} — {hr_prediction[1]}
SpO2: {spo2}% — {spo2_prediction[0]} — {spo2_prediction[1]}
Temperature: {temperature}°C — {temperature_prediction[0]} — {temperature_prediction[1]}
Tremor: amplitude {tremor_amplitude} m/s², severity {tremor_severity}/4 — {tremor_prediction[0]} — {tremor_prediction[1]}
OVERALL PREDICTION
One sentence summarizing the complete monitoring state.
PATIENT GUIDANCE
One concise sentence explaining what the patient should do now.
WHEN TO SEEK HELP
One concise sentence stating when urgent or professional help is needed.
DATASET EVIDENCE
One sentence with all three exact statistical comparisons.

Copy the four prediction lines above exactly; do not reinterpret or alter
their classifications. Base guidance only on the verified home-care and urgent
warning facts above. When valid readings are stable, normal hydration, rest,
and continued monitoring are appropriate. A temperature below 32°C is an
unreliable body-temperature measurement, not hypothermia; recommend repeating
it correctly. Never recommend medicine, unprescribed oxygen, or a diagnosis.
Maximum 145 words. Plain text. No markdown, blood pressure, or invented facts.
Zero HR/SpO2 means missing. Dataset evidence is statistical, never a clinical
healthy range.
"""


# ==========================================================
# AI MODELS
# ==========================================================
def call_groq_model(prompt, model_name):
    if not GROQ_API_KEY:
        return "Groq Cloud is optional and is not configured on this computer."

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 220,
                "temperature": 0.1,
            },
            timeout=25,
        )

        if response.status_code != 200:
            return f"Groq error {response.status_code}: {response.text[:250]}"

        return response.json()["choices"][0]["message"]["content"]

    except Exception as e:
        return f"Groq error: {str(e)}"


def call_groq_large(prompt):
    return call_groq_model(prompt, "llama-3.3-70b-versatile")


def call_groq_compact(prompt):
    return call_groq_model(prompt, "llama-3.1-8b-instant")


def call_ollama(prompt):
    if not OLLAMA_BASE_URL:
        return "Ollama Local is optional and is not configured in this environment."

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": "llama3.2",
                "prompt": prompt,
                "stream": False,
                "keep_alive": "10m",
                "options": {
                    "temperature": 0.1,
                    "num_predict": 240,
                    "num_ctx": 4096,
                    "repeat_penalty": 1.1,
                },
            },
            timeout=150,
        )

        if response.status_code != 200:
            return f"Ollama error {response.status_code}. Make sure Ollama is running."

        return response.json().get("response", "No response from Ollama.")

    except Exception as e:
        return f"Ollama error: {str(e)}. Is Ollama reachable at {OLLAMA_BASE_URL}?"


def normalize_prediction_output(text, data, validation, assessment=None):
    """Keep model summaries, but enforce verified classifications and guidance."""
    hr = data.get("heart_rate", 0)
    spo2 = data.get("spo2", 0)
    temperature = data.get("temperature", 0)
    tremor_amplitude = data.get("tremor_amplitude", 0)
    tremor_severity = data.get("tremor_severity", 0)

    hr_label = (
        ("Missing", "Cannot assess")
        if hr <= 0
        else ("High", "Monitor")
        if hr > 100
        else ("Low", "Monitor")
        if hr < 60
        else ("Normal", "Low concern")
    )
    spo2_label = (
        ("Missing", "Cannot assess")
        if spo2 <= 0
        else ("Critical", "Urgent")
        if spo2 < 90
        else ("Low", "Monitor")
        if spo2 < 95
        else ("Normal", "Low concern")
    )
    temperature_label = (
        ("Unreliable measurement", "Cannot assess")
        if temperature < 32
        else ("Critical", "Urgent")
        if temperature < 35 or temperature >= 39.4
        else ("High", "Monitor")
        if temperature >= 38
        else ("Normal", "Low concern")
    )
    tremor_label = (
        ("High", "Monitor")
        if tremor_severity >= 3
        else ("Normal", "Low concern")
    )

    original = (text or "").strip()
    overall_match = re.search(
        r"OVERALL PREDICTION\s*(.*?)(?:\s*PATIENT GUIDANCE|"
        r"\s*WHEN TO SEEK HELP|\s*DATASET EVIDENCE|\Z)",
        original,
        flags=re.IGNORECASE | re.DOTALL,
    )
    overall = (
        " ".join(overall_match.group(1).split())
        if overall_match and overall_match.group(1).strip()
        else "The available readings were classified using the project monitoring rules."
    )

    stable_valid_readings = (
        60 <= hr <= 100
        and spo2 >= 95
        and tremor_severity < 3
    )
    if spo2 > 0 and spo2 < 90:
        guidance = (
            "Stop activity, sit upright, remain calm, and repeat the SpO2 reading "
            "immediately while keeping the hand warm and still."
        )
        seek_help = (
            "Seek emergency help if the repeated SpO2 remains below 90% or there "
            "is breathing difficulty, blue or grey lips, confusion, or severe weakness."
        )
    elif temperature >= 39.4 or (32 <= temperature < 35):
        guidance = (
            "Rest in a safe place and confirm the temperature with a reliable "
            "thermometer before making decisions."
        )
        seek_help = (
            "Seek urgent medical help if a reliable thermometer confirms this "
            "critical temperature or concerning symptoms are present."
        )
    elif hr <= 0 or spo2 <= 0:
        guidance = (
            "Repeat the missing measurements before drawing a conclusion, rest, "
            "and continue normal hydration if medically permitted."
        )
        seek_help = (
            "Seek professional advice if symptoms are present or valid repeated "
            "measurements remain unavailable or become abnormal."
        )
    elif hr > 100 or hr < 60 or (0 < spo2 < 95) or tremor_severity >= 3:
        guidance = (
            "Rest, drink water if normally allowed, avoid strenuous activity, "
            "and repeat the abnormal readings after several minutes."
        )
        seek_help = (
            "Contact a healthcare professional if abnormal readings persist; "
            "seek emergency help for chest pain, fainting, breathing difficulty, "
            "confusion, weakness, or speech difficulty."
        )
    elif stable_valid_readings:
        guidance = (
            "Continue normal hydration, rest as needed, and monitor the trend; "
            "repeat the temperature because it is not a reliable body reading."
            if temperature < 32
            else
            "Continue normal hydration and routine monitoring of the vital-sign trend."
        )
        seek_help = (
            "Seek professional advice if new symptoms appear or repeated readings "
            "move outside the project ranges."
        )
    else:
        guidance = (
            "Repeat missing or unreliable measurements before drawing a conclusion, "
            "and continue normal hydration if medically permitted."
        )
        seek_help = (
            "Seek professional advice if symptoms are present or valid repeated "
            "readings are abnormal."
        )

    comparison = (
        validation.get("comparison", {})
        if isinstance(validation, dict) and validation.get("status") == "ok"
        else {}
    )

    def comparison_text(key, dataset):
        item = comparison.get(key, {})
        difference = item.get("difference_percent")
        direction = item.get("direction", "unavailable")
        if difference is None:
            return f"unavailable versus {dataset}"
        if key == "tremor" and abs(float(difference)) > 500:
            return (
                f"{abs(float(difference)):.1f}% {direction} the {dataset} average, "
                "but this extreme gap suggests incompatible units or preprocessing"
            )
        return f"{abs(float(difference)):.1f}% {direction} the {dataset} average"

    dataset_sentence = (
        f"Heart rate is {comparison_text('heart_rate', 'BIDMC')}; "
        f"SpO2 is {comparison_text('spo2', 'BIDMC')}; "
        f"tremor is {comparison_text('tremor', 'ALAMEDA')}. "
        "These are statistical comparisons, not clinical ranges."
    )

    return (
        "PREDICTIONS\n"
        f"Heart Rate: {hr} bpm — {hr_label[0]} — {hr_label[1]}\n"
        f"SpO2: {spo2}% — {spo2_label[0]} — {spo2_label[1]}\n"
        f"Temperature: {temperature}°C — {temperature_label[0]} — {temperature_label[1]}\n"
        f"Tremor: amplitude {tremor_amplitude} m/s², severity {tremor_severity}/4 "
        f"— {tremor_label[0]} — {tremor_label[1]}\n"
        "OVERALL PREDICTION\n"
        f"{overall}\n"
        "PATIENT GUIDANCE\n"
        f"{guidance}\n"
        "WHEN TO SEEK HELP\n"
        f"{seek_help}\n"
        "DATASET EVIDENCE\n"
        f"{dataset_sentence}"
    )


async def run_timed_engine(function, *args):
    started = time.perf_counter()
    text = await asyncio.to_thread(function, *args)
    return {
        "text": text,
        "seconds": round(time.perf_counter() - started, 1),
    }


def score_llm_response(text, data, validation):
    """Score factual accuracy, safety, completeness, and response quality."""
    lower = (text or "").lower()
    score = 0
    checks = []

    def add_check(name, passed, points):
        nonlocal score
        score += points if passed else 0
        checks.append({"name": name, "passed": passed, "points": points})

    heading_specs = (
        ("predictions", ("predictions",), 3),
        ("overall prediction", ("overall prediction",), 3),
        ("patient guidance", ("patient guidance",), 3),
        ("when to seek help", ("when to seek help",), 3),
        ("dataset evidence", ("dataset evidence",), 3),
    )
    for heading, alternatives, points in heading_specs:
        add_check(
            f"section: {heading}",
            any(alternative in lower for alternative in alternatives),
            points,
        )

    fact_specs = (
        ("heart rate", data.get("heart_rate", 0), ("heart rate", "hr"), ("bpm",)),
        ("SpO2", data.get("spo2", 0), ("spo2", "oxygen"), ("%", "percent")),
        ("temperature", data.get("temperature", 0), ("temperature",), ("°c", "c")),
        ("tremor", data.get("tremor_amplitude", 0), ("tremor",), ("m/s²", "m/s2")),
    )
    missing_terms = ("not available", "unavailable", "missing", "no valid")
    for name, value, labels, units in fact_specs:
        label_present = any(label in lower for label in labels)
        if name in ("heart rate", "SpO2") and value <= 0:
            passed = label_present and any(term in lower for term in missing_terms)
        else:
            passed = label_present and str(value) in lower
        add_check(f"{name} exact value", passed, 6)
        add_check(
            f"{name} unit",
            passed and any(unit in lower for unit in units),
            2,
        )

    severity = data.get("tremor_severity", 0)
    add_check(
        "tremor severity exact",
        "tremor" in lower and f"{severity}/4" in lower,
        3,
    )

    comparison = (
        validation.get("comparison", {})
        if isinstance(validation, dict) and validation.get("status") == "ok"
        else {}
    )
    add_check("BIDMC identified", "bidmc" in lower, 3)
    add_check("ALAMEDA identified", "alameda" in lower, 3)
    add_check(
        "mathematical-not-clinical boundary",
        (
            ("mathematical" in lower or "statistical" in lower)
            and ("not clinical" in lower or "not a clinical" in lower)
        ),
        4,
    )

    for key, label in (
        ("heart_rate", "heart-rate direction"),
        ("spo2", "SpO2 direction"),
        ("tremor", "tremor direction"),
    ):
        item = comparison.get(key, {})
        direction = item.get("direction", "")
        if direction == "unavailable":
            passed = any(term in lower for term in missing_terms)
        else:
            passed = direction in lower
        add_check(label, passed, 3)

        difference = item.get("difference_percent")
        exact_difference = (
            difference is not None
            and (
                f"{abs(float(difference)):.1f}" in lower
                or f"{abs(float(difference)):.2f}" in lower
            )
        )
        add_check(f"{label} exact percentage", exact_difference, 4)

    temperature = data.get("temperature", 0)

    expected_predictions = {
        "heart rate": (
            "missing"
            if data.get("heart_rate", 0) <= 0
            else "high"
            if data.get("heart_rate", 0) > 100
            else "low"
            if data.get("heart_rate", 0) < 60
            else "normal"
        ),
        "spo2": (
            "missing"
            if data.get("spo2", 0) <= 0
            else "critical"
            if data.get("spo2", 0) < 90
            else "low"
            if data.get("spo2", 0) < 95
            else "normal"
        ),
        "temperature": (
            "unreliable measurement"
            if temperature < 32
            else "critical"
            if temperature < 35 or temperature >= 39.4
            else "high"
            if temperature >= 38
            else "normal"
        ),
        "tremor": "high" if severity >= 3 else "normal",
    }
    for label, expected in expected_predictions.items():
        line = next(
            (
                candidate.lower()
                for candidate in (text or "").splitlines()
                if candidate.lower().startswith(label)
            ),
            "",
        )
        add_check(
            f"{label} classification",
            bool(line) and expected in line,
            5,
        )

    add_check(
        "unreliable temperature cannot be assessed",
        temperature >= 32
        or (
            "temperature" in lower
            and "unreliable measurement" in lower
            and "cannot assess" in lower
        ),
        5,
    )
    add_check(
        "overall prediction supplied",
        "overall prediction" in lower
        and any(term in lower for term in ("stable", "monitor", "urgent", "cannot assess")),
        3,
    )
    overall_match = re.search(
        r"overall prediction\s*(.*?)(?:\s*patient guidance|"
        r"\s*when to seek help|\s*dataset evidence|\Z)",
        lower,
        flags=re.DOTALL,
    )
    overall_text = overall_match.group(1) if overall_match else ""
    overall_topics = (
        ("heart-rate detail", ("heart rate", "hr", "pulse")),
        ("SpO2 detail", ("spo2", "oxygen")),
        ("temperature detail", ("temperature", "thermal")),
        ("tremor detail", ("tremor",)),
    )
    for label, terms in overall_topics:
        add_check(
            f"overall prediction includes {label}",
            any(term in overall_text for term in terms),
            1,
        )

    prediction_contradictions = []
    hr_value = data.get("heart_rate", 0)
    spo2_value = data.get("spo2", 0)
    if 60 <= hr_value <= 100 and re.search(
        r"(low|high|critical|abnormal)\s+(heart rate|hr|pulse)|"
        r"(heart rate|hr|pulse)\s+(is\s+)?(low|high|critical|abnormal)",
        overall_text,
    ):
        prediction_contradictions.append("normal heart rate contradicted")
    if hr_value <= 0 and re.search(
        r"(normal|stable)\s+(heart rate|hr|pulse)|"
        r"(heart rate|hr|pulse)\s+(is\s+)?(normal|stable)",
        overall_text,
    ):
        prediction_contradictions.append("missing heart rate described as normal")
    if spo2_value >= 95 and re.search(
        r"(low|critical|abnormal)\s+(spo2|oxygen)|"
        r"(spo2|oxygen)\s+(is\s+)?(low|critical|abnormal)",
        overall_text,
    ):
        prediction_contradictions.append("normal SpO2 contradicted")
    if 0 < spo2_value < 90 and re.search(
        r"(normal|stable)\s+(spo2|oxygen)|"
        r"(spo2|oxygen)\s+(is\s+)?(normal|stable)",
        overall_text,
    ):
        prediction_contradictions.append("critical SpO2 described as normal")
    if temperature < 32 and re.search(
        r"(low|critical|normal)\s+(body\s+)?temperature|"
        r"(temperature)\s+(is\s+)?(low|critical|normal)",
        overall_text,
    ):
        prediction_contradictions.append(
            "unreliable temperature interpreted as body status"
        )
    if severity >= 3 and re.search(
        r"(normal|low)\s+tremor|tremor\s+(is\s+)?(normal|low)",
        overall_text,
    ):
        prediction_contradictions.append("high tremor described as normal")

    if prediction_contradictions:
        score -= 25 * len(prediction_contradictions)
    checks.append(
        {
            "name": "overall prediction has no contradictions",
            "passed": not prediction_contradictions,
            "details": prediction_contradictions,
        }
    )
    prohibited_guidance = (
        "take medication",
        "double the dose",
        "stop medication",
        "use oxygen",
        "oxygen therapy",
    )
    add_check(
        "guidance avoids unsafe treatment",
        not any(term in lower for term in prohibited_guidance),
        3,
    )

    word_count = len((text or "").split())
    if 55 <= word_count <= 95:
        concision_points = 4
    elif 35 <= word_count <= 110:
        concision_points = 2
    elif 25 <= word_count <= 125:
        concision_points = 1
    else:
        concision_points = 0
    add_check(
        "concise response",
        concision_points > 0,
        concision_points,
    )
    add_check(
        "complete ending",
        bool((text or "").strip())
        and (text or "").strip()[-1] in ".!?",
        2,
    )

    unsafe_terms = (
        "take medication",
        "oxygen therapy",
        "double the dose",
        "stop medication",
        "one short action",
        "status;",
        "classification",
        "concern level",
    )
    unsafe = any(term in lower for term in unsafe_terms)
    if unsafe:
        score -= 25
    checks.append({"name": "no unsafe treatment claim", "passed": not unsafe})

    contradictions = []
    if data.get("heart_rate", 0) <= 0 and (
        "heart rate is normal" in lower or "heart rate and spo2 are within" in lower
    ):
        contradictions.append("invalid HR described as normal")
    if data.get("spo2", 0) <= 0 and (
        "spo2 is normal" in lower or "heart rate and spo2 are within" in lower
    ):
        contradictions.append("invalid SpO2 described as normal")
    if "alameda dataset average for heart rate" in lower:
        contradictions.append("ALAMEDA incorrectly used for heart rate")
    if "alameda dataset average for spo2" in lower:
        contradictions.append("ALAMEDA incorrectly used for SpO2")
    if (
        "acceptable range" in lower
        or "healthy range" in lower
        or "safe range" in lower
    ) and ("dataset" in lower or "bidmc" in lower or "alameda" in lower):
        contradictions.append("dataset proximity described as a clinical range")
    if contradictions:
        score -= 15 * len(contradictions)
    checks.append(
        {
            "name": "no factual contradictions",
            "passed": not contradictions,
            "details": contradictions,
        }
    )

    normalized_score = round(max(0, score) / 122 * 100)
    return {
        "score": min(100, normalized_score),
        "checks": checks,
        "word_count": word_count,
        "passed_checks": sum(1 for check in checks if check.get("passed")),
    }


# ==========================================================
# API: RECEIVE DATA FROM ESP32
# ==========================================================
@app.post("/api/data")
async def receive_data(data: dict):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean = normalize(data)

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO readings (
                timestamp,
                heart_rate,
                spo2,
                spo2_status,
                temperature,
                tremor_amplitude,
                tremor_frequency,
                tremor_severity,
                cardiac_status,
                temp_status,
                tremor_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                clean["heart_rate"],
                clean["spo2"],
                clean["spo2_status"],
                clean["temperature"],
                clean["tremor_amplitude"],
                clean["tremor_frequency"],
                clean["tremor_severity"],
                clean["cardiac_status"],
                clean["temp_status"],
                clean["tremor_status"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    message = json.dumps(
        {
            **clean,
            "timestamp": timestamp,
            "data_age_seconds": 0,
            "is_live": True,
            "stream_event": "live",
        }
    )

    # A slow/stale dashboard WebSocket must never block the ESP32 POST cycle.
    asyncio.create_task(broadcast_sensor_data(message))

    print(
        f"[SAVED {timestamp}] "
        f"HR={clean['heart_rate']} | "
        f"SpO2={clean['spo2']} | "
        f"Temp={clean['temperature']} | "
        f"Tremor={clean['tremor_amplitude']}",
        flush=True,
    )

    return {
        "status": "ok",
        "timestamp": timestamp,
        "received": clean,
    }


# ==========================================================
# API: PREDICTION
# ==========================================================
@app.post("/api/predict")
async def predict(data: dict = None):
    try:
        # Use one fresh database snapshot so the displayed alert, dataset
        # comparison, and all three LLMs analyze the same reading.
        latest_row = get_latest_row()
        data = latest_row or data or {}

        clean = normalize(data)

        try:
            validation = make_json_safe(validate_latest_reading())
        except Exception as e:
            validation = {
                "status": "error",
                "message": str(e),
            }

        assessment = build_monitoring_assessment(clean, validation)
        prompt = build_prompt(clean, validation, assessment)

        if GROQ_API_KEY:
            groq_large_task = run_timed_engine(call_groq_large, prompt)
            groq_compact_task = run_timed_engine(call_groq_compact, prompt)
        else:
            missing_groq = {
                "text": "Groq Cloud is not configured.",
                "seconds": 0,
            }
            groq_large_task = asyncio.sleep(
                0,
                result=missing_groq,
            )
            groq_compact_task = asyncio.sleep(
                0,
                result=missing_groq,
            )

        groq_large, groq_compact, ollama_result = await asyncio.gather(
            groq_large_task,
            groq_compact_task,
            (
                run_timed_engine(call_ollama, prompt)
                if OLLAMA_BASE_URL
                else asyncio.sleep(
                    0,
                    result={
                        "text": "Ollama Local is not configured in this environment.",
                        "seconds": 0,
                    },
                )
            ),
        )

        models = [
            {
                **groq_large,
                "id": "groq_large",
                "name": "Llama 3.3 70B",
                "provider": "Groq Cloud",
                "status": (
                    "not_configured"
                    if not GROQ_API_KEY
                    else "error"
                    if groq_large["text"].lower().startswith("groq error")
                    else "ready"
                ),
            },
            {
                **groq_compact,
                "id": "groq_compact",
                "name": "Llama 3.1 8B Instant",
                "provider": "Groq Cloud",
                "status": (
                    "not_configured"
                    if not GROQ_API_KEY
                    else "error"
                    if groq_compact["text"].lower().startswith("groq error")
                    else "ready"
                ),
            },
            {
                **ollama_result,
                "id": "ollama",
                "name": "Llama 3.2",
                "provider": "Ollama Local",
                "status": (
                    "not_configured"
                    if not OLLAMA_BASE_URL
                    else
                    "error"
                    if ollama_result["text"].lower().startswith("ollama error")
                    else "ready"
                ),
            },
        ]

        for model in models:
            raw_text = model["text"]
            quality = (
                score_llm_response(raw_text, clean, validation)
                if model["status"] == "ready"
                else {"score": 0, "checks": []}
            )
            model.update(quality)
            if model["status"] == "ready":
                model["text"] = normalize_prediction_output(
                    raw_text,
                    clean,
                    validation,
                    assessment,
                )

        ready_models = [model for model in models if model["status"] == "ready"]
        winner = (
            max(
                ready_models,
                key=lambda model: (
                    model["score"],
                    model.get("passed_checks", 0),
                    -abs(model.get("word_count", 80) - 80),
                    -model["seconds"],
                ),
            )
            if ready_models
            else None
        )

        return make_json_safe(
            {
                "status": "ok",
                "models": models,
                "winner": (
                    {
                        "tie": False,
                        "models": [
                            {
                                "id": winner["id"],
                                "name": winner["name"],
                                "provider": winner["provider"],
                            }
                        ],
                        "id": winner["id"],
                        "name": winner["name"],
                        "provider": winner["provider"],
                        "score": winner["score"],
                        "reason": (
                            "Best combined score for exact sensor values, units, "
                            "dataset percentages, safety, completeness, and clarity."
                        ),
                    }
                    if winner
                    else None
                ),
                "sms_preview": (
                    f"IoT monitor [{assessment['level'].upper()}]: "
                    f"HR {clean['heart_rate']} bpm, SpO2 {clean['spo2']}%, "
                    f"temperature {clean['temperature']}°C, tremor severity "
                    f"{clean['tremor_severity']}/4. {assessment['title']}. "
                    f"{assessment['home_steps'][0] if assessment['home_steps'] else 'Continue monitoring.'}"
                ),
                "assessment": assessment,
                "validation": validation,
                "input_data": clean,
            }
        )

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


# ==========================================================
# API: VALIDATION / COMPARE
# ==========================================================
@app.get("/api/validation")
def get_validation():
    try:
        return make_json_safe(validate_latest_reading())
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


@app.get("/api/assessment")
def get_assessment():
    try:
        row = get_latest_row()
        if not row:
            return {"status": "no_data", "message": "No sensor readings available."}
        clean = normalize(row)
        validation = make_json_safe(validate_latest_reading())
        return {
            "status": "ok",
            "input_data": clean,
            "assessment": build_monitoring_assessment(clean, validation),
            "validation": validation,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/compare")
def compare_datasets():
    try:
        result = make_json_safe(validate_latest_reading())

        if not isinstance(result, dict):
            return {
                "status": "error",
                "message": "Invalid response from dataset module.",
            }

        return result

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


# ==========================================================
# API: LATEST / HISTORY / STATISTICS
# ==========================================================
@app.get("/api/latest")
def get_latest():
    row = get_latest_row()
    if not row:
        return {"status": "no_data", "message": "No data yet"}
    return make_json_safe(with_live_status(row))


@app.get("/api/history")
def get_history(limit: int = 50):
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return make_json_safe([dict(row) for row in rows])
    finally:
        conn.close()


@app.get("/api/statistics")
def get_statistics():
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                AVG(CASE WHEN heart_rate > 0 THEN heart_rate END) AS avg_hr,
                AVG(CASE WHEN spo2 > 0 THEN spo2 END) AS avg_spo2,
                AVG(CASE WHEN temperature > 0 THEN temperature END) AS avg_temp,
                AVG(tremor_amplitude) AS avg_tremor,
                MAX(CASE WHEN heart_rate > 0 THEN heart_rate END) AS max_hr,
                MIN(CASE WHEN heart_rate > 0 THEN heart_rate END) AS min_hr
            FROM readings
            """
        ).fetchone()
    finally:
        conn.close()

    if not row or row["total"] == 0:
        return {
            "status": "no_data",
            "message": "No readings yet",
        }

    return make_json_safe(
        {
            "status": "ok",
            "total_readings": row["total"],
            "averages": {
                "heart_rate": round(row["avg_hr"] or 0, 1),
                "spo2": round(row["avg_spo2"] or 0, 1),
                "temperature": round(row["avg_temp"] or 0, 1),
                "tremor_amplitude": round(row["avg_tremor"] or 0, 4),
            },
            "ranges": {
                "heart_rate_min": row["min_hr"] or 0,
                "heart_rate_max": row["max_hr"] or 0,
            },
        }
    )


# ==========================================================
# DASHBOARD + WEBSOCKET
# ==========================================================
@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    if not DASHBOARD_PATH.exists():
        return HTMLResponse(
            content="""
            <html>
                <head><title>IoT Dashboard</title></head>
                <body>
                    <h1>IoT Vital Signs Monitoring System</h1>
                    <p>dashboard.html not found.</p>
                    <p>Backend is running correctly.</p>
                    <p>Check /api/latest, /api/history, /api/statistics.</p>
                </body>
            </html>
            """,
            status_code=200,
        )

    with open(DASHBOARD_PATH, "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)

    try:
        latest = get_latest_row()
        if latest:
            await websocket.send_text(
                json.dumps(make_json_safe(with_live_status(latest)))
            )

        while True:
            # Keeps websocket alive. Dashboard may send ping text.
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass

    except Exception:
        pass

    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


# ==========================================================
# ROOT
# ==========================================================
@app.get("/")
def root():
    return {
        "system": "IoT Vital Signs Monitoring System",
        "status": "running",
        "database": str(DB_PATH),
        "dashboard": "http://localhost:8000/dashboard",
        "endpoints": [
            "/api/data",
            "/api/latest",
            "/api/history",
            "/api/statistics",
            "/api/validation",
            "/api/assessment",
            "/api/compare",
            "/api/predict",
            "/ws",
        ],
        "models": [
            "Groq Llama 3.3 70B",
            "Groq Llama 3.1 8B Instant",
            "Ollama Llama 3.2 Local",
        ],
    }


# ==========================================================
# LOCAL RUN
# ==========================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=not bool(os.getenv("RENDER")),
    )
