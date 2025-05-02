/*****************************************************************************************
 *  HYDROLEAF SMART-CAM  –  Production Firmware  v2.5  (27-Apr-2025)
 *  Target  : ESP32-CAM (AI Thinker) – 4 MB flash, PSRAM enabled
 *  Author  : ChatGPT (OpenAI o4-mini)
 *
 *  • Camera-ID auto-generated once (date/time + random) and persisted in NVS.
 *  • AP SSID = Camera-ID, never user-configurable.
 *  • AP password stored in NVS and resettable via captive portal.
 *  • Always-on captive portal (AP+STA) at **192.168.0.1** for Wi-Fi switch,
 *    AP-password reset, subscription activation & manual OTA.
 *  • Full production-grade: day/night, chunked frame upload, cloud-driven OTA.
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

// ───────── GPIO Map (AI-Thinker) ───────────────────────────────────────────
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
#define LED_STATUS 33  // heartbeat
#define BTN_CONFIG 4   // LOW = pressed
#define PIN_LDR 14     // LDR input
#define PIN_IRLED 12   // IR LED output
bool  g_camReady   = false;   
bool  g_streamNote = false;
// ───────── Backend & Portal Defaults ───────────────────────────────────────
static const char* BACKEND_HOST = "192.168.29.26";
static const uint16_t BACKEND_PORT = 3000;
static const char* BACKEND_UPLOAD_PREFIX = "/upload/";  
static const char* AUTH_PATH = "/api/v1/cloud/authenticate";
static const char* UPDATE_PULL_PREFIX = "/api/v1/device_comm/update/pull?device_id=";
// —— LDR filtering & hysteresis ——
#define LDR_SAMPLES           10       // number of readings to average
#define LDR_DARK_THRESHOLD    1500     // < this ⇒ switch to night
#define LDR_LIGHT_THRESHOLD   2500     // > this ⇒ switch back to day

uint16_t ldrBuf[LDR_SAMPLES] = {0};
uint8_t  ldrIdx             = 0;


static const char* DEFAULT_AP_PWD = "configme";
static const byte DNS_PORT = 53;
static bool otaSuccess = false;

Preferences prefs;
WebServer http(80);
DNSServer dns;
sensor_t* cam = nullptr;

#define LDR_INTERVAL_MS   (2UL * 60UL * 1000UL)

// ───────── Forward Declarations ────────────────────────────────────────────
void initCamera();
bool wifiConnect(uint8_t tries = 5);
void generateCameraId();
bool sendFrame();
void updateLDR();
void handleButton();
String htmlHeader(const char* title);
void setupRoutes();
void portalStart();
bool validateServerKey();
void checkCloudUpdate();
 String g_ssid, g_pass, g_camId, g_apPass, g_subKey;
 bool   g_night   = false;
 bool   g_activated = false;
 String g_token; 
// ───────── Helper: commit NVS String ───────────────────────────────────────
inline void commitPref(const char* key, const String& val) {
  prefs.begin("cam_cfg", false);
  prefs.putString(key, val);
  prefs.end();
}

// ───────── Heartbeat LED ───────────────────────────────────────────────────
const uint32_t BLINK_MS = 3000;
uint32_t lastBlink = 0;
inline void beatLED() {
  uint32_t now = millis();
  if (now - lastBlink < BLINK_MS) return;
  lastBlink = now;
  digitalWrite(LED_STATUS, HIGH);
  delay(20);
  digitalWrite(LED_STATUS, LOW);
}

// ───────── HTML + CSS header ───────────────────────────────────────────────
String htmlHeader(const char* title) {
  String h = "<!doctype html><html><head><meta charset='utf-8'>";
  h += "<meta name='viewport' content='width=device-width,initial-scale=1'>";
  h += "<title>";
  h += title;
  h += "</title><style>"
       "body{font-family:Arial;background:#f7f7f7;margin:0;padding:20px}h2{margin-top:0}"
       "input,button{width:100%;padding:10px;margin:8px 0;box-sizing:border-box;font-size:16px}"
       "button{background:#007bff;border:none;color:#fff;cursor:pointer}"
       "button:hover{background:#0069d9}"
       "a{display:block;margin:8px 0;color:#007bff;text-decoration:none}"
       "a:hover{text-decoration:underline}"
       "</style></head><body>";
  return h;
}

// ───────── Captive-Portal Handlers ─────────────────────────────────────────

// Subscription page
void handleSubscribePage() {
  String p = htmlHeader("Activate Camera");
  p += "<h2>Enter Subscription Key</h2><form action='/save_subscribe'>"
       "Key:<input name='key' value='"
       + g_subKey + "'>"
                    "<button type='submit'>Activate</button></form>"
                    "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

// Save & validate subscription key
void handleSubscribeSave() {

  if (!http.hasArg("key")) {
    http.send(400, "text/plain", "Missing subscription key");
    return;
  }

  g_subKey = http.arg("key");
  commitPref("subKey", g_subKey);

  // make sure we have an ID (first boot, Wifi might be off)
  if (g_camId.isEmpty()) generateCameraId();

  bool ok = false;
  bool haveWifi = (WiFi.status() == WL_CONNECTED);

  if (haveWifi) {
    ok = validateServerKey();  // online check
  }

  if (ok || !haveWifi) {  // accept key even if offline
    g_activated = true;
    commitBool("activated", true);  //  ⬅️  persist the flag
    http.send(200, "text/html", "<h1>Activated! Restarting…</h1>");
    delay(1000);
    ESP.restart();
  } else {
    http.send(200, "text/html",
              "<h1>Invalid key!</h1><a href='/subscribe'>Try again</a>");
  }
}

// Wi-Fi form
void handleWiFiPage() {
  String p = htmlHeader("Wi-Fi Setup");
  p += "<h2>Select Wi-Fi Network</h2><form action='/save_wifi'>"
       "SSID:<input name='ssid' value='"
       + g_ssid + "'>"
                  "Password:<input type='password' name='pass' value='"
       + g_pass + "'>"
                  "<button type='submit'>Save &amp; Restart</button></form>"
                  "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

// AP-Password form
void handleAPPage() {
  String p = htmlHeader("Hotspot Password");
  p += "<h2>Reset AP Password</h2><form action='/reset_ap'>"
       "New Password:<input type='password' name='apass' value='"
       + g_apPass + "'>"
                    "<button type='submit'>Update</button></form>"
                    "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

// Save Wi-Fi & restart
void handleSaveWiFi() {
  if (!http.hasArg("ssid") || !http.hasArg("pass")) {
    http.send(400, "text/plain", "Missing fields");
    return;
  }
  g_ssid = http.arg("ssid");
  commitPref("ssid", g_ssid);
  g_pass = http.arg("pass");
  commitPref("pass", g_pass);
  http.send(200, "text/html", "<h1>Saved! Restarting…</h1>");
  delay(1000);
  ESP.restart();
}

// Save AP-Password (no restart)
void handleResetAP() {
  if (!http.hasArg("apass")) {
    http.send(400, "text/plain", "Missing password");
    return;
  }
  g_apPass = http.arg("apass");
  commitPref("apPass", g_apPass);
  dns.stop();
  http.stop();
  portalStart();
  http.send(200, "text/html", "<h1>AP Password Updated!</h1><a href='/'>Back</a>");
}

// Redirect all other paths to portal
void handleNotFound() {
  http.sendHeader("Location", "http://192.168.0.1", true);
  http.send(302);
}
void handleStatus();


// Build routes
void setupRoutes() {
  http.on("/", HTTP_GET, handleMenu);
  http.on("/subscribe", HTTP_GET, handleSubscribePage);
  http.on("/save_subscribe", HTTP_GET, handleSubscribeSave);
  http.on("/wifi", HTTP_GET, handleWiFiPage);
  http.on("/ap_password", HTTP_GET, handleAPPage);
  http.on("/save_wifi", HTTP_GET, handleSaveWiFi);
  http.on("/reset_ap", HTTP_GET, handleResetAP);
  http.on("/status",   HTTP_GET, handleStatus);
  // OTA upload endpoint (fixed IP)
  http.on(
    "/update_firmware", HTTP_POST,
    []() {
      if (otaSuccess) http.send(200, "text/plain", "DONE");
      else http.send(500, "text/plain", "FAIL");
      otaSuccess = false;
      delay(100);
      ESP.restart();
    },
    []() {
      HTTPUpload& up = http.upload();
      switch (up.status) {
        case UPLOAD_FILE_START:
          if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
          break;
        case UPLOAD_FILE_WRITE:
          if (Update.write(up.buf, up.currentSize) != up.currentSize) Update.printError(Serial);
          break;
        case UPLOAD_FILE_END:
          otaSuccess = Update.end(true);
          break;
        case UPLOAD_FILE_ABORTED:
          otaSuccess = false;
          break;
      }
    });

  http.onNotFound(handleNotFound);
  http.begin();
}

// Start captive portal (AP+STA) at fixed IP 192.168.0.1
void portalStart() {
  Serial.println("[CFG] starting portal");
  WiFi.mode(WIFI_AP_STA);

  IPAddress apIP(192, 168, 0, 1);   // CHANGED
  IPAddress gwIP(192, 168, 0, 1);
  IPAddress netmask(255, 255, 255, 0);
  WiFi.softAPConfig(apIP, gwIP, netmask);

  WiFi.softAP(g_camId.length() ? g_camId.c_str() : DEFAULT_AP_PWD,
              g_apPass.c_str());
  dns.start(DNS_PORT, "*", apIP);

  setupRoutes();
}

// ───────── Camera & Streaming ──────────────────────────────────────────────
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
  cfg.xclk_freq_hz = 20000000;
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
  g_camReady = true; 
}

// Wi-Fi with backoff
bool wifiConnect(uint8_t tries) {
  WiFi.mode(WIFI_STA);
  while (tries--) {
    Serial.printf("[NET] connect '%s'\n", g_ssid.c_str());
    WiFi.begin(g_ssid.c_str(), g_pass.c_str());
    for (int i = 0; i < 50; i++) {
      if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[NET] IP %s RSSI %d\n",
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
      }
      delay(200);
    }
    WiFi.disconnect(true);
    delay(200);
  }
  return false;
}

// Generate & persist Camera-ID once
void generateCameraId() {
  prefs.begin("cam_cfg", false);
  String stored = prefs.getString("camId", "");
  if (stored.length()) {
    g_camId = stored;
    prefs.end();
    return;
  }
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  struct tm tm;
  if (!getLocalTime(&tm, 5000)) {
    g_camId = "CAM_" + String((uint32_t)esp_random(), HEX);
  } else {
    char buf[32];
    strftime(buf, sizeof(buf), "CAM_%Y%m%d_%H%M%S", &tm);
    g_camId = String(buf) + "_" + String((uint32_t)esp_random(), HEX);
  }
  prefs.putString("camId", g_camId);
  prefs.end();
}

// Upload frame in chunks
bool sendFrame() {
  if (!g_camReady) return false;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return false;

  // build URL
  String url = String("http://") + BACKEND_HOST + ":" + BACKEND_PORT
               + BACKEND_UPLOAD_PREFIX + g_camId + (g_night?"/night":"/day");
  HTTPClient httpc;
  httpc.begin(url);
  if (g_token.length()) {
    httpc.addHeader("Authorization", "Bearer " + g_token);
  }
  httpc.addHeader("Content-Type", "image/jpeg");

  int code = httpc.POST(fb->buf, fb->len);
  httpc.end();
  esp_camera_fb_return(fb);

  // handle 401 → clear token & require fresh auth
  if (code == 401) {
    g_token = "";
    commitPref("token", "");
    g_activated = false;
    commitBool("activated", false);
  }
  return (code >= 200 && code < 300);
}

void updateLDR() {
  static unsigned long lastLDR = 0;
  unsigned long now = millis();
  if (now - lastLDR < LDR_INTERVAL_MS) return;
  lastLDR = now;

  // 1) temporarily disable IR to avoid reflection
  bool wasNight = g_night;
  digitalWrite(PIN_IRLED, LOW);
  delay(50);                      // let LDR settle
  uint32_t raw = analogRead(PIN_LDR);
  // restore IR if we’re currently in night mode
  if (wasNight) digitalWrite(PIN_IRLED, HIGH);

  // 2) sliding-window average
  ldrBuf[ldrIdx++] = raw;
  if (ldrIdx >= LDR_SAMPLES) ldrIdx = 0;
  uint32_t sum = 0;
  for (uint8_t i = 0; i < LDR_SAMPLES; i++) sum += ldrBuf[i];
  uint16_t avg = sum / LDR_SAMPLES;

  // 3) hysteresis
  if (!wasNight && avg < LDR_DARK_THRESHOLD) {
    // → switch to night
    g_night = true;
    digitalWrite(PIN_IRLED, HIGH);
    cam->set_whitebal(cam, 0);
    cam->set_awb_gain(cam, 0);
    cam->set_brightness(cam,  2);
    cam->set_contrast(cam,    2);
    cam->set_saturation(cam, -1);
    cam->set_denoise(cam,     7);
  }
  else if (wasNight && avg > LDR_LIGHT_THRESHOLD) {
    // → switch back to day
    g_night = false;
    digitalWrite(PIN_IRLED, LOW);

    prefs.begin("cam_cfg", false);
    cam->set_brightness(cam, prefs.getInt("bright", 1));
    cam->set_contrast(cam,   prefs.getInt("contr", 1));
    cam->set_saturation(cam, prefs.getInt("sat",   1));
    cam->set_denoise(cam,    prefs.getInt("dn",    5));
    prefs.end();

    cam->set_whitebal(cam, 1);
    cam->set_awb_gain(cam, 1);
  }
}

// Config button: short=portal toggle, long=factory reset
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
    } else if (held >= 100) {
      portalStart();
    }
  }
}

// Validate subscription key with cloud
bool validateServerKey() {
  if (g_camId.isEmpty() || g_subKey.isEmpty()) return false;
  HTTPClient httpc;
  String url = String("http://") + BACKEND_HOST + ":" + String(BACKEND_PORT) + AUTH_PATH;
  httpc.begin(url);
  httpc.addHeader("Content-Type", "application/json");
  String body = String("{\"device_id\":\"") + g_camId + String("\",\"cloud_key\":\"") + g_subKey + "\"}";
  int code = httpc.POST(body);
  String resp = httpc.getString();
  httpc.end();
  if (code == 200) {
    // parse {"token":"…"} and persist
    DynamicJsonDocument doc(256);
    if (!deserializeJson(doc, resp) && doc["token"].is<String>()) {
      g_token = doc["token"].as<String>();
      commitPref("token", g_token);
      commitBool("activated", true);
      return true;
    }
  }
  return false;
}

// Check & perform OTA from cloud
void checkCloudUpdate() {
  if (!WiFi.isConnected() || !g_activated) return;

  HTTPClient http;
  String url = "http://" + String(BACKEND_HOST) + String(UPDATE_PULL_PREFIX) + g_camId;


  Serial.printf("[OTA] Checking %s\n", url.c_str());

  http.begin(url);
  int httpCode = http.GET();

  if (httpCode == 200) {
    int len = http.getSize();
    WiFiClient* stream = http.getStreamPtr();

    if (!Update.begin(len == 0 ? UPDATE_SIZE_UNKNOWN : len)) {
      Update.printError(Serial);
      http.end();
      return;
    }

    uint8_t buff[128] = { 0 };
    int written = 0;
    while (http.connected() && (written < len || len == 0)) {
      size_t available = stream->available();
      if (available) {
        size_t readBytes = stream->readBytes(buff, ((available > sizeof(buff)) ? sizeof(buff) : available));
        Update.write(buff, readBytes);
        written += readBytes;
      }
      delay(1);
    }

    if (Update.end()) {
      if (Update.isFinished()) {
        Serial.println("[OTA] Update successful. Rebooting...");
        delay(500);
        ESP.restart();
      } else {
        Serial.println("[OTA] Update failed (not finished).");
      }
    } else {
      Update.printError(Serial);
    }
  } else if (httpCode == 304) {
    Serial.println("[OTA] No update available.");
  } else {
    Serial.printf("[OTA] HTTP error %d\n", httpCode);
  }

  http.end();
}
inline void commitBool(const char* key, bool val) {
  prefs.begin("cam_cfg", false);
  prefs.putBool(key, val);
  prefs.end();
}
void handleMenu() {
  String p = htmlHeader("Config Menu");
  p += "<h2>Settings</h2><ul>"
       "<li><a href='/status'>Device Status</a></li>"
       "<li><a href='/subscribe'>Activate Camera</a></li>"
       "<li><a href='/wifi'>Change Wi-Fi</a></li>"
       "<li><a href='/ap_password'>Reset AP Password</a></li>"
       "</ul></body></html>";
  http.send(200, "text/html", p);
}

void handleStatus() {
  String cssid = (WiFi.status()==WL_CONNECTED? WiFi.SSID() : "—");
  String iconConn = (WiFi.status()==WL_CONNECTED? "Connected" : "Offline");
  String haveToken = (g_token.length()? "Yes" : "No");
  String isActive  = (g_activated?   "Streaming" : "Idle");

  String p = htmlHeader("Device Status");
  p += "<h2>Status</h2>"
       "<p>Wi-Fi SSID: <b>" + cssid + "</b> (" + iconConn + ")</p>"
       "<p>Token Avail: <b>" + haveToken+ "</b></p>"
       "<p>Streaming:   <b>" + isActive  + "</b></p>"
       "<a href='/'>← Back</a></body></html>";
  http.send(200, "text/html", p);
}

// ───────── Setup & Loop ─────────────────────────────────────────────────────
// ───────── Setup ───────────────────────────────────────────────────────────
void setup() {
  /* ── basic GPIO & serial ──────────────────────────────────────────────── */
  Serial.begin(115200);

  pinMode(LED_STATUS, OUTPUT);   digitalWrite(LED_STATUS, LOW);
  pinMode(BTN_CONFIG, INPUT_PULLUP);
  pinMode(PIN_LDR,    INPUT);     
  pinMode(PIN_IRLED,  OUTPUT);   digitalWrite(PIN_IRLED, LOW);

  g_camReady   = false;          // camera not initialised yet
  g_streamNote = false;          // one-shot stream banner

  /* ── load persisted settings ──────────────────────────────────────────── */
  prefs.begin("cam_cfg", false);
  g_ssid      = prefs.getString("ssid",   "");
  g_pass      = prefs.getString("pass",   "");
  g_apPass    = prefs.getString("apPass", DEFAULT_AP_PWD);
  g_subKey    = prefs.getString("subKey", "");
  g_activated = prefs.getBool  ("activated", false);
  g_token     = prefs.getString("token",   "");
  prefs.end();

  /* ── Camera-ID must exist before any cloud calls ──────────────────────── */
  generateCameraId();

  /* ── try STA connection first (if credentials exist) ─────────────────── */
  bool wifiOk = g_ssid.length() && wifiConnect();

  if (wifiOk) {
    // 1) if we already have a valid token, trust it
    if (g_token.length() > 0) {
      g_activated = true;
    }

    // 2) otherwise, if not activated yet but we have a subKey, try authenticate
    if (!g_activated && g_subKey.length()) {
      if (validateServerKey()) {
        // validateServerKey() will persist both token and "activated"
        Serial.println("[ACT] subscription validated");
      } else {
        Serial.println("[ACT] invalid stored key");
      }
    }

    // 3) if activated (via token or fresh auth), start camera & OTA
    if (g_activated) {
      initCamera();                       // sets g_camReady = true
      Serial.println("[SETUP] ready to stream");
      checkCloudUpdate();                 // one shot OTA check at boot
    } else {
      Serial.println("[SETUP] waiting for activation");
    }
  } else {
    Serial.println("[SETUP] Wi-Fi connect failed");
  }

  /* ── Always launch AP+portal (STA will stay alive if already connected) ─ */
  portalStart();
}


void loop() {
  handleButton();
  updateLDR();
  beatLED();

  static unsigned long lastFrame = 0;
  if (millis() - lastFrame > 1000 && g_activated && WiFi.status() == WL_CONNECTED) {
    if (sendFrame()) {
      Serial.println("[FRAME] sent");
    } else {
      Serial.println("[FRAME] failed");
    }
    lastFrame = millis();
  }

  dns.processNextRequest();
  http.handleClient();
}

