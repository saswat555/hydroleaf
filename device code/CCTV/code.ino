/*****************************************************************************************
 *  HYDROLEAF SMART-CAM  –  Production Firmware  v2.6  (02‑May‑2025)
 *  Target  : ESP32‑CAM (AI‑Thinker) – 4 MB flash, PSRAM enabled
 *  Author  : ChatGPT (OpenAI o3)
 *
 *  CHANGES (v2.6)
 *  ────────────────────────────────────────────────────────────────────────────
 *  • Auto‑activation – camera authenticates itself on first Wi‑Fi connect using
 *    the hard‑coded Cloud Key, obtains JWT and starts streaming immediately.
 *  • Removed manual subscription UI + routes (simpler captive portal ↔ Wi‑Fi).
 *  • Hard‑coded CLOUD_KEY constant; persisted token still honoured to avoid
 *    re‑auth on every reboot.
 *  • Leaned‑out includes & helper prototypes; no functional loss.
 *  • Consistent naming + formatting pass.
 *****************************************************************************************/

#pragma GCC optimize("Os")
#include <ArduinoJson.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <esp_camera.h>
#include <time.h>
#include <Update.h>
#include <HTTPClient.h>

/* ───────────────────────────── GPIO MAP (AI‑Thinker) ───────────────────── */
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22
#define LED_STATUS 33  // Heart‑beat LED
#define BTN_CONFIG 4   // LOW = pressed (setup/reset)
#define PIN_LDR 14     // LDR input
#define PIN_IRLED 12   // IR‑LED output

/* ───────────────────────────── Cloud End‑points ─────────────────────────── */
static const char* BACKEND_HOST = "cloud.hydroleaf.in";
static const uint16_t BACKEND_PORT = 3000;
static const char* BACKEND_UPLOAD_PREFIX = "/upload/";        // +<camId>/<day|night>
static const char* AUTH_PATH = "/api/v1/cloud/authenticate";  // POST → {token}
static const char* UPDATE_PULL_PREFIX = "/api/v1/device_comm/update/pull?device_id=";

static const char* CLOUD_KEY = "371688b7edd0dbf049c5344ead7f4c6a";  // 🔒 Hard‑coded key

/* ───────────────────────────── Runtime Globals ─────────────────────────── */
Preferences prefs;
WebServer http(80);
DNSServer dns;
sensor_t* cam = nullptr;
bool camReady = false;
bool nightMode = false;
String ssid, pass, apPass, camId, jwt;
bool activated = false;

/* ───────────────────────────── Streaming Timers ─────────────────────────── */
#define WIFI_RETRY_MS (30UL * 1000UL)
#define FRAME_INTERVAL_MS 1000UL
static unsigned long lastWifiTry = 0;
static unsigned long lastFrameTx = 0;

/* ───────────────────────────── Forward Decl. ────────────────────────────── */
void initCamera();
bool wifiConnect(uint8_t tries = 5);
void generateCameraId();
bool cloudAuthenticate();
bool sendFrame();
void updateLDR();
void handleButton();
void portalStart();
void checkCloudUpdate();

/* ───────────────────────────── Helper: NVS commit ───────────────────────── */
inline void commitPref(const char* key, const String& val) {
  prefs.begin("cam_cfg", false);
  prefs.putString(key, val);
  prefs.end();
}
inline void commitBool(const char* key, bool val) {
  prefs.begin("cam_cfg", false);
  prefs.putBool(key, val);
  prefs.end();
}

/* ───────────────────────────── LED Heart‑beat ───────────────────────────── */
inline void beatLED() {
  static uint32_t last = 0;
  if (millis() - last < 3000) return;
  last = millis();
  digitalWrite(LED_STATUS, HIGH);
  delay(20);
  digitalWrite(LED_STATUS, LOW);
}

/* ───────────────────────────── Captive Portal ───────────────────────────── */
static const char* DEFAULT_AP_PWD = "configme";
static const byte DNS_PORT = 53;

String htmlHeader(const char* title) {
  String h = "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
  h += "<title>" + String(title) + "</title><style>body{font-family:Arial;background:#f7f7f7;margin:0;padding:20px}h2{margin-top:0}input,button{width:100%;padding:10px;margin:8px 0;box-sizing:border-box;font-size:16px}button{background:#007bff;border:none;color:#fff;cursor:pointer}button:hover{background:#0069d9}a{display:block;margin:8px 0;color:#007bff;text-decoration:none}a:hover{text-decoration:underline}</style></head><body>";
  return h;
}

