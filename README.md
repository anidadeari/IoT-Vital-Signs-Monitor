# IoT Vital Signs Monitoring System

This is my Computer Science capstone project, developed by **Anida Deari**.

The idea behind the project was to build one complete IoT system that can collect physiological measurements, display them in real time, save them for later review, compare them with biomedical reference data, and provide an AI-assisted explanation of the latest reading.

The system is built around an ESP32 and three sensors:

- MAX30102 for heart rate and blood oxygen saturation (SpO₂)
- DS18B20 for contact-temperature measurement
- MPU6050 for movement and tremor analysis
- SSD1306 OLED for local display of the latest values

The measurements are sent over Wi-Fi to a FastAPI backend, stored in SQLite, and shown on a live web dashboard.

> This project is an academic monitoring prototype. It is not a certified medical device and should not be used for diagnosis or treatment decisions.

## What the system does

The ESP32 collects and processes the sensor readings before sending them to the backend.

The current implementation supports:

- real-time heart-rate monitoring;
- SpO₂ calculation using red and infrared MAX30102 samples;
- temperature monitoring and status classification;
- tremor amplitude, frequency, severity, and status;
- live values on a 128 × 64 OLED display;
- local and cloud data delivery;
- SQLite storage with timestamps;
- live WebSocket updates;
- historical readings and summary statistics;
- CSV history export;
- comparison with bundled biomedical datasets;
- deterministic monitoring guidance;
- comparison of cloud and local language-model responses.

## System architecture

```text
MAX30102 ─┐
DS18B20 ──┼──> ESP32 ──Wi-Fi/HTTP──> FastAPI ──> SQLite
MPU6050 ──┘       │                       │
                  └──> OLED              ├──> REST API
                                          ├──> WebSocket
                                          ├──> Dataset comparison
                                          └──> AI-assisted analysis
                                                     │
                                                     └──> Web dashboard
```

The local backend receives ESP32 data every second. The deployed Render service receives the same data less frequently to reduce the interruption caused by repeated HTTPS requests.

## Hardware connections

The MAX30102, MPU6050, and OLED share the same I²C bus.

| Component | Pin | ESP32 |
|---|---|---|
| MAX30102 | SDA | GPIO 21 |
| MAX30102 | SCL | GPIO 22 |
| MPU6050 | SDA | GPIO 21 |
| MPU6050 | SCL | GPIO 22 |
| SSD1306 OLED | SDA | GPIO 21 |
| SSD1306 OLED | SCL | GPIO 22 |
| SSD1306 OLED | VCC | 3.3 V |
| SSD1306 OLED | GND | GND |
| DS18B20 | DATA | GPIO 4 |
| DS18B20 | VCC | 3.3 V |
| DS18B20 | GND | GND |

The DS18B20 data line requires a **4.7 kΩ pull-up resistor** between DATA and 3.3 V.

## OLED display

The OLED uses I²C address `0x3C` and displays:

- heart rate;
- SpO₂;
- temperature;
- tremor severity;
- tremor amplitude;
- tremor frequency.

While SpO₂ is being calculated, the display shows the sample progress. This makes it clear that the sensor is collecting the required optical window rather than being stuck.

## Software used

### ESP32

- Arduino framework
- SparkFun MAX3010x sensor library
- Adafruit MPU6050
- DallasTemperature and OneWire
- Adafruit GFX
- Adafruit SSD1306

### Backend and dashboard

- Python
- FastAPI
- Uvicorn
- SQLite
- WebSocket
- HTML, CSS, and JavaScript
- Chart.js

### AI integration

The backend can compare responses from:

- Llama 3.3 70B through Groq Cloud;
- Llama 3.1 8B Instant through Groq Cloud;
- Llama 3.2 through a local Ollama service.

The AI layer is optional. Sensor collection, storage, the dashboard, dataset comparison, and deterministic monitoring assessment continue to work when an AI provider is not configured.

## Biomedical dataset comparison

The dashboard compares the latest live ESP32 reading with averages calculated from the bundled CSV files.

| ESP32 measurement | Reference |
|---|---|
| Heart rate | BIDMC PPG and Respiration Dataset |
| SpO₂ | BIDMC PPG and Respiration Dataset |
| Tremor amplitude | ALAMEDA Parkinson's Disease Tremor Dataset |
| Temperature | Project reference range of 36.1–37.2 °C |

The comparison shows:

- the live ESP32 value;
- the reference average;
- the exact numerical difference;
- the percentage above or below the average;
- a plain-language conclusion.

