/**
 * Hydroleaf Smart Dosing Controller – HTTP‑only build (v3.1.1)
 * ------------------------------------------------------------
 *  ✦  Non‑blocking pumps   ✦  Wi‑Fi watchdog                 *
 *  ✦  Cloud API heartbeat  ✦  HTTP OTA (no TLS certificate)  *
 */

#pragma GCC optimize("Os")

/* ───────── 1. INCLUDES ─────────────────────────────────────────── */
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>  //  ← fixes ESPhttpUpdate error
#include <Update.h>
#include <ArduinoJson.h>
#include <esp_adc_cal.h>

/* ───────── 2. HARDWARE MAP ─────────────────────────────────────── */
#define RELAY_PUMP_1 26
#define RELAY_PUMP_2 27
#define RELAY_PUMP_3 14
#define RELAY_PUMP_4 12
#define PH_SENSOR_PIN 34
#define TDS_SENSOR_PIN 35
#define LED_STATUS 2

/* ───────── 3. CONSTANTS ────────────────────────────────────────── */
static const uint8_t DNS_PORT = 53;
static const char* DEFAULT_AP_PASS = "hydroleaf";
static const char* CLOUD_HOST = "http://cloud.hydroleaf.in";  // HTTP!
static const char* OTA_PATH = "/api/v1/device_comm/update/pull?device_id=";
static const char* UPDATE_CHECK_PATH = "/api/v1/device_comm/update?device_id=";
static const char* HEARTBEAT_PATH = "/api/v1/device_comm/heartbeat";
static const char* FW_VERSION = "3.1.1";
static const char* DEVICE_TYPE = "dosing_unit";

/*  Timing  */
static const uint32_t HB_INTERVAL = 60UL * 1000;             // 1 min
static const uint32_t UPDATE_INTERVAL = 60UL * 60UL * 1000;  // 1 hr
static const uint32_t WIFI_WATCHDOG_MS = 120UL * 1000;       // 2 min

/* ───────── 4. GLOBALS ─────────────────────────────────────────── */
Preferences prefs;
WebServer http(80);
DNSServer dns;
TFT_eSPI tft;
esp_adc_cal_characteristics_t* adc_chars = nullptr;

String g_ssid, g_pass, g_apPass, g_deviceId;
bool g_wifiConnected = false;
uint32_t wifiLostSince = 0;

/*  Sensor cache  */
float pHValue = -1.0f;
float tdsValue = -1.0f;

/*  Non‑blocking pump state  */
struct PumpState {
  bool active = false;
  uint32_t startMs = 0, durMs = 0;
} pumps[4];

/* ───────── 5. UTILITIES ───────────────────────────────────────── */
void showStatus(const String& msg) {
  tft.fillScreen(TFT_BLACK);
  tft.setCursor(10, 100);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.println(msg);
}
void setRelay(int idx, bool on) {
  digitalWrite(RELAY_PUMP_1 + idx - 1, on ? LOW : HIGH);
}
void savePref(const char* key, const String& v) {
  prefs.begin("doser_cfg", false);
  prefs.putString(key, v);
  prefs.end();
}

/* ───────── 6. PUMP CONTROL ────────────────────────────────────── */
void schedulePump(int pump, uint32_t ms) {
  if (pump < 1 || pump > 4 || ms == 0) return;
  pumps[pump - 1] = { true, millis(), ms };
  setRelay(pump, true);
  showStatus("Pump#" + String(pump) + " ON");
}
void updatePumps() {
  uint32_t now = millis();
  for (int i = 0; i < 4; ++i)
    if (pumps[i].active && now - pumps[i].startMs >= pumps[i].durMs) {
      setRelay(i + 1, false);
      pumps[i].active = false;
      showStatus("Pump#" + String(i + 1) + " OFF");
    }
}

/* ───────── 7. SENSOR READ ─────────────────────────────────────── */
void readSensors() {
  static uint32_t last = 0;
  if (millis() - last < 1000) return;
  last = millis();
  uint32_t sum = 0;
  for (int i = 0; i < 30; ++i) sum += analogRead(PH_SENSOR_PIN);
  float v = esp_adc_cal_raw_to_voltage(sum / 30, adc_chars) / 1000.0f;
  pHValue = constrain(7.0f + ((2.5f - v) / 0.18f), 0.0f, 14.0f);
  sum = 0;
  for (int i = 0; i < 30; ++i) sum += analogRead(TDS_SENSOR_PIN);
  float v2 = esp_adc_cal_raw_to_voltage(sum / 30, adc_chars) / 1000.0f;
  float cv = max(0.0f, v2 * (100.0f / 110.0f) - 0.12f);
  tdsValue = constrain(133.42f * pow(cv, 3) - 255.86f * pow(cv, 2) + 857.39f * cv, 0.0f, 2500.0f);
}