void handleWiFiPage() {
  String p = htmlHeader("Wi‑Fi Setup");
  p += "<h2>Select Wi‑Fi Network</h2><form action='/save_wifi'>SSID:<input name='ssid' value='" + ssid + "'>Password:<input type='password' name='pass' value='" + pass + "'><button type='submit'>Save &amp; Restart</button></form><a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleSaveWiFi() {
  if (!http.hasArg("ssid") || !http.hasArg("pass")) {
    http.send(400, "text/plain", "Missing fields");
    return;
  }
  ssid = http.arg("ssid");
  commitPref("ssid", ssid);
  pass = http.arg("pass");
  commitPref("pass", pass);
  http.send(200, "text/html", "<h1>Saved! Restarting…</h1>");
  delay(1000);
  ESP.restart();
}

void handleAPPage() {
  String p = htmlHeader("Hotspot Password");
  p += "<h2>Reset AP Password</h2><form action='/reset_ap'>New Password:<input type='password' name='apass' value='" + apPass + "'><button type='submit'>Update</button></form><a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleResetAP() {
  if (!http.hasArg("apass")) {
    http.send(400, "text/plain", "Missing password");
    return;
  }
  apPass = http.arg("apass");
  commitPref("apPass", apPass);
  dns.stop();
  http.stop();
  portalStart();
  http.send(200, "text/html", "<h1>AP Password Updated!</h1><a href='/'>Back</a>");
}

void handleMenu() {
  String p = htmlHeader("Config Menu");
  p += "<h2>Settings</h2><ul><li><a href='/status'>Device Status</a></li><li><a href='/wifi'>Change Wi‑Fi</a></li><li><a href='/ap_password'>Reset AP Password</a></li></ul></body></html>";
  http.send(200, "text/html", p);
}

void handleStatus() {
  String cssid = (WiFi.status() == WL_CONNECTED ? WiFi.SSID() : "—");
  String conn = (WiFi.status() == WL_CONNECTED ? "Connected" : "Offline");
  String haveJWT = (jwt.length() ? "Yes" : "No");
  String stream = (activated && camReady ? "Streaming" : "Idle");
  String p = htmlHeader("Device Status");
  p += "<h2>Status</h2><p>Wi‑Fi SSID: <b>" + cssid + "</b> (" + conn + ")</p><p>Token Avail: <b>" + haveJWT + "</b></p><p>State: <b>" + stream + "</b></p><a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

void handleNotFound() {
  http.sendHeader("Location", "http://192.168.0.1", true);
  http.send(302);
}

void setupRoutes() {
  http.on("/", HTTP_GET, handleMenu);
  http.on("/wifi", HTTP_GET, handleWiFiPage);
  http.on("/save_wifi", HTTP_GET, handleSaveWiFi);
  http.on("/ap_password", HTTP_GET, handleAPPage);
  http.on("/reset_ap", HTTP_GET, handleResetAP);
  http.on("/status", HTTP_GET, handleStatus);
  http.onNotFound(handleNotFound);
  http.begin();
}

void portalStart() {
  WiFi.mode(WIFI_AP_STA);
  IPAddress apIP(192, 168, 0, 1), gwIP(192, 168, 0, 1), netmask(255, 255, 255, 0);
  WiFi.softAPConfig(apIP, gwIP, netmask);
  WiFi.softAP(camId.c_str(), apPass.c_str());
  dns.start(DNS_PORT, "*", apIP);
  setupRoutes();
}

/* ───────────────────────────── Camera & Streaming ──────────────────────── */
void initCamera() {
  camera_config_t cfg = {};
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.ledc_timer = LEDC_TIMER_0;
  cfg.pin_d0 = Y2_GPIO_NUM;
  cfg.pin_d1 = Y3_GPIO_NUM;
  cfg.pin_d2 = Y4_GPIO_NUM;
  cfg.pin_d3 = Y5_GPIO_NUM;
  cfg.pin_d4 = Y6_GPIO_NUM;
  cfg.pin_d5 = Y7_GPIO_NUM;
  cfg.pin_d6 = Y8_GPIO_NUM;
  cfg.pin_d7 = Y9_GPIO_NUM;
  cfg.pin_xclk = XCLK_GPIO_NUM;
  cfg.pin_pclk = PCLK_GPIO_NUM;
  cfg.pin_vsync = VSYNC_GPIO_NUM;
  cfg.pin_href = HREF_GPIO_NUM;
  cfg.pin_sscb_sda = SIOD_GPIO_NUM;
  cfg.pin_sscb_scl = SIOC_GPIO_NUM;
  cfg.pin_pwdn = PWDN_GPIO_NUM;
  cfg.pin_reset = RESET_GPIO_NUM;
  cfg.xclk_freq_hz = 20'000'000;
  cfg.pixel_format = PIXFORMAT_JPEG;
  if (psramFound()) {
    cfg.frame_size = FRAMESIZE_HD;
    cfg.fb_count = 2;
    cfg.jpeg_quality = 12;
  } else {
    cfg.frame_size = FRAMESIZE_SVGA;
    cfg.fb_count = 1;
    cfg.jpeg_quality = 15;
  }
  if (esp_camera_init(&cfg) != ESP_OK) {
    Serial.println("[ERR] camera init failed");
    delay(500);
    ESP.restart();
  }
  cam = esp_camera_sensor_get();
  cam->set_hmirror(cam, 1);
  cam->set_vflip(cam, 0);
  camReady = true;
}

bool wifiConnect(uint8_t tries) {
  WiFi.mode(WIFI_STA);
  while (tries--) {
    Serial.printf("[NET] connect '%s'\n", ssid.c_str());
    WiFi.begin(ssid.c_str(), pass.c_str());
    for (int i = 0; i < 50; ++i) {
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[NET] IP %s RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
      }
      delay(200);
    }
    WiFi.disconnect(true);
    delay(200);
  }
  return false;
}

