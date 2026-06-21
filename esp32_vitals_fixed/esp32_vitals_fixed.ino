#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include "spo2_algorithm.h"
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <math.h>
#include "arduino_config.h"

const int SCREEN_WIDTH = 128;
const int SCREEN_HEIGHT = 64;
const int OLED_RESET = -1;
const uint8_t OLED_ADDRESS = 0x3C;

const char* WIFI_SSID = SECRET_WIFI_SSID;
const char* WIFI_PASS = SECRET_WIFI_PASS;
const char* RENDER_SERVER_URL = SECRET_RENDER_SERVER_URL;
const char* LOCAL_SERVER_URL = SECRET_LOCAL_SERVER_URL;

MAX30105 particleSensor;
Adafruit_MPU6050 mpu;
OneWire oneWire(4);
DallasTemperature tempSensor(&oneWire);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

bool maxOK = false;
bool mpuOK = false;
bool fingerOn = false;
bool oledOK = false;

float avgBpm = 0;
int spo2 = 0;

// Maxim's reference algorithm uses 100 samples (4 seconds at an effective
// 25 samples/second when sampleAverage=4 and sampleRate=100).
const int OXIMETER_BUFFER_SIZE = 100;
const int OXIMETER_STEP = 25;
// MAX30102 modules differ noticeably in LED/lens sensitivity. Use a slightly
// lower threshold and debounce removal so one noisy sample cannot erase the
// whole four-second calibration buffer.
// Detect the finger quickly. Declare removal after only a few consecutive
// near-zero readings, so the OLED/dashboard shows NO FINGER immediately,
// while the low OFF threshold avoids false removal during small movements.
const uint32_t FINGER_IR_THRESHOLD_ON = 3000;
const uint32_t FINGER_IR_THRESHOLD_OFF = 1200;
const int FINGER_OFF_SAMPLES = 3;
int fingerLowSamples = 0;
uint32_t irBuffer[OXIMETER_BUFFER_SIZE];
uint32_t redBuffer[OXIMETER_BUFFER_SIZE];
int oximeterSamples = 0;
int32_t calculatedHeartRate = 0;
int32_t calculatedSpO2 = 0;
int8_t heartRateValid = 0;
int8_t spo2Valid = 0;
uint32_t lastIR = 0;
uint32_t lastRed = 0;
unsigned long lastFastBeat = 0;
const int FAST_BPM_SIZE = 4;
float fastBpmBuffer[FAST_BPM_SIZE] = {0};
int fastBpmIndex = 0;
int fastBpmCount = 0;
const unsigned long MIN_BEAT_INTERVAL_MS = 300;

float correctPossibleDoubleBpm(float bpm) {
  // Optical sensors can detect both the main pulse and a secondary wave.
  // If that produces an implausible doubled resting value, use its half.
  if (bpm >= 120.0f && bpm <= 200.0f) {
    float halfBpm = bpm / 2.0f;
    if (halfBpm >= 50.0f && halfBpm <= 100.0f) {
      return halfBpm;
    }
  }
  return bpm;
}

void addFastBpm(float bpm) {
  bpm = correctPossibleDoubleBpm(bpm);
  if (bpm < 40.0f || bpm > 180.0f) return;

  // Reject sudden isolated jumps after a stable value has been established.
  if (
    avgBpm > 0
    && (bpm > avgBpm * 1.35f || bpm < avgBpm * 0.65f)
  ) {
    return;
  }

  fastBpmBuffer[fastBpmIndex] = bpm;
  fastBpmIndex = (fastBpmIndex + 1) % FAST_BPM_SIZE;
  if (fastBpmCount < FAST_BPM_SIZE) fastBpmCount++;

  float bpmSum = 0;
  for (int i = 0; i < fastBpmCount; i++) {
    bpmSum += fastBpmBuffer[i];
  }
  avgBpm = bpmSum / fastBpmCount;
}

float bodyTemp = 0.0;
String tempStatus = "---";

const int TREM_WIN = 150;
float tremBuffer[TREM_WIN] = {0};
int tremIdx = 0;
float tremBaseline = 9.8;
float tremAmplitude = 0.0;
float tremFrequency = 0.0;
int tremSeverity = 0;
String tremStatus = "No tremor";

String cardiacStatus = "---";
String spo2Status = "---";

unsigned long lastSample = 0;
unsigned long lastLocalSend = 0;
unsigned long lastRenderSend = 0;
unsigned long lastTempRead = 0;
unsigned long lastDebug = 0;
unsigned long lastOledUpdate = 0;
unsigned long lastWifiAttempt = 0;
unsigned long lastSuccessfulPost = 0;
unsigned long localRetryAfter = 0;
int consecutiveAllPostFailures = 0;
int consecutiveLocalPostFailures = 0;
bool urgentLocalFingerUpdate = false;

