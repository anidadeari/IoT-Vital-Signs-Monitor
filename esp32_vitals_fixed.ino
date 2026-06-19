#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Wire.h>
#include "MAX30105.h"
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>
#include "arduino_secrets.h"

const char* WIFI_SSID = SECRET_WIFI_SSID;
const char* WIFI_PASS = SECRET_WIFI_PASS;
const char* SERVER_URL = SECRET_SERVER_URL;

MAX30105 particleSensor;
Adafruit_MPU6050 mpu;
OneWire oneWire(4);
DallasTemperature tempSensor(&oneWire);

bool maxOK = false;
bool mpuOK = false;
bool fingerOn = false;

long irBaseline = 0;
long redBaseline = 0;
long prevAC = 0;
long lastBeatTime = 0;
const int BPM_AVG = 8;
float bpmBuffer[BPM_AVG] = {0};
int bpmIdx = 0;
int bpmCount = 0;
float avgBpm = 0;

long irMin = 999999;
long irMax = 0;
long redMin = 999999;
long redMax = 0;
unsigned long lastSpO2 = 0;
int spo2 = 0;

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
unsigned long lastSend = 0;
unsigned long lastTempRead = 0;
unsigned long lastDebug = 0;
unsigned long lastWifiAttempt = 0;
unsigned long lastSuccessfulPost = 0;
int consecutivePostFailures = 0;

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

bool sendData() {
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

  WiFiClient plainClient;
  WiFiClientSecure secureClient;
  WiFiClient* client = &plainClient;

  if (String(SERVER_URL).startsWith("https://")) {
    // Railway uses HTTPS. For a capstone prototype, accept its managed TLS
    // certificate without storing a CA certificate on the ESP32.
    secureClient.setInsecure();
    client = &secureClient;
  }
  client->setTimeout(6);

  HTTPClient http;
  if (!http.begin(*client, SERVER_URL)) {
    Serial.println("[HTTP] ERROR: http.begin failed");
    return false;
  }

  http.setConnectTimeout(3000);
  http.setTimeout(6000);
  http.addHeader("Content-Type", "application/json");

  int code = http.POST((uint8_t*)json, strlen(json));

  if (code == HTTP_CODE_OK) {
    consecutivePostFailures = 0;
    lastSuccessfulPost = millis();
    Serial.printf("[HTTP] POST 200 OK | heap=%u\n", ESP.getFreeHeap());
  } else {
    consecutivePostFailures++;
    if (code > 0) {
      Serial.printf(
        "[HTTP] POST failed: HTTP %d | response=%s | failures=%d\n",
        code,
        http.getString().c_str(),
        consecutivePostFailures
      );
    } else {
      Serial.printf(
        "[HTTP] POST failed: %s (%d) | failures=%d\n",
        HTTPClient::errorToString(code).c_str(),
        code,
        consecutivePostFailures
      );
    }
  }

  http.end();
  client->stop();

  // Rebuild WiFi after repeated socket/connection failures.
  if (consecutivePostFailures >= 3) {
    Serial.println("[HTTP] Three failures: rebuilding WiFi connection");
    consecutivePostFailures = 0;
    WiFi.disconnect(false, false);
    delay(100);
    startWiFiConnection();
  }

  return code == HTTP_CODE_OK;
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Wire.begin(21, 22);

  Serial.println("\n=== IoT Vital Signs Monitor ROBUST ===");

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
    Serial.print("[WiFi] Server: ");
    Serial.println(SERVER_URL);
  } else {
    Serial.println("[WiFi] Initial connection failed; background retry enabled.");
  }

  if (particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    particleSensor.setup(0x1F, 4, 2, 100, 411, 4096);
    irBaseline = particleSensor.getIR();
    redBaseline = particleSensor.getRed();
    maxOK = true;
    Serial.println("[MAX30102] Ready");
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

  // HTTP scheduling stays outside the sensor sampling gate.
  if (now - lastSend >= 1000) {
    lastSend = now;
    sendData();
  }

  if (now - lastSample < 20) {
    delay(1);
    return;
  }
  lastSample = now;

  long ir = 0;
  long red = 0;

  if (maxOK) {
    ir = particleSensor.getIR();
    red = particleSensor.getRed();
    fingerOn = ir > 50000;

    if (!fingerOn) {
      avgBpm = 0;
      bpmCount = 0;
      spo2 = 0;
      irMin = 999999;
      irMax = 0;
      redMin = 999999;
      redMax = 0;
    } else {
      irBaseline = (irBaseline * 95 + ir * 5) / 100;
      redBaseline = (redBaseline * 95 + red * 5) / 100;

      if (ir > irMax) irMax = ir;
      if (ir < irMin) irMin = ir;
      if (red > redMax) redMax = red;
      if (red < redMin) redMin = red;

      long ac = ir - irBaseline;
      if (prevAC < 0 && ac >= 0) {
        long interval = now - lastBeatTime;
        if (interval > 300) {
          lastBeatTime = now;
          float newBpm = 60000.0 / interval;
          if (newBpm > 40 && newBpm < 180) {
            bool accept = true;
            if (
              bpmCount >= 3
              && (newBpm > avgBpm * 1.3 || newBpm < avgBpm * 0.7)
            ) {
              accept = false;
            }
            if (accept) {
              bpmBuffer[bpmIdx] = newBpm;
              bpmIdx = (bpmIdx + 1) % BPM_AVG;
              if (bpmCount < BPM_AVG) bpmCount++;
              float sum = 0;
              for (int i = 0; i < bpmCount; i++) sum += bpmBuffer[i];
              avgBpm = sum / bpmCount;
            }
          }
        }
      }
      prevAC = ac;

      if (now - lastSpO2 >= 1000) {
        if (irBaseline > 0 && redBaseline > 0) {
          float irAC = irMax - irMin;
          float redAC = redMax - redMin;
          if (irAC > 0 && redAC > 0) {
            float ratio =
              (redAC / (float)redBaseline) / (irAC / (float)irBaseline);
            spo2 = (int)(110 - 25 * ratio);
            if (spo2 > 100) spo2 = 100;
            if (spo2 < 70) spo2 = 0;
          }
        }
        irMax = 0;
        irMin = 999999;
        redMax = 0;
        redMin = 999999;
        lastSpO2 = now;
      }
    }
  } else {
    fingerOn = false;
    avgBpm = 0;
    spo2 = 0;
  }

  updateCardiacStatus();
  updateSpo2Status();

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
      ir,
      red
    );
    Serial.printf("[HR] %.1f bpm | %s\n", avgBpm, cardiacStatus.c_str());
    Serial.printf("[SpO2] %d%% | %s\n", spo2, spo2Status.c_str());
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