/* ───────── 8. CLOUD I/O (HTTP) ────────────────────────────────── */
bool cloudGET(const String& url, String& payload) {
  HTTPClient cli;
  if (!cli.begin(url)) return false;
  int code = cli.GET();
  if (code == HTTP_CODE_OK) payload = cli.getString();
  cli.end();
  return code == HTTP_CODE_OK;
}
bool cloudPOST(const String& url, const String& body, String& resp) {
  HTTPClient cli;
  if (!cli.begin(url)) return false;
  cli.addHeader("Content-Type", "application/json");
  int code = cli.POST(body);
  if (code == HTTP_CODE_OK) resp = cli.getString();
  cli.end();
  return code == HTTP_CODE_OK;
}

/* OTA (HTTP) */
void checkForUpdate() {
  if (!g_wifiConnected) return;
  String body;
  if (!cloudGET(String(CLOUD_HOST) + UPDATE_CHECK_PATH + g_deviceId, body)) return;
  if (body.indexOf("\"update_available\":true") < 0) return;

  showStatus("OTA updating…");
  WiFiClient client;
  httpUpdate.setLedPin(LED_STATUS, LOW);  // Blink LED while flashing
  httpUpdate.rebootOnUpdate(true);
  httpUpdate.update(client, String(CLOUD_HOST) + OTA_PATH + g_deviceId);
}

/* Heartbeat */
void sendHeartbeat() {
  if (!g_wifiConnected) return;
  StaticJsonDocument<256> d;
  d["device_id"] = g_deviceId;
  d["type"] = DEVICE_TYPE;
  d["version"] = FW_VERSION;
  String out;
  serializeJson(d, out);
  String resp;
  if (!cloudPOST(String(CLOUD_HOST) + HEARTBEAT_PATH, out, resp)) return;

  StaticJsonDocument<512> r;
  if (deserializeJson(r, resp) != DeserializationError::Ok) return;
  for (JsonObject t : r["tasks"].as<JsonArray>()) schedulePump(t["pump"] | 0, t["amount"] | 0);
}

/* ───────── 9. WEB ROUTES (unchanged + API) ────────────────────── */
String htmlHeader(const String& title) {
  return "<!doctype html><html><head><meta charset='utf-8' "
         "name='viewport' content='width=device-width,initial-scale=1'><title>"
         + title + "</title><style>body{font-family:Arial;margin:0;padding:20px;}h2{margin-top:0;}"
                   "input,button{width:100%;padding:10px;margin:8px 0;font-size:16px;}"
                   "button{background:#007bff;border:none;color:#fff;}button:hover{background:#0069d9}"
                   "a{display:block;margin:8px 0;color:#007bff;}</style></head><body>";
}

void sendJSON(int code, const JsonDocument& doc) {
  String o;
  serializeJson(doc, o);
  http.send(code, "application/json", o);
}

void handleDiscovery() {
  StaticJsonDocument<160> d;
  d["device_id"] = g_deviceId;
  d["name"] = "Hydroleaf Smart Doser";
  d["type"] = DEVICE_TYPE;
  d["version"] = FW_VERSION;
  d["status"] = g_wifiConnected ? "online" : "offline";
  d["ip"] = WiFi.localIP().toString();
  sendJSON(200, d);
}
void handleVersion() {
  StaticJsonDocument<32> d;
  d["version"] = FW_VERSION;
  sendJSON(200, d);
}

void handlePumpPOST() {
  if (!http.hasArg("plain")) {
    http.send(400, "text/plain", "Missing JSON");
    return;
  }
  StaticJsonDocument<128> j;
  if (deserializeJson(j, http.arg("plain"))) {
    http.send(400, "Bad JSON");
    return;
  }
  int pump = j["pump"] | 0, amt = j["amount"] | 0;
  schedulePump(pump, amt);
  j["timestamp"] = millis();
  sendJSON(200, j);
}
void handleDoseMonitor() {
  if (!http.hasArg("plain")) {
    http.send(400, "Missing");
    return;
  }
  StaticJsonDocument<128> j;
  if (deserializeJson(j, http.arg("plain"))) {
    http.send(400, "Bad");
    return;
  }
  int pump = j["pump"] | 0, amt = j["amount"] | 0;
  schedulePump(pump, amt);
  StaticJsonDocument<160> r;
  r["message"] = "Started";
  r["pump"] = pump;
  r["dose_ms"] = amt;
  r["ph"] = pHValue;
  r["tds"] = tdsValue;
  r["timestamp"] = millis();
  sendJSON(200, r);
}
void handlePumpCal() {
  if (!http.hasArg("plain")) {
    http.send(400, "Missing");
    return;
  }
  StaticJsonDocument<64> j;
  if (deserializeJson(j, http.arg("plain"))) {
    http.send(400, "Bad");
    return;
  }
  String cmd = j["command"] | "";
  if (cmd == "start") {
    for (int p = 1; p <= 4; ++p) schedulePump(p, 50000);
    http.send(200, "application/json", "{\"message\":\"calibration started\"}");
  } else if (cmd == "stop") {
    for (int i = 0; i < 4; ++i) {
      pumps[i].active = false;
      setRelay(i + 1, false);
    }
    http.send(200, "application/json", "{\"message\":\"calibration stopped\"}");
  } else http.send(400, "text/plain", "Invalid command");
}
void handleMonitor() {
  StaticJsonDocument<128> d;
  d["ph"] = pHValue;
  d["tds"] = tdsValue;
  sendJSON(200, d);
}