void updateOled() {
  if (!oledOK) return;

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setTextWrap(false);

  display.setCursor(0, 0);
  display.print("HR: ");
  if (fingerOn && avgBpm > 0) {
    display.print((int)round(avgBpm));
    display.print(" bpm");
  } else if (fingerOn) {
    display.print("measuring...");
  } else {
    display.print("place finger");
  }

  display.setCursor(0, 11);
  display.print("SpO2: ");
  if (fingerOn && spo2 > 0) {
    display.print(spo2);
    display.print("%");
  } else if (fingerOn) {
    display.print(oximeterSamples);
    display.print("/100");
  } else {
    display.print("--%");
  }

  display.setCursor(0, 22);
  display.print("TEMP: ");
  if (isfinite(bodyTemp) && bodyTemp > -20 && bodyTemp < 80) {
    display.print(bodyTemp, 1);
    display.print(" C");
  } else {
    display.print("-- C");
  }

  display.setCursor(0, 33);
  display.print("TREM: ");
  display.print(tremSeverity);
  display.print("/4");

  display.setCursor(0, 44);
  display.print("AMP: ");
  display.print(tremAmplitude, 2);

  display.setCursor(0, 55);
  display.print("FREQ: ");
  display.print(tremFrequency, 1);
  display.print("Hz");

  display.display();
}

void updateCardiacStatus() {
  if (!fingerOn) cardiacStatus = "No finger";
  else if (avgBpm <= 0) cardiacStatus = "Calibrating";
  else if (avgBpm > 100) cardiacStatus = "TACHYCARDIA";
  else if (avgBpm < 60) cardiacStatus = "BRADYCARDIA";
  else cardiacStatus = "NORMAL";
}

void updateSpo2Status() {
  if (!fingerOn || spo2 <= 0) spo2Status = "---";
  else if (spo2 >= 95) spo2Status = "NORMAL";
  else if (spo2 >= 90) spo2Status = "LOW";
  else spo2Status = "CRITICAL";
}

void updateTempStatus() {
  if (bodyTemp < -20 || bodyTemp > 80) tempStatus = "Sensor error";
  else if (bodyTemp < 35.0) tempStatus = "LOW";
  else if (bodyTemp <= 37.5) tempStatus = "NORMAL";
  else if (bodyTemp <= 38.0) tempStatus = "ELEVATED";
  else if (bodyTemp <= 39.0) tempStatus = "FEVER";
  else tempStatus = "HIGH FEVER";
}

void startWiFiConnection() {
  Serial.println("[WiFi] Starting connection...");
  WiFi.disconnect(false, false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lastWifiAttempt = millis();
}

void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  unsigned long now = millis();
  if (now - lastWifiAttempt >= 10000) {
    startWiFiConnection();
  }
}

void resetOximeter() {
  oximeterSamples = 0;
  calculatedHeartRate = 0;
  calculatedSpO2 = 0;
  heartRateValid = 0;
  spo2Valid = 0;
  avgBpm = 0;
  spo2 = 0;
  lastFastBeat = 0;
  fastBpmIndex = 0;
  fastBpmCount = 0;
  fingerLowSamples = 0;
  for (int i = 0; i < FAST_BPM_SIZE; i++) fastBpmBuffer[i] = 0;
}