These results describe mathematical distance from a dataset average. They are not clinical accuracy scores and do not determine whether a person is healthy. Tremor comparison is especially dependent on compatible units, sensor placement, and preprocessing methods.

## Project structure

```text
CapstoneIOT/
├── analysis/
│   └── compare_datasets.py
├── datasets/
│   ├── bidmc_01_Numerics.csv
│   └── ALAMEDA_PD_tremor_dataset.csv
├── esp32_vitals_fixed/
│   ├── esp32_vitals_fixed.ino
│   ├── arduino_config.example.h
│   └── arduino_config.h        # local only, ignored by Git
├── dashboard.html
├── main.py
├── render.yaml
├── requirements.txt
└── README.md
```

## Running the backend locally

### 1. Clone the repository

```bash
git clone https://github.com/anidadeari/IoT-Vital-Signs-Monitor.git
cd IoT-Vital-Signs-Monitor
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install the Python dependencies

```powershell
pip install -r requirements.txt
```

### 4. Start FastAPI

```powershell
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### 5. Open the dashboard

```text
http://127.0.0.1:8000/dashboard
```

The dashboard should be opened through FastAPI, not directly as a local `dashboard.html` file.

## Configuring the ESP32

Copy:

```text
esp32_vitals_fixed/arduino_config.example.h
```

to:

```text
esp32_vitals_fixed/arduino_config.h
```

Then enter the local configuration:

```cpp
#pragma once

#define SECRET_WIFI_SSID "YOUR_WIFI_NAME"
#define SECRET_WIFI_PASS "YOUR_WIFI_PASSWORD"
#define SECRET_RENDER_SERVER_URL "https://YOUR-SERVICE-NAME.onrender.com/api/data"
#define SECRET_LOCAL_SERVER_URL "http://YOUR-COMPUTER-LAN-IP:8000/api/data"
```

The ESP32 and the computer running FastAPI must be connected to the same network for local communication.

`arduino_config.h` is ignored by Git so that Wi-Fi credentials are not uploaded to GitHub.

## Uploading the Arduino sketch

1. Open `esp32_vitals_fixed/esp32_vitals_fixed.ino` in Arduino IDE.
2. Select **ESP32 Dev Module**.
3. Select the correct COM port.
4. Install the required sensor and display libraries.
5. Click **Verify**.
6. Click **Upload**.
7. If the board remains at `Connecting...`, hold the ESP32 **BOOT** button until writing begins.

After upload, open Serial Monitor at `115200` baud.

## Optional AI configuration

For Groq Cloud in PowerShell:

```powershell
$env:GROQ_API_KEY="YOUR_GROQ_API_KEY"
```

For a local Ollama service:

```powershell
$env:OLLAMA_BASE_URL="http://localhost:11434"
```

Start the backend from the same terminal so it can read the environment variables.

## Main API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/data` | Receive and store ESP32 data |
| `GET` | `/api/latest` | Return the latest reading |
| `GET` | `/api/history` | Return historical readings |
| `GET` | `/api/statistics` | Return summary statistics |
| `GET` | `/api/validation` | Compare the latest reading with reference data |
| `GET` | `/api/assessment` | Return deterministic monitoring guidance |
| `POST` | `/api/predict` | Run configured AI models |
| `WS` | `/ws` | Send live readings to the dashboard |
| `GET` | `/dashboard` | Open the monitoring interface |

## Notes from testing

The MAX30102 is sensitive to movement, finger pressure, placement, and surrounding light. A steady finger position is important during the first few seconds of measurement.

The DS18B20 can show room temperature when it is exposed to air. For a meaningful contact-temperature demonstration, the waterproof probe should be placed securely against the measurement surface and given time to stabilize.

The MPU6050 tremor value can change with sensor orientation, attachment, and intentional hand movement. Its result should be interpreted as a prototype movement indicator.

## Limitations and future work

This project demonstrates the complete engineering workflow, but it has not undergone clinical validation.

Future work could include:

- paired testing with certified medical devices;
- formal error metrics and repeated trials;
- standardized tremor-sensor placement;
- user authentication and patient profiles;
- encrypted storage and stricter access control;
- verified TLS certificates on the ESP32;
- offline buffering when the network is unavailable;
- mobile support and downloadable reports;
- additional sensors such as ECG or respiratory-rate monitoring.

## Author

**Anida Deari**  
Computer Science Capstone Project  
June 2026

