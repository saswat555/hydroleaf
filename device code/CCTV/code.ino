/*****************************************************************************************
 *  HYDROLEAF SMART-CAM  –  Production Firmware  v4.3
 *  Target  : ESP32-CAM (AI-Thinker) – 4 MB flash, PSRAM enabled
 *  Author  : ChatGPT (o4-mini)
 *
 *  • Always-on AP + captive portal on 192.168.0.1  
 *  • /wifi & /save_wifi for credentials  
 *  • /status for IPs & cloud auth state  
 *  • /logs, /, OTA push, day/night LDR, HTTP frame streaming (~30 FPS)  
 *****************************************************************************************/

#include <ArduinoJson.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <esp_camera.h>
#include <Update.h>
#include <time.h>
#include <HTTPClient.h>
#include <esp_wifi.h>
// ───────── GPIO MAP (AI-Thinker) ─────────
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

#define LED_STATUS 33
#define BTN_CONFIG 4
#define PIN_LDR 14
#define PIN_IRLED 12

#define DEFAULT_CLOUD_KEY ""

// ───────── CLOUD ENDPOINTS ─────────
static const char BACKEND_HOST[] = "cloud.hydroleaf.in";
static const char AUTH_PATH[] = "/api/v1/cloud/authenticate";
static const char UPLOAD_PRFX[] = "/upload/";

// ───────── TIMINGS ─────────
#define WIFI_RETRY_MS 30000UL