void processOximeter() {
  if (!maxOK) {
    fingerOn = false;
    resetOximeter();
    return;
  }

  // Read every sample already waiting in the MAX30102 FIFO.
  particleSensor.check();
  while (particleSensor.available()) {
    lastRed = particleSensor.getRed();
    lastIR = particleSensor.getIR();
    particleSensor.nextSample();

    if (!fingerOn && lastIR >= FINGER_IR_THRESHOLD_ON) {
      fingerOn = true;
      fingerLowSamples = 0;
      Serial.printf("[MAX30102] Finger detected | IR=%lu\n", lastIR);
    } else if (fingerOn && lastIR < FINGER_IR_THRESHOLD_OFF) {
      fingerLowSamples++;
    } else if (fingerOn) {
      fingerLowSamples = 0;
    }

    if (fingerOn && fingerLowSamples >= FINGER_OFF_SAMPLES) {
      fingerOn = false;
      Serial.printf("[MAX30102] Finger removed | IR=%lu\n", lastIR);
      resetOximeter();
      urgentLocalFingerUpdate = true;
      continue;
    }

    if (!fingerOn) {
      continue;
    }

    // Fast heart-rate result: available after the first two detected beats,
    // while the 100-sample Maxim calculation continues in the background.
    if (checkForBeat((int32_t)lastIR)) {
      unsigned long beatNow = millis();
      if (lastFastBeat == 0) {
        lastFastBeat = beatNow;
      } else {
        unsigned long beatInterval = beatNow - lastFastBeat;
        if (beatInterval >= MIN_BEAT_INTERVAL_MS) {
          float instantBpm = 60000.0f / beatInterval;
          addFastBpm(instantBpm);
          lastFastBeat = beatNow;
        }
      }
    }

    irBuffer[oximeterSamples] = lastIR;
    redBuffer[oximeterSamples] = lastRed;
    oximeterSamples++;

    if (oximeterSamples == OXIMETER_BUFFER_SIZE) {
      maxim_heart_rate_and_oxygen_saturation(
        irBuffer,
        OXIMETER_BUFFER_SIZE,
        redBuffer,
        &calculatedSpO2,
        &spo2Valid,
        &calculatedHeartRate,
        &heartRateValid
      );

      if (
        heartRateValid
        && calculatedHeartRate >= 40
        && calculatedHeartRate <= 200
      ) {
        float correctedHeartRate =
          correctPossibleDoubleBpm((float)calculatedHeartRate);

        // Smooth valid readings without hiding genuine changes.
        avgBpm = avgBpm <= 0
          ? correctedHeartRate
          : avgBpm * 0.80f + correctedHeartRate * 0.20f;
      }

      if (
        spo2Valid
        && calculatedSpO2 >= 70
        && calculatedSpO2 <= 100
      ) {
        spo2 = calculatedSpO2;
      }

      // Keep the newest 75 samples and calculate again after 25 new ones.
      for (int i = OXIMETER_STEP; i < OXIMETER_BUFFER_SIZE; i++) {
        irBuffer[i - OXIMETER_STEP] = irBuffer[i];
        redBuffer[i - OXIMETER_STEP] = redBuffer[i];
      }
      oximeterSamples = OXIMETER_BUFFER_SIZE - OXIMETER_STEP;
    }
  }
}

bool postJsonToServer(
  const char* serverUrl,
  const char* serverName,
  const char* json,
  int connectTimeoutMs,
  int requestTimeoutMs
) {
  Serial.printf("[%s] POST -> %s\n", serverName, serverUrl);

  WiFiClient plainClient;
  WiFiClientSecure secureClient;
  WiFiClient* client = &plainClient;

  if (String(serverUrl).startsWith("https://")) {
    // Render uses HTTPS. For a capstone prototype, accept its managed TLS
    // certificate without storing a CA certificate on the ESP32.
    secureClient.setInsecure();
    client = &secureClient;
  }
  client->setTimeout((requestTimeoutMs + 999) / 1000);

  HTTPClient http;
  if (!http.begin(*client, serverUrl)) {
    Serial.printf("[%s] ERROR: http.begin failed\n", serverName);
    return false;
  }

  // Free Render services can need extra time after an idle spin-down.
  http.setConnectTimeout(connectTimeoutMs);
  http.setTimeout(requestTimeoutMs);
  http.addHeader("Content-Type", "application/json");

  int code = http.POST((uint8_t*)json, strlen(json));

  if (code == HTTP_CODE_OK) {
    lastSuccessfulPost = millis();
    Serial.printf("[%s] POST 200 OK | heap=%u\n", serverName, ESP.getFreeHeap());
  } else {
    if (code > 0) {
      Serial.printf(
        "[%s] POST failed: HTTP %d | response=%s\n",
        serverName,
        code,
        http.getString().c_str()
      );
    } else {
      Serial.printf(
        "[%s] POST failed: %s (%d)\n",
        serverName,
        HTTPClient::errorToString(code).c_str(),
        code
      );
    }
  }

  http.end();
  client->stop();
  return code == HTTP_CODE_OK;
}

