#pragma GCC optimize("Os")

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <TFT_eSPI.h>
#include <Update.h>
#include <ESPHTTPUpdate.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_adc_cal.h>

// ───────── Pin Mapping ────────────────────────────────────────────────
#define RELAY_PUMP_1   26
#define RELAY_PUMP_2   27
#define RELAY_PUMP_3   14
#define RELAY_PUMP_4   12
#define PH_SENSOR_PIN  34
#define TDS_SENSOR_PIN 35
#define LED_STATUS      2

// ───────── Constants ──────────────────────────────────────────────────
const byte   DNS_PORT           = 53;
const char*  DEFAULT_AP_PASS    = "hydroleaf";
const char*  CLOUD_HOST         = "http://cloud.hydroleaf.in";
const char*  OTA_PATH           = "/api/v1/device_comm/update/pull?device_id=";
const char*  UPDATE_CHECK_PATH  = "/api/v1/device_comm/update?device_id=";
const char*  HEARTBEAT_PATH     = "/api/v1/device_comm/heartbeat";
const char*  FW_VERSION         = "3.0.0";

// ───────── Globals ────────────────────────────────────────────────────
Preferences         prefs;
WebServer           http(80);
DNSServer           dns;
TFT_eSPI            tft;
esp_adc_cal_characteristics_t* adc_chars;

String  g_ssid, g_pass, g_apPass, g_deviceId;
bool    g_wifiConnected = false;

unsigned long lastHeartbeat    = 0;
unsigned long lastUpdateCheck  = 0;
const unsigned long HB_INTERVAL     = 60UL * 1000;        // 1 min
const unsigned long UPDATE_INTERVAL = 60UL * 60UL * 1000; // 1 hr

float   pHValue  = -1.0;
float   tdsValue = -1.0;

// ───────── Helpers ────────────────────────────────────────────────────
void showStatus(const String& msg) {
  tft.fillScreen(TFT_BLACK);
  tft.setCursor(10, 100);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.println(msg);
}

void savePref(const char* key, const String& v) {
  prefs.begin("doser_cfg", false);
  prefs.putString(key, v);
  prefs.end();
}

void executePump(int pump, int amount) {
  int pin = RELAY_PUMP_1 + (pump - 1);
  if (pump < 1 || pump > 4) return;
  digitalWrite(pin, LOW);
  showStatus("Pumping #" + String(pump));
  delay(amount * 100);
  digitalWrite(pin, HIGH);
  showStatus("Done");
}

// ───────── Cloud OTA & Heartbeat ─────────────────────────────────────
void checkForUpdate() {
  if (!g_wifiConnected) return;
  HTTPClient client;
  client.begin(String(CLOUD_HOST) + UPDATE_CHECK_PATH + g_deviceId);
  int code = client.GET();
  if (code == HTTP_CODE_OK) {
    String body = client.getString();
    if (body.indexOf("\"update_available\":true") >= 0) {
      ESPhttpUpdate.update(String(CLOUD_HOST) + OTA_PATH + g_deviceId);
    }
  }
  client.end();
}

void sendHeartbeat() {
  if (!g_wifiConnected) return;
  HTTPClient client;
  client.begin(String(CLOUD_HOST) + HEARTBEAT_PATH);
  client.addHeader("Content-Type", "application/json");
  StaticJsonDocument<256> doc;
  doc["device_id"] = g_deviceId;
  doc["version"]   = FW_VERSION;
  String out; serializeJson(doc, out);
  int code = client.POST(out);
  if (code == HTTP_CODE_OK) {
    String resp = client.getString();
    StaticJsonDocument<512> r;
    if (deserializeJson(r, resp) == DeserializationError::Ok) {
      for (auto task : r["tasks"].as<JsonArray>()) {
        int p = task["pump"], a = task["amount"];
        executePump(p, a);
      }
    }
  }
  client.end();
}