/* Register routes (+ keep existing portal pages if any) */
void setupRoutes() {
  http.on("/discovery", HTTP_GET, handleDiscovery);
  http.on("/version", HTTP_GET, handleVersion);
  http.on("/pump", HTTP_POST, handlePumpPOST);
  http.on("/dose_monitor", HTTP_POST, handleDoseMonitor);
  http.on("/pump_calibration", HTTP_POST, handlePumpCal);
  http.on("/monitor", HTTP_GET, handleMonitor);

  http.onNotFound([]() {
    http.sendHeader("Location", "/", true);
    http.send(302);
  });
  http.begin();
}

/* ───────── 10. CAPTIVE PORTAL ─────────────────────────────────── */
void portalStart() {
  WiFi.mode(WIFI_AP_STA);
  IPAddress apIP(192, 168, 0, 1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  WiFi.softAP(g_deviceId.c_str(), g_apPass.c_str());
  dns.start(DNS_PORT, "*", apIP);
  setupRoutes();
}

/* ───────── 11. SETUP ─────────────────────────────────────────── */
void setup() {
  Serial.begin(115200);
  tft.init();
  tft.setRotation(1);
  pinMode(LED_STATUS, OUTPUT);
  digitalWrite(LED_STATUS, LOW);
  for (int p : { RELAY_PUMP_1, RELAY_PUMP_2, RELAY_PUMP_3, RELAY_PUMP_4 }) {
    pinMode(p, OUTPUT);
    digitalWrite(p, HIGH);
  }

  prefs.begin("doser_cfg", false);
  g_ssid = prefs.getString("ssid", "");
  g_pass = prefs.getString("pass", "");
  g_apPass = prefs.getString("apPass", DEFAULT_AP_PASS);
  g_deviceId = prefs.getString("id", "");
  if (!g_deviceId.length()) {
    g_deviceId = "DOSER_" + String((uint32_t)esp_random(), HEX);
    prefs.putString("id", g_deviceId);
  }
  prefs.end();

  adc_chars = (esp_adc_cal_characteristics_t*)calloc(1, sizeof(*adc_chars));
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, 1100, adc_chars);
  analogSetPinAttenuation(PH_SENSOR_PIN, ADC_11db);
  analogSetPinAttenuation(TDS_SENSOR_PIN, ADC_11db);

  showStatus("Connecting Wi‑Fi…");
  WiFi.mode(WIFI_STA);
  WiFi.begin(g_ssid.c_str(), g_pass.c_str());
  uint32_t st = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - st < 10000) delay(200);
  g_wifiConnected = (WiFi.status() == WL_CONNECTED);
  showStatus(g_wifiConnected ? "Wi‑Fi OK" : "AP mode");

  portalStart();
}

/* ───────── 12. LOOP ──────────────────────────────────────────── */
void loop() {
  dns.processNextRequest();
  http.handleClient();
  updatePumps();
  readSensors();

  static uint32_t tHB = 0, tUpd = 0;
  uint32_t now = millis();
  if (now - tHB >= HB_INTERVAL) {
    tHB = now;
    sendHeartbeat();
  }
  if (now - tUpd >= UPDATE_INTERVAL) {
    tUpd = now;
    checkForUpdate();
  }

  if (WiFi.status() == WL_CONNECTED) wifiLostSince = 0;
  else {
    if (!wifiLostSince) wifiLostSince = now;
    if (now - wifiLostSince > WIFI_WATCHDOG_MS) {
      for (int i = 0; i < 4; ++i) {
        pumps[i].active = false;
        setRelay(i + 1, false);
      }
      ESP.restart();
    }
  }
}