bool sendData(bool sendRender, bool sendLocal) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[HTTP] SKIP: WiFi disconnected");
    return false;
  }

  int sendHR = (fingerOn && avgBpm > 0) ? (int)round(avgBpm) : 0;
  int sendSpo2 = (fingerOn && spo2 > 0) ? spo2 : 0;
  float sendTemp = bodyTemp;
  if (!isfinite(sendTemp) || sendTemp < -20 || sendTemp > 80) sendTemp = 0.0;

  // Fixed buffer avoids String heap fragmentation after long runtimes.
  char json[640];
  int written = snprintf(
    json,
    sizeof(json),
    "{\"heart_rate\":%d,\"spo2\":%d,\"spo2_status\":\"%s\","
    "\"temperature\":%.1f,\"tremor_amplitude\":%.3f,"
    "\"tremor_frequency\":%.2f,\"tremor_severity\":%d,"
    "\"cardiac_status\":\"%s\",\"temp_status\":\"%s\","
    "\"tremor_status\":\"%s\"}",
    sendHR,
    sendSpo2,
    spo2Status.c_str(),
    sendTemp,
    tremAmplitude,
    tremFrequency,
    tremSeverity,
    cardiacStatus.c_str(),
    tempStatus.c_str(),
    tremStatus.c_str()
  );

  if (written <= 0 || written >= (int)sizeof(json)) {
    Serial.println("[HTTP] ERROR: JSON buffer too small");
    return false;
  }

  // Render and local delivery are independent. Send to Render first whenever
  // it is due, so a slow/unreachable laptop can never delay the cloud update.
  bool renderOK = !sendRender;
  bool localOK = !sendLocal;
  if (sendRender) {
    renderOK = postJsonToServer(
      RENDER_SERVER_URL,
      "RENDER",
      json,
      8000,
      12000
    );
  }

  if (sendLocal) {
    localOK = postJsonToServer(
      LOCAL_SERVER_URL,
      "LOCAL",
      json,
      1000,
      2000
    );

    if (localOK) {
      consecutiveLocalPostFailures = 0;
      localRetryAfter = 0;
    } else {
      consecutiveLocalPostFailures++;
      if (consecutiveLocalPostFailures >= 1) {
        localRetryAfter = millis() + 5000;
        consecutiveLocalPostFailures = 0;
        Serial.println("[LOCAL] Offline; retrying in 5 seconds");
      }
    }
  }

  if (renderOK || localOK) {
    consecutiveAllPostFailures = 0;
  } else {
    consecutiveAllPostFailures++;
  }

  // Rebuild WiFi only when neither destination can be reached repeatedly.
  if (consecutiveAllPostFailures >= 3) {
    Serial.println("[HTTP] Both servers failed 3 times: rebuilding WiFi");
    consecutiveAllPostFailures = 0;
    WiFi.disconnect(false, false);
    delay(100);
    startWiFiConnection();
  }

  return renderOK && localOK;
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Wire.begin(21, 22);

  Serial.println("\n=== IoT Vital Signs Monitor ROBUST ===");

  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS)) {
    oledOK = true;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(20, 20);
    display.println("OLED READY");
    display.setCursor(8, 36);
    display.println("Starting system...");
    display.display();
    Serial.println("[OLED] Ready");
  } else {
    Serial.println("[OLED] NOT FOUND");
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  startWiFiConnection();

  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 15000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[WiFi] Connected, IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("[WiFi] Render server: ");
    Serial.println(RENDER_SERVER_URL);
    Serial.print("[WiFi] Local server: ");
    Serial.println(LOCAL_SERVER_URL);
  } else {
    Serial.println("[WiFi] Initial connection failed; background retry enabled.");
  }

  if (particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    particleSensor.setup(0x1F, 4, 2, 100, 411, 4096);
    maxOK = true;
    Serial.println("[MAX30102] Ready");
    Serial.println("[MAX30102] Place finger steadily for 4-6 seconds");
  } else {
    Serial.println("[MAX30102] NOT FOUND");
  }

  if (mpu.begin()) {
    mpu.setAccelerometerRange(MPU6050_RANGE_2_G);
    mpu.setGyroRange(MPU6050_RANGE_250_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_10_HZ);
    mpuOK = true;
    Serial.println("[MPU6050] Ready");
  } else {
    Serial.println("[MPU6050] NOT FOUND");
  }

  tempSensor.begin();
  tempSensor.requestTemperatures();
  bodyTemp = tempSensor.getTempCByIndex(0);
  updateTempStatus();
  Serial.println("[DS18B20] Ready");
}