// ───────── HTML TEMPLATES ─────────
#include <pgmspace.h>
static const char PAGE_KEY_TPL[] PROGMEM = R"rawliteral(
<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content=width=device-width,initial-scale=1>
<title>Hydroleaf Smart‑Cam · Cloud Key</title>
<style>
 body{margin:0;background:#f7f9fc;font:16px system-ui,sans-serif}
 .card{max-width:420px;margin:48px auto;background:#fff;border-radius:12px;
       box-shadow:0 4px 18px rgba(0,0,0,.08);padding:24px}
 h1{margin:0 0 24px;font-size:20px}
 input,button{width:100%;font-size:15px;padding:10px;border-radius:6px;
       border:1px solid #ccc;box-sizing:border-box}
 button{margin-top:24px;background:#007bff;border:none;color:#fff;font-weight:600}
 button:hover{opacity:.9;cursor:pointer}
 small{display:block;margin-top:18px;text-align:center}
</style>
<div class=card>
<h1>Cloud Key</h1>
<form action=/save_key method=POST>
<input id=cloudkey name=cloudkey value="%s" placeholder="key from HydroLeaf app">
<button type=submit>Save Key</button>
</form>
<small><a href="/">← back</a></small>
</div>
)rawliteral";

static const char PAGE_MENU_TPL[] PROGMEM = R"rawliteral(
<!doctype html><html lang=en><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HydroLeaf Smart‑Cam · Dashboard</title>
<style>
 :root{--accent:#007bff;--bg:#fafafa;--r:12px}
 body{margin:0;font:16px/1.4 system-ui,sans-serif;background:var(--bg);padding:24px;color:#222}
 .card{max-width:480px;margin:auto;background:#fff;border-radius:var(--r);
       box-shadow:0 4px 16px rgba(0,0,0,.07);padding:24px}
 h1{margin:0 0 20px;font-size:22px;font-weight:600}
 p{margin:0 0 20px}
 a{display:block;width:100%;padding:12px;margin:8px 0;border-radius:var(--r);
   background:var(--accent);color:#fff;text-align:center;font-weight:600;
   text-decoration:none}
 a:hover{filter:brightness(1.05)}
 small{display:block;margin-top:16px;text-align:center;font-size:13px;color:#666}
</style>
<main class=card>
 <h1>HydroLeaf Smart‑Cam</h1>
 <p><b>ID:</b> %s<br>
    <b>STA IP:</b> %s<br>
    <b>AP IP:</b> %s<br>
    <b>Cloud Key:</b> %s</p>
 <a href="/wifi">Configure Wi‑Fi</a>
 <a href="/cloudkey">Set Cloud Key</a>
 <a href="/status">Device Status</a>
 <a href="/logs">Logs</a>
 <small>Firmware v4.3</small>
</main>
</html>
)rawliteral";


static const char PAGE_WIFI_TPL[] PROGMEM = R"rawliteral(
<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content=width=device-width,initial-scale=1>
<title>Hydroleaf Smart‑Cam · Wi‑Fi setup</title>
<style>
 body{margin:0;background:#f7f9fc;font:16px system-ui,sans-serif;color:#333}
 .card{max-width:420px;margin:48px auto;background:#fff;border-radius:12px;
       box-shadow:0 4px 18px rgba(0,0,0,.08);padding:24px}
 h1{margin:0 0 24px;font-size:20px}
 label{display:block;margin:12px 0 6px;font-weight:600}
 input,select,button{width:100%;font-size:15px;padding:10px;border-radius:6px;
       border:1px solid #ccc;box-sizing:border-box}
 button{margin-top:24px;background:#28a745;border:none;color:#fff;font-weight:600}
 button:hover{opacity:.9;cursor:pointer}
 small{display:block;margin-top:18px;text-align:center}
</style>
<div class=card>
<h1>Connect to Wi‑Fi</h1>
<form action=/save_wifi method=POST>
<label for=ssid>Networks</label>
<select id=ssid name=ssid>%s</select>
<label for=ssid_manual>…or type SSID manually</label>
<input id=ssid_manual oninput="ssid.value=this.value">
<label for=pass>Password</label>
<input id=pass name=pass type=password value="%s" placeholder="network password">
<label for=cloudkey>Cloud Key</label>
<input id=cloudkey name=cloudkey value="%s" placeholder="key from HydroLeaf app">
<button type=submit>Save & Connect</button>
</form>
<small><a href="/">← back</a></small>
</div>
<script>
// auto‑select manual SSID field if dropdown stays on custom value
const s=document.getElementById('ssid'),m=document.getElementById('ssid_manual');
s.addEventListener('change',()=>{if(s.selectedIndex===0)m.focus();});
</script>
)rawliteral";


static const char PAGE_STATUS_TPL[] PROGMEM = R"rawliteral(
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Device Status</title>
<style>body{font-family:system-ui;background:#fafafa;margin:0;padding:18px}</style>
</head><body>
<h1>Status</h1>
<p><b>Camera ID:</b> %s<br>
   <b>AP IP:</b> %s<br>
   <b>STA IP:</b> %s<br>
   <b>Cloud Auth:</b> %s</p>
<a href="/">Back</a>
</body></html>
)rawliteral";

static const char PAGE_LOGS_TPL[] PROGMEM = R"rawliteral(
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Hydroleaf Logs</title>
<style>body{font-family:system-ui;background:#fafafa;margin:0;padding:18px}
pre{background:#222;color:#0f0;padding:12px;overflow:auto;height:80vh}
a{display:block;width:100%;padding:10px;margin:8px 0;border:0;
background:#007bff;color:#fff;font-size:16px;text-align:center;text-decoration:none;border-radius:4px}
</style></head><body>
<h1>Logs</h1><pre>%s</pre>
<a href="/">Back to Menu</a>
</body></html>
)rawliteral";

// ───────── GLOBALS ─────────
Preferences prefs;
WebServer server(80);
DNSServer dns;
WiFiClient client;
HTTPClient http;
sensor_t* cam = nullptr;
bool camReady = false;
bool nightMode = false;

String ssid, pass, apPass, camId, jwt, cloudKey;
bool activated = false;
unsigned long lastWifiTry = 0;
void handleCloudKey() {
  prefs.begin("cam_cfg", true);
  String curK = prefs.getString("cloudkey", DEFAULT_CLOUD_KEY);
  prefs.end();

  char page[600];
  snprintf_P(page, sizeof(page), PAGE_KEY_TPL, curK.c_str());
  server.send(200, "text/html", page);
}

void handleSaveKey() {
  if (server.hasArg("cloudkey")) {
    cloudKey = server.arg("cloudkey");
    prefs.begin("cam_cfg", false);
    prefs.putString("cloudkey", cloudKey);
    prefs.end();

    if (WiFi.isConnected()) cloudAuthenticate();  // re‑auth immediately
  }
  server.sendHeader("Location", "/status", true);
  server.send(302, "text/plain", "");
}

// ───────── LOG BUFFER ─────────
#define LOG_BUF_SZ 2048
static char logBuf[LOG_BUF_SZ];
static size_t logHead = 0;
void logLine(const char* msg) {
  size_t n = strlen(msg);
  if (n + 2 > LOG_BUF_SZ) return;
  if (logHead + n + 2 >= LOG_BUF_SZ) logHead = 0;
  memcpy(logBuf + logHead, msg, n);
  logBuf[logHead + n] = '\n';
  logBuf[logHead + n + 1] = 0;
  logHead += n + 1;
  Serial.println(msg);
}

// ───────── NVS HELPERS ─────────
inline void storeString(const char* key, const String& val) {
  prefs.begin("cam_cfg", false);
  prefs.putString(key, val);
  prefs.end();
}
inline void storeBool(const char* key, bool v) {
  prefs.begin("cam_cfg", false);
  prefs.putBool(key, v);
  prefs.end();
}

// ───────── PROTOTYPES ─────────
void generateCameraId();
bool wifiConnect(uint8_t retries = 5);
bool cloudAuthenticate();
void initCamera();
void applyDayParams();
bool sendFrame();
void updateLDR();
void handleButton();
void portalStart();
void setupRoutes();
void handleMenu();
void handleWiFi();
void handleSaveWiFi();
void handleStatus();
void handleLogs();
void handleOtaPush();

// ───────── LED HEARTBEAT ─────────
inline void beatLED() {
  static uint32_t t = 0;
  if (millis() - t < 3000) return;
  t = millis();
  digitalWrite(LED_STATUS, HIGH);
  digitalWrite(LED_STATUS, LOW);
}

// ───────── PAGE HANDLERS ─────────
void handleMenu() {
  char buf[512];
  String staStr = WiFi.isConnected() ? WiFi.localIP().toString() : String("Not connected");
  IPAddress apIP(192, 168, 0, 1);
  snprintf_P(buf, sizeof(buf), PAGE_MENU_TPL,
             camId.c_str(),
             staStr.c_str(),
             apIP.toString().c_str(),
             cloudKey.c_str());
  server.send(200, "text/html", buf);
}

void handleWiFi() {
  prefs.begin("cam_cfg", true);
  String curS = prefs.getString("ssid", ""),
         curP = prefs.getString("pass", ""),
         curK = prefs.getString("cloudkey", DEFAULT_CLOUD_KEY);
  prefs.end();

  /* Scan once (blocking, but we’re inside a request handler). */
  int n = WiFi.scanNetworks(false, true);
  String opts = "<option value='' selected hidden>– select –</option>";
  for (int i = 0; i < n && opts.length() < 850; ++i) {  // protect 1 kB buffer
    String ss = WiFi.SSID(i);
    opts += "<option value='" + ss + "'";
    if (ss == curS) opts += " selected";
    opts += ">" + ss + " (" + String(WiFi.RSSI(i)) + " dBm)</option>";
  }
  WiFi.scanDelete();

  char page[1800];
  snprintf_P(page, sizeof(page), PAGE_WIFI_TPL,
             opts.c_str(), /* NEW */
             curP.c_str(),
             curK.c_str());
  server.send(200, "text/html", page);
}


void handleSaveWiFi() {
  if (server.hasArg("ssid") && server.hasArg("pass") && server.hasArg("cloudkey")) {
    ssid = server.arg("ssid");
    pass = server.arg("pass");
    cloudKey = server.arg("cloudkey");

    prefs.begin("cam_cfg", false);
    prefs.putString("ssid", ssid);
    prefs.putString("pass", pass);
    prefs.putString("cloudkey", cloudKey);
    prefs.end();
    if (WiFi.isConnected()) cloudAuthenticate();

    /* Don’t block here – let the main loop do the reconnect.   */
    lastWifiTry = 0;  // forces an immediate attempt in loop()
  }
  server.sendHeader("Location", "/status", true);
  server.send(302, "text/plain", "");
}


void handleStatus() {
  IPAddress apIP(192, 168, 0, 1);
  IPAddress staIP = WiFi.isConnected() ? WiFi.localIP() : IPAddress(0, 0, 0, 0);
  const char* cloudSt = activated ? "OK" : "Not Auth";
  char buf[512];
  snprintf_P(buf, sizeof(buf), PAGE_STATUS_TPL,
             camId.c_str(),
             apIP.toString().c_str(),
             staIP.toString().c_str(),
             cloudSt);
  server.send(200, "text/html", buf);
}

void handleLogs() {
  static char tmp[LOG_BUF_SZ * 2];
  size_t fp = strnlen(logBuf + logHead, LOG_BUF_SZ - logHead);
  memcpy(tmp, logBuf + logHead, fp);
  memcpy(tmp + fp, logBuf, logHead);
  tmp[fp + logHead] = 0;
  char page[LOG_BUF_SZ * 2 + 64];
  snprintf_P(page, sizeof(page), PAGE_LOGS_TPL, tmp);
  server.send(200, "text/html", page);
}

void handleOtaPush() {
  HTTPUpload& up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    if (!server.hasHeader("X-OTA-KEY") || server.header("X-OTA-KEY") != cloudKey) {
      server.send(403, "text/plain", "Forbidden");
      return;
    }
    logLine("[OTA] push-start");
    Update.begin(UPDATE_SIZE_UNKNOWN);
  } else if (up.status == UPLOAD_FILE_WRITE) {
    Update.write(up.buf, up.currentSize);
  } else if (up.status == UPLOAD_FILE_END) {
    bool ok = Update.end(true);
    logLine(ok ? "[OTA] push-done" : "[OTA] push-fail");
    server.send(ok ? 200 : 500, "text/plain", ok ? "OK" : "FAIL");
    delay(400);
    ESP.restart();
  }
}

void setupRoutes() {
  server.on("/", HTTP_GET, handleMenu);
  server.on("/wifi", HTTP_GET, handleWiFi);
  server.on("/save_wifi", HTTP_POST, handleSaveWiFi);
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/logs", HTTP_GET, handleLogs);
  server.on("/cloudkey", HTTP_GET, handleCloudKey);
  server.on("/save_key", HTTP_POST, handleSaveKey);

  server.on(
    "/manual_update", HTTP_POST, []() {}, handleOtaPush);
  server.begin();
}


// ───────── CAMERA INIT ─────────
void applyDayParams() {
  prefs.begin("cam_cfg", true);
  int b = prefs.getInt("bright", 1),
      c = prefs.getInt("contr", 1),
      s = prefs.getInt("sat", 1),
      d = prefs.getInt("dn", 5);
  prefs.end();
  cam->set_brightness(cam, b);
  cam->set_contrast(cam, c);
  cam->set_saturation(cam, s);
  cam->set_denoise(cam, d);
}

void initCamera() {
  camera_config_t cfg{};
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
    cfg.fb_count = 3;
    cfg.jpeg_quality = 6;
  } else {
    cfg.frame_size = FRAMESIZE_QVGA;
    cfg.fb_count = 1;
    cfg.jpeg_quality = 12;
  }

  if (esp_camera_init(&cfg) != ESP_OK) {
    logLine("[CAM] init fail");
    delay(500);
    ESP.restart();
  }
  cam = esp_camera_sensor_get();
  cam->set_hmirror(cam, 1);
  cam->set_vflip(cam, 0);
  applyDayParams();
  camReady = true;
  logLine("[CAM] ready");
}
void portalStart() {
  /* Use a fixed channel inside 1‑11 to avoid country‑code resets.   */
  wifi_country_t in = { "IN", 1, 11, 0, WIFI_COUNTRY_POLICY_MANUAL };
  esp_wifi_set_country(&in);

  WiFi.mode(WIFI_AP_STA);  // dual‑mode from the start
  IPAddress apIP(192, 168, 0, 1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  WiFi.softAP(camId.c_str(), apPass.c_str(), 6, false);

  dns.start(53, "*", apIP);
  setupRoutes();
  logLine("[AP] portal ready (192.168.0.1)");
}

// ───────── WIFI & CLOUD AUTH ─────────
// ───── wifiConnect() – replace the whole function ─────
bool wifiConnect(uint8_t retries) {
  /* 1.  Never drop the AP: use dual mode.
     * 2.  Give the AP a predictable hostname (good for mDNS later).    */
  WiFi.mode(WIFI_AP_STA);
  WiFi.setHostname(camId.c_str());

  for (uint8_t i = 0; i < retries; ++i) {
    logLine("[NET] connecting…");
    WiFi.begin(ssid.c_str(), pass.c_str());

    for (uint8_t t = 0; t < 50; ++t) {
      if (WiFi.status() == WL_CONNECTED) {
        logLine(("[NET] IP " + WiFi.localIP().toString()).c_str());
        return true;
      }
      delay(100);
      yield();
    }

    /* 3.  ONLY drop the *station* connection; leave the AP intact.   */
    WiFi.disconnect(false /* keep STA object */, true /* erase cred */);
  }
  return false;
}


void generateCameraId() {
  prefs.begin("cam_cfg", false);
  camId = prefs.getString("camId", "");
  apPass = prefs.getString("apPass", "configme");
  prefs.end();

  if (camId.length()) return;
  // time needed for reproducible ID
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  struct tm tm;
  char buf[32] = "CAM";
  if (getLocalTime(&tm, 4000))
    strftime(buf, sizeof(buf), "CAM_%Y%m%d_%H%M%S", &tm);

  camId = String(buf) + "_" + String((uint32_t)esp_random(), HEX);
  storeString("camId", camId);
}

bool cloudAuthenticate() {
  String url = String("http://") + BACKEND_HOST + AUTH_PATH;
  http.begin(client, url);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<128> d;
  d["device_id"] = camId;
  d["cloud_key"] = cloudKey;
  String body;
  serializeJson(d, body);

  int code = http.POST(body);
  String resp = http.getString();
  http.end();

  if (code != 200) {
    logLine("[AUTH] fail");
    return false;
  }
  StaticJsonDocument<256> rd;
  if (deserializeJson(rd, resp) || !rd["token"].is<const char*>()) {
    logLine("[AUTH] bad JSON");
    return false;
  }

  jwt = rd["token"].as<const char*>();
  storeString("token", jwt);
  activated = true;
  storeBool("activated", true);
  logLine("[AUTH] success");
  return true;
}

// ───────── FRAME STREAMING ─────────
bool sendFrame() {
  if (!camReady) return false;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return false;

  String url = String("http://") + BACKEND_HOST + UPLOAD_PRFX + camId + (nightMode ? "/night" : "/day");

  http.begin(client, url);
  http.setTimeout(3000);  // 3 s hard wall
  http.addHeader("Content-Type", "image/jpeg");
  if (jwt.length()) http.addHeader("Authorization", "Bearer " + jwt);

  int code = http.sendRequest("POST", fb->buf, fb->len);
  esp_camera_fb_return(fb);
  http.end();
  yield();  // keep the WDT happy

  if (code == 401) { cloudAuthenticate(); }
  return code >= 200 && code < 300;
}


// ───────── LDR DAY/NIGHT SWITCH ─────────
uint8_t ldrRing[5] = { 1, 1, 1, 1, 1 }, ldrIdx = 0;
void updateLDR() {
  ldrRing[ldrIdx++] = digitalRead(PIN_LDR);
  if (ldrIdx >= 5) ldrIdx = 0;
  uint8_t dark = 0;
  for (auto v : ldrRing)
    if (v == LOW) ++dark;

  if (dark >= 4 && !nightMode) {
    nightMode = true;
    digitalWrite(PIN_IRLED, HIGH);
    cam->set_pixformat(cam, PIXFORMAT_GRAYSCALE);
    cam->set_brightness(cam, 3);
    cam->set_contrast(cam, 3);
    cam->set_saturation(cam, 0);
    cam->set_gain_ctrl(cam, 1);      // enable AGC
    cam->set_exposure_ctrl(cam, 1);  // enable AEC
    cam->set_denoise(cam, 7);
  } else if (dark <= 1 && nightMode) {
    nightMode = false;
    digitalWrite(PIN_IRLED, LOW);
    applyDayParams();
    cam->set_whitebal(cam, 1);
    cam->set_awb_gain(cam, 1);
  }
}

// ───────── BUTTON HANDLER ─────────
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
      logLine("[BTN] factory reset");
      delay(500);
      ESP.restart();
    } else if (held >= 100) {
      portalStart();
    }
  }
}

// ───────── SETUP & LOOP ─────────
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
  apPass = prefs.getString("apPass", "configme");
  jwt = prefs.getString("token", "");
  activated = prefs.getBool("activated", false);
  cloudKey = prefs.getString("cloudkey", DEFAULT_CLOUD_KEY);
  prefs.end();

  generateCameraId();

  if (ssid.length() && wifiConnect()) {
    // sync time for initial ID + future logging
    configTime(0, 0, "pool.ntp.org", "time.google.com");
    if (!activated) activated = cloudAuthenticate();
    if (activated && !camReady) initCamera();
  }

  portalStart();
}

void loop() {
  handleButton();
  updateLDR();
  beatLED();

  // retry Wi-Fi
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiTry > WIFI_RETRY_MS) {
    lastWifiTry = millis();
    logLine("[NET] retry");
    if (wifiConnect() && activated && !camReady) {
      initCamera();
    }
  }

  // continuous streaming
  if (activated && WiFi.status() == WL_CONNECTED && camReady) {
    bool ok = sendFrame();
    logLine(ok ? "[TX] ok" : "[TX] fail");
  }

  dns.processNextRequest();
  server.handleClient();
}