// ───────── Web UI ─────────────────────────────────────────────────────
String htmlHeader(const String& title) {
  String s = "<!doctype html><html><head><meta charset='utf-8' "
             "name='viewport' content='width=device-width,initial-scale=1'>"
             "<title>" + title + "</title><style>"
             "body{font-family:Arial;margin:0;padding:20px;}h2{margin-top:0;}"
             "input,button{width:100%;padding:10px;margin:8px 0;box-sizing:border-box;font-size:16px;}"
             "button{background:#007bff;border:none;color:#fff;cursor:pointer;}button:hover{background:#0069d9}"
             "a{display:block;margin:8px 0;color:#007bff;text-decoration:none;}a:hover{text-decoration:underline}"
             "</style></head><body>";
  return s;
}

void handleMenu() {
  String p = htmlHeader("Hydroleaf Controller");
  p += "<h2>Main Menu</h2><ul>"
       "<li><a href='/wifi'>Wi-Fi Setup</a></li>"
       "<li><a href='/dosing'>Manual Dosing</a></li>"
       "<li><a href='/sensor'>Sensor Readings</a></li>"
       "<li><a href='/update'>Firmware Update</a></li>"
       "<li><a href='/reset'>Factory Reset</a></li>"
       "</ul></body></html>";
  http.send(200, "text/html", p);
}

void handleWiFiPage() {
  String p = htmlHeader("Wi-Fi Setup");
  p += "<h2>Enter Credentials</h2><form action='/save_wifi'>"
       "SSID:<input name='ssid' value='" + g_ssid + "'>"
       "Password:<input type='password' name='pass' value='" + g_pass + "'>"
       "<button type='submit'>Save & Restart</button></form>"
       "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleSaveWiFi() {
  if (!http.hasArg("ssid") || !http.hasArg("pass")) {
    http.send(400, "text/plain", "Missing fields");
    return;
  }
  g_ssid = http.arg("ssid");
  g_pass = http.arg("pass");
  savePref("ssid", g_ssid);
  savePref("pass", g_pass);
  http.send(200, "text/html", "<h2>Saved! Restarting…</h2>");
  delay(1000);
  ESP.restart();
}

void handleDosingPage() {
  String p = htmlHeader("Manual Dosing");
  p += "<h2>Start a Pump</h2><form action='/dose'>"
       "Pump (1–4):<input name='pump' type='number' min='1' max='4'>"
       "Amount (ms):<input name='amount' type='number' min='1'>"
       "<button type='submit'>Start</button></form>"
       "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleDose() {
  if (!http.hasArg("pump") || !http.hasArg("amount")) {
    http.send(400, "application/json", "{\"error\":\"Missing parameters\"}");
    return;
  }
  int p = http.arg("pump").toInt();
  int a = http.arg("amount").toInt();
  if (p < 1 || p > 4 || a <= 0) {
    http.send(400, "application/json", "{\"error\":\"Invalid pump/amount\"}");
    return;
  }
  executePump(p, a);
  http.send(200, "application/json", "{\"message\":\"Pump started\"}");
}

void handleSensorPage() {
  // quick refresh
  static unsigned long lastSensor = 0;
  if (millis() - lastSensor > 1000) {
    // read twice/sec
    // pH
    uint32_t sum=0;
    for(int i=0;i<30;i++) sum += analogRead(PH_SENSOR_PIN);
    float voltage = esp_adc_cal_raw_to_voltage(sum/30, adc_chars)/1000.0;
    pHValue = constrain(7.0 + ((2.5 - voltage)/0.18), 0.0, 14.0);
    // TDS
    sum=0;
    for(int i=0;i<30;i++) sum += analogRead(TDS_SENSOR_PIN);
    float v2 = esp_adc_cal_raw_to_voltage(sum/30, adc_chars)/1000.0;
    float cvolt = v2; cvolt *= (100.0/110.0); cvolt -= 0.12; if(cvolt<0) cvolt=0;
    float rawT = 133.42*pow(cvolt,3) - 255.86*pow(cvolt,2) + 857.39*cvolt;
    // simple correction
    tdsValue = rawT<0?0:(rawT>2500?2500:rawT);
    lastSensor = millis();
  }
  String p = htmlHeader("Sensor Readings");
  p += "<h2>pH: "   + String(pHValue, 2) + "</h2>"
       "<h2>TDS: " + String(tdsValue, 0) + " ppm</h2>"
       "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleUpdatePage() {
  String p = htmlHeader("Firmware Update");
  p += "<h2>Upload New Firmware</h2>"
       "<form method='POST' action='/update_firmware' enctype='multipart/form-data'>"
       "<input type='file' name='firmware'><br>"
       "<button type='submit'>Upload</button></form>"
       "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleFirmwareUpload() {
  HTTPUpload& upload = http.upload();
  if (upload.status == UPLOAD_FILE_START) {
    uint32_t maxSz = ESP.getFreeSketchSpace();
    if (!Update.begin(maxSz)) {
      http.send(500, "text/plain", "OTA begin failed");
    }
  }
  else if (upload.status == UPLOAD_FILE_WRITE) {
    Update.write(upload.buf, upload.currentSize);
  }
  else if (upload.status == UPLOAD_FILE_END) {
    if (Update.end(true)) {
      http.send(200, "text/plain", "Update Success; restarting");
      delay(500);
      ESP.restart();
    } else {
      http.send(500, "text/plain", "Update failed");
    }
  }
}