void loop() {
  unsigned long now = millis();

  maintainWiFi();

  if (now - lastSample < 20) {
    delay(1);
    return;
  }
  lastSample = now;

  processOximeter();

  updateCardiacStatus();
  updateSpo2Status();

  // Do not wait for the normal one-second cycle when the finger is removed.
  // Push HR=0, SpO2=0 and "No finger" to localhost immediately.
  if (urgentLocalFingerUpdate && WiFi.status() == WL_CONNECTED) {
    urgentLocalFingerUpdate = false;
    lastLocalSend = now;
    Serial.println("[LOCAL] Immediate NO FINGER update");
    sendData(false, true);
  }

  // Localhost refreshes every second. Cloud updates are intentionally slower:
  // HTTPS can pause the loop and starve the MAX30102 FIFO during calibration.
  bool localRetryReady = (
    localRetryAfter == 0
    || (long)(now - localRetryAfter) >= 0
  );
  bool localDue = localRetryReady && now - lastLocalSend >= 1000;
  bool renderDue = now - lastRenderSend >= 10000;
  if (localDue || renderDue) {
    if (localDue) lastLocalSend = now;
    if (renderDue) lastRenderSend = now;
    sendData(renderDue, localDue);
  }

  if (mpuOK) {
    sensors_event_t a, g, tempEvent;
    mpu.getEvent(&a, &g, &tempEvent);

    float mag = sqrt(
      a.acceleration.x * a.acceleration.x
      + a.acceleration.y * a.acceleration.y
      + a.acceleration.z * a.acceleration.z
    );

    tremBaseline = tremBaseline * 0.99 + mag * 0.01;
    tremBuffer[tremIdx++] = mag - tremBaseline;

    if (tremIdx >= TREM_WIN) {
      tremIdx = 0;
      float sumSq = 0;
      for (int i = 0; i < TREM_WIN; i++) {
        sumSq += tremBuffer[i] * tremBuffer[i];
      }
      tremAmplitude = sqrt(sumSq / TREM_WIN);

      int crossings = 0;
      for (int i = 1; i < TREM_WIN; i++) {
        if (
          (tremBuffer[i - 1] < -0.08 && tremBuffer[i] > 0.08)
          || (tremBuffer[i - 1] > 0.08 && tremBuffer[i] < -0.08)
        ) {
          crossings++;
        }
      }

      float windowSec = (TREM_WIN * 20.0) / 1000.0;
      tremFrequency = (crossings / 2.0) / windowSec;

      if (tremAmplitude < 0.20) {
        tremSeverity = 0;
        tremStatus = "No tremor";
      } else if (tremAmplitude < 0.70) {
        tremSeverity = 1;
        tremStatus = "Slight";
      } else if (tremAmplitude < 1.40) {
        tremSeverity = 2;
        tremStatus = "Moderate";
      } else if (tremAmplitude < 2.30) {
        tremSeverity = 3;
        tremStatus = "Marked";
      } else {
        tremSeverity = 4;
        tremStatus = "Severe";
      }
    }
  } else {
    tremAmplitude = 0;
    tremFrequency = 0;
    tremSeverity = 0;
    tremStatus = "MPU error";
  }

  if (now - lastTempRead >= 3000) {
    tempSensor.requestTemperatures();
    bodyTemp = tempSensor.getTempCByIndex(0);
    if (bodyTemp == DEVICE_DISCONNECTED_C) {
      bodyTemp = 0.0;
      tempStatus = "Sensor error";
    } else {
      updateTempStatus();
    }
    lastTempRead = now;
  }

  if (now - lastOledUpdate >= 500) {
    lastOledUpdate = now;
    updateOled();
  }

  if (now - lastDebug >= 2000) {
    Serial.println("--------------------------------");
    Serial.printf(
      "[WiFi] status=%d | IP=%s | RSSI=%d | heap=%u | last POST=%lus\n",
      WiFi.status(),
      WiFi.localIP().toString().c_str(),
      WiFi.RSSI(),
      ESP.getFreeHeap(),
      lastSuccessfulPost == 0 ? 0 : (millis() - lastSuccessfulPost) / 1000
    );
    Serial.printf(
      "[MAX30102] OK=%s | Finger=%s | IR=%ld | RED=%ld\n",
      maxOK ? "YES" : "NO",
      fingerOn ? "YES" : "NO",
      (long)lastIR,
      (long)lastRed
    );
    Serial.printf(
      "[HR] %.1f bpm | valid=%d | samples=%d | %s\n",
      avgBpm,
      heartRateValid,
      oximeterSamples,
      cardiacStatus.c_str()
    );
    Serial.printf(
      "[SpO2] %d%% | valid=%d | %s\n",
      spo2,
      spo2Valid,
      spo2Status.c_str()
    );
    Serial.printf("[TEMP] %.1f C | %s\n", bodyTemp, tempStatus.c_str());
    Serial.printf(
      "[TREMOR] Amp=%.3f | Freq=%.2fHz | Sev=%d | %s\n",
      tremAmplitude,
      tremFrequency,
      tremSeverity,
      tremStatus.c_str()
    );
    lastDebug = now;
  }
}