void generateCameraId() {
  prefs.begin("cam_cfg", false);
  camId = prefs.getString("camId", "");
  if (camId.length()) {
    prefs.end();
    return;
  }
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  struct tm tm;
  char buf[32] = "";
  if (getLocalTime(&tm, 5000)) {
    strftime(buf, sizeof(buf), "CAM_%Y%m%d_%H%M%S", &tm);
    camId = String(buf) + "_" + String((uint32_t)esp_random(), HEX);
  } else {
    camId = "CAM_" + String((uint32_t)esp_random(), HEX);
  }
  prefs.putString("camId", camId);
  prefs.end();
}

bool cloudAuthenticate() {
  HTTPClient http;
  String url = String("http://") + BACKEND_HOST + AUTH_PATH;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  String body = String("{\"device_id\":\"") + camId + "\",\"cloud_key\":\"" + CLOUD_KEY + "\"}";
  int code = http.POST(body);
  String resp = http.getString();
  http.end();
  if (code != 200) {
    Serial.printf("[AUTH] failed (%d)\n", code);
    return false;
  }
  DynamicJsonDocument doc(256);
  if (deserializeJson(doc, resp) != DeserializationError::Ok || !doc["token"].is<String>()) return false;
  jwt = doc["token"].as<String>();
  commitPref("token", jwt);
  commitBool("activated", true);
  activated = true;
  Serial.println("[AUTH] success");
  return true;
}

bool sendFrame() {
  if (!camReady) return false;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return false;
  String url = String("http://") + BACKEND_HOST + BACKEND_UPLOAD_PREFIX + camId + (nightMode ? "/night" : "/day");
  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "image/jpeg");
  if (jwt.length()) http.addHeader("Authorization", "Bearer " + jwt);
  int code = http.POST(fb->buf, fb->len);
  http.end();
  esp_camera_fb_return(fb);
  if (code == 401) {
    jwt = "";
    commitPref("token", "");
    activated = false;
    commitBool("activated", false);
  }
  return code >= 200 && code < 300;
}

/* ── Simple 5‑sample median switch between day/night ─────────────────────── */
uint8_t ldrBuf[5] = { 1, 1, 1, 1, 1 };
uint8_t ldrIdx = 0;
void updateLDR() {
  ldrBuf[ldrIdx++] = digitalRead(PIN_LDR);
  if (ldrIdx >= 5) ldrIdx = 0;
  int dark = 0;
  for (auto v : ldrBuf)
    if (v == LOW) ++dark;
  if (dark >= 4 && !nightMode) {
    nightMode = true;
    digitalWrite(PIN_IRLED, HIGH);
    cam->set_whitebal(cam, 0);
    cam->set_awb_gain(cam, 0);
    cam->set_brightness(cam, 2);
    cam->set_contrast(cam, 2);
    cam->set_saturation(cam, -1);
    cam->set_denoise(cam, 7);
  } else if (dark <= 1 && nightMode) {
    nightMode = false;
    digitalWrite(PIN_IRLED, LOW);
    prefs.begin("cam_cfg", false);
    cam->set_brightness(cam, prefs.getInt("bright", 1));
    cam->set_contrast(cam, prefs.getInt("contr", 1));
    cam->set_saturation(cam, prefs.getInt("sat", 1));
    cam->set_denoise(cam, prefs.getInt("dn", 5));
    prefs.end();
    cam->set_whitebal(cam, 1);
    cam->set_awb_gain(cam, 1);
  }
}