void handleReset() {
  savePref("apPass", "");
  http.send(200, "text/html", "<h2>Reset! Restarting…</h2>");
  delay(500);
  ESP.restart();
}

void handleNotFound() {
  http.sendHeader("Location", "/", true);
  http.send(302, "text/plain", "");
}

void setupRoutes() {
  http.on("/",            HTTP_GET,    handleMenu);
  http.on("/wifi",        HTTP_GET,    handleWiFiPage);
  http.on("/save_wifi",   HTTP_GET,    handleSaveWiFi);
  http.on("/dosing",      HTTP_GET,    handleDosingPage);
  http.on("/dose",        HTTP_GET,    handleDose);
  http.on("/sensor",      HTTP_GET,    handleSensorPage);
  http.on("/update",      HTTP_GET,    handleUpdatePage);
  http.on("/update_firmware", HTTP_POST, [](){}, handleFirmwareUpload);
  http.on("/reset",       HTTP_GET,    handleReset);
  http.onNotFound(handleNotFound);
  http.begin();
}

void portalStart() {
  WiFi.mode(WIFI_AP_STA);
  IPAddress apIP(192,168,0,1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255,255,255,0));
  WiFi.softAP(g_deviceId.c_str(), g_apPass.c_str());
  dns.start(DNS_PORT, "*", apIP);
  setupRoutes();
}

// ───────── Setup & Loop ─────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  tft.init(); tft.setRotation(1);

  // Relays & LED
  pinMode(LED_STATUS, OUTPUT); digitalWrite(LED_STATUS, LOW);
  for (int r : {RELAY_PUMP_1, RELAY_PUMP_2, RELAY_PUMP_3, RELAY_PUMP_4}) {
    pinMode(r, OUTPUT); digitalWrite(r, HIGH);
  }

  // Load prefs
  prefs.begin("doser_cfg", false);
  g_ssid   = prefs.getString("ssid", "");
  g_pass   = prefs.getString("pass", "");
  g_apPass = prefs.getString("apPass", DEFAULT_AP_PASS);
  prefs.end();

  // Unique ID
  g_deviceId = "DOSER_" + String((uint32_t)esp_random(), HEX);

  // ADC calibration
  adc_chars = (esp_adc_cal_characteristics_t*) calloc(1, sizeof(*adc_chars));
  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, 1100, adc_chars);
  analogSetPinAttenuation(PH_SENSOR_PIN, ADC_11db);
  analogSetPinAttenuation(TDS_SENSOR_PIN, ADC_11db);

  // Try Wi-Fi
  showStatus("Connecting WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(g_ssid.c_str(), g_pass.c_str());
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 10000) {
    delay(200);
  }
  g_wifiConnected = (WiFi.status() == WL_CONNECTED);
  showStatus(g_wifiConnected ? "WiFi Connected" : "WiFi Failed. AP Mode");
  portalStart();
}

void loop() {
  dns.processNextRequest();
  http.handleClient();

  unsigned long now = millis();
  if (now - lastHeartbeat >= HB_INTERVAL) {
    lastHeartbeat = now;
    sendHeartbeat();
  }
  if (now - lastUpdateCheck >= UPDATE_INTERVAL) {
    lastUpdateCheck = now;
    checkForUpdate();
  }
}