/* ── CONFIG button: short→portal, long(>3s)→factory reset ───────────────── */
void handleButton() {
  static unsigned long down = 0;
  bool pressed = digitalRead(BTN_CONFIG) == LOW;
  if (pressed && !down) down = millis();
  if (!pressed && down) {
    unsigned long held = millis() - down;
    down = 0;
    if (held >= 3000) {
      prefs.begin("cam_cfg", false);
      prefs.clear();
      prefs.end();
      delay(500);
      ESP.restart();
    } else if (held >= 100) portalStart();
  }
}

/* ───────────────────────────── OTA pull from Cloud ─────────────────────── */
void checkCloudUpdate() {
  if (!WiFi.isConnected() || !activated) return;
  HTTPClient http;
  String url = String("http://") + BACKEND_HOST + UPDATE_PULL_PREFIX + camId;
  http.begin(url);
  int code = http.GET();
  if (code != 200) {
    http.end();
    return;
  }
  int len = http.getSize();
  WiFiClient* stream = http.getStreamPtr();
  if (!Update.begin(len == 0 ? UPDATE_SIZE_UNKNOWN : len)) {
    Update.printError(Serial);
    http.end();
    return;
  }
  uint8_t buf[128];
  int written = 0;
  while (http.connected() && (written < len || len == 0)) {
    size_t avail = stream->available();
    if (avail) {
      size_t rd = stream->readBytes(buf, avail > sizeof(buf) ? sizeof(buf) : avail);
      Update.write(buf, rd);
      written += rd;
    }
    delay(1);
  }
  if (Update.end() && Update.isFinished()) {
    Serial.println("[OTA] Update OK → reboot");
    delay(500);
    ESP.restart();
  } else {
    Update.printError(Serial);
  }
  http.end();
}

/* ───────────────────────────── Arduino SETUP ───────────────────────────── */
void setup() {
  Serial.begin(115200);
  pinMode(LED_STATUS, OUTPUT);
  digitalWrite(LED_STATUS, LOW);
  pinMode(BTN_CONFIG, INPUT_PULLUP);
  pinMode(PIN_LDR, INPUT_PULLUP);
  pinMode(PIN_IRLED, OUTPUT);
  digitalWrite(PIN_IRLED, LOW);

  prefs.begin("cam_cfg", false);
  ssid = prefs.getString("ssid", "");
  pass = prefs.getString("pass", "");
  apPass = prefs.getString("apPass", DEFAULT_AP_PWD);
  jwt = prefs.getString("token", "");
  activated = prefs.getBool("activated", false);
  prefs.end();

  generateCameraId();

  bool wifiOk = ssid.length() && wifiConnect();
  if (wifiOk) {
    if (!activated) activated = cloudAuthenticate();
    if (activated && !camReady) {
      initCamera();
      checkCloudUpdate();
      Serial.println("[SETUP] Streaming enabled");
    } else if (!activated) Serial.println("[SETUP] Activation failed – portal open");
  } else Serial.println("[SETUP] Wi‑Fi connect failed – portal only");

  portalStart();
}

/* ───────────────────────────── Arduino LOOP ────────────────────────────── */
void loop() {
  handleButton();
  updateLDR();
  beatLED();

  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiTry > WIFI_RETRY_MS) {
    lastWifiTry = millis();
    Serial.println("[NET] Wi‑Fi lost, retrying…");
    if (wifiConnect() && activated && !camReady) {
      initCamera();
      Serial.println("[NET] Re‑init camera after reconnect");
    }
  }

  if (activated && WiFi.status() == WL_CONNECTED && camReady && millis() - lastFrameTx > FRAME_INTERVAL_MS) {
    if (sendFrame()) Serial.println("[TX] frame OK");
    else Serial.println("[TX] frame FAIL");
    lastFrameTx = millis();
  }

  dns.processNextRequest();
  http.handleClient();
}
