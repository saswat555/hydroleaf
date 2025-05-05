/*****************************************************************************************
 *  HYDROLEAF SMART-CAM  –  Production Firmware  v4.1
 *  Target  : ESP32-CAM (AI-Thinker) – 4 MB flash, PSRAM enabled
 *  Author  : ChatGPT (o4-mini)
 *
 *  COMPLETE PRODUCTION-GRADE
 *  • Always-on AP + captive portal on 192.168.0.1  
 *  • /wifi & /save_wifi for credentials  
 *  • /status for IPs & cloud auth state  
 *  • /logs, /, OTA push/pull, day/night LDR, cloud upload preserved
 *****************************************************************************************/

#include <ArduinoJson.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <esp_camera.h>
#include <Update.h>
#include <HTTPClient.h>
#include <time.h>

// ───────── GPIO MAP (AI-Thinker) ─────────
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM   -1
#define XCLK_GPIO_NUM     0
#define SIOD_GPIO_NUM    26
#define SIOC_GPIO_NUM    27
#define Y9_GPIO_NUM      35
#define Y8_GPIO_NUM      34
#define Y7_GPIO_NUM      39
#define Y6_GPIO_NUM      36
#define Y5_GPIO_NUM      21
#define Y4_GPIO_NUM      19
#define Y3_GPIO_NUM      18
#define Y2_GPIO_NUM       5
#define VSYNC_GPIO_NUM   25
#define HREF_GPIO_NUM    23
#define PCLK_GPIO_NUM    22

#define LED_STATUS       33
#define BTN_CONFIG        4
#define PIN_LDR          14
#define PIN_IRLED        12

// ───────── CLOUD ENDPOINTS ─────────
static const char BACKEND_HOST[]       = "cloud.hydroleaf.in";
static const char AUTH_PATH[]          = "/api/v1/cloud/authenticate";
static const char UPLOAD_PRFX[]        = "/upload/";            
static const char UPDATE_CHECK_PRFX[]  = "/api/v1/device_comm/update?device_id=";
static const char CLOUD_KEY[]          = "5e882fe3a75c3dfce2fd90459eaa4997";

// ───────── TIMINGS ─────────
#define WIFI_RETRY_MS    (30UL * 1000UL)
#define FRAME_IVL_MS     50UL     // ~20 FPS
#define UPDATE_IVL_MS    (6UL * 60UL * 60UL * 1000UL)

// ───────── HTML TEMPLATES IN PROGMEM ─────────
static const char PAGE_MENU_TPL[]     PROGMEM = R"rawliteral(
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Hydroleaf Smart-Cam</title>
<style>body{font-family:system-ui;background:#fafafa;margin:0;padding:18px}
h1{font-size:20px}a,button{display:block;width:100%;padding:10px;margin:8px 0;border:0;
background:#007bff;color:#fff;font-size:16px;text-align:center;text-decoration:none;border-radius:4px}
</style></head><body>
<h1>Hydroleaf Smart-Cam</h1>
<p><b>ID:</b> %s<br>
   <b>STA IP:</b> %s<br>
   <b>AP IP:</b> %s<br>
   <b>Cloud Key:</b> %s</p>
<a href="/wifi">Configure Wi-Fi</a>
<a href="/status">Device Status</a>
<a href="/logs">View Logs</a>
</body></html>
)rawliteral";

static const char PAGE_WIFI_TPL[]     PROGMEM = R"rawliteral(
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Configure Wi-Fi</title>
<style>body{font-family:system-ui;background:#fafafa;margin:0;padding:18px}
label, input{display:block;width:100%;padding:6px;margin:6px 0;font-size:16px}
button{padding:10px;width:100%;border:none;background:#007bff;color:#fff;border-radius:4px;font-size:16px}
</style></head><body>
<h1>Wi-Fi Settings</h1>
<form action="/save_wifi" method="POST">
  <label>SSID:<input name="ssid" value="%s"></label>
  <label>Password:<input name="pass" type="password" value="%s"></label>
  <button type="submit">Save &amp; Connect</button>
</form>
<a href="/">Back to Menu</a>
</body></html>
)rawliteral";

static const char PAGE_STATUS_TPL[]   PROGMEM = R"rawliteral(
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

static const char PAGE_LOGS_TPL[]     PROGMEM = R"rawliteral(
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
WebServer  server(80);
DNSServer  dns;
sensor_t*  cam         = nullptr;
bool       camReady    = false;
bool       nightMode   = false;

String     ssid, pass, apPass, camId, jwt;
bool       activated   = false;

static unsigned long lastWifiTry   = 0;
static unsigned long lastFrameTx   = 0;
static unsigned long lastUpdateChk = 0;

// ───────── Tiny in-RAM circular log buffer ─────────
#define LOG_BUF_SZ 2048
static char logBuf[LOG_BUF_SZ];
static size_t logHead = 0;
void logLine(const char* msg) {
  size_t n = strlen(msg);
  if (n+2 > LOG_BUF_SZ) return;
  if (logHead + n+2 >= LOG_BUF_SZ) logHead = 0;
  memcpy(logBuf + logHead, msg, n);
  logBuf[logHead + n]   = '\n';
  logBuf[logHead + n+1] = 0;
  logHead += n+1;
  Serial.println(msg);
}

// ───────── PROTOTYPES ─────────
void generateCameraId();
bool wifiConnect(uint8_t retries=5);
bool cloudAuthenticate();
void initCamera();
void applyDayParams();
bool sendFrame();
void updateLDR();
void handleButton();
void portalStart();
void setupRoutes();
void checkCloudOta();
void handleMenu();
void handleWiFi();
void handleSaveWiFi();
void handleStatus();
void handleLogs();
void handleOtaPush();

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

// ───────── LED HEARTBEAT ─────────
inline void beatLED() {
  static uint32_t t=0;
  if (millis()-t < 3000) return;
  t = millis();
  digitalWrite(LED_STATUS, HIGH);
  delay(20);
  digitalWrite(LED_STATUS, LOW);
}

// ───────── PAGE HANDLERS ─────────
void handleMenu() {
  char buf[512];
  IPAddress staIP = WiFi.isConnected() ? WiFi.localIP() : IPAddress(0,0,0,0);
  IPAddress apIP(192,168,0,1);
  snprintf_P(buf,sizeof(buf),PAGE_MENU_TPL,
             camId.c_str(),
             staIP.toString().c_str(),
             apIP.toString().c_str(),
             CLOUD_KEY);
  server.send(200, "text/html", buf);
}

void handleWiFi() {
  prefs.begin("cam_cfg", true);
  String curS = prefs.getString("ssid",""), curP = prefs.getString("pass","");
  prefs.end();

  char buf[512];
  snprintf_P(buf,sizeof(buf),PAGE_WIFI_TPL,
             curS.c_str(), curP.c_str());
  server.send(200, "text/html", buf);
}

void handleSaveWiFi() {
  if (server.hasArg("ssid") && server.hasArg("pass")) {
    ssid = server.arg("ssid");
    pass = server.arg("pass");
    prefs.begin("cam_cfg", false);
    prefs.putString("ssid", ssid);
    prefs.putString("pass", pass);
    prefs.end();
    WiFi.begin(ssid.c_str(), pass.c_str());
    delay(2500);
  }
  server.sendHeader("Location","/status",true);
  server.send(302, "text/plain", "");
}

void handleStatus() {
  IPAddress apIP(192,168,0,1);
  IPAddress staIP = WiFi.isConnected() ? WiFi.localIP() : IPAddress(0,0,0,0);
  const char* cloudSt = activated ? "OK" : "Not Auth";
  char buf[512];
  snprintf_P(buf,sizeof(buf),PAGE_STATUS_TPL,
             camId.c_str(),
             apIP.toString().c_str(),
             staIP.toString().c_str(),
             cloudSt);
  server.send(200,"text/html",buf);
}

void handleLogs() {
  static char tmp[LOG_BUF_SZ*2];
  size_t fp = strnlen(logBuf+logHead, LOG_BUF_SZ-logHead);
  memcpy(tmp, logBuf+logHead, fp);
  memcpy(tmp+fp, logBuf, logHead);
  tmp[fp+logHead] = 0;

  char page[LOG_BUF_SZ*2+64];
  snprintf_P(page,sizeof(page),PAGE_LOGS_TPL,tmp);
  server.send(200,"text/html",page);
}

void handleOtaPush() {
  HTTPUpload& up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    if (!server.hasHeader("X-OTA-KEY") || server.header("X-OTA-KEY")!=CLOUD_KEY) {
      server.send(403,"text/plain","Forbidden");
      return;
    }
    logLine("[OTA] push-start");
    Update.begin(UPDATE_SIZE_UNKNOWN);
  }
  else if (up.status == UPLOAD_FILE_WRITE) {
    Update.write(up.buf, up.currentSize);
  }
  else if (up.status == UPLOAD_FILE_END) {
    bool ok = Update.end(true);
    logLine(ok?"[OTA] push-done":"[OTA] push-fail");
    server.send(ok?200:500,"text/plain", ok?"OK":"FAIL");
    delay(400);
    ESP.restart();
  }
}

// ───────── SETUP ROUTES & PORTAL ─────────
void setupRoutes() {
  server.on("/",            HTTP_GET,  handleMenu);
  server.on("/wifi",        HTTP_GET,  handleWiFi);
  server.on("/save_wifi",   HTTP_POST, handleSaveWiFi);
  server.on("/status",      HTTP_GET,  handleStatus);
  server.on("/logs",        HTTP_GET,  handleLogs);
  server.on("/manual_update",HTTP_POST, [](){}, handleOtaPush);
  server.begin();
}

void portalStart() {
  WiFi.mode(WIFI_AP_STA);
  IPAddress apIP(192,168,0,1);
  WiFi.softAPConfig(apIP,apIP,IPAddress(255,255,255,0));
  WiFi.softAP(camId.c_str(), apPass.c_str());
  dns.start(53,"*",apIP);
  setupRoutes();
  logLine("[AP] portal ready");
}

// ───────── CAMERA INIT & PARAMETERS ─────────
void applyDayParams() {
  prefs.begin("cam_cfg", true);
  int b = prefs.getInt("bright",1),
      c = prefs.getInt("contr",1),
      s = prefs.getInt("sat",1),
      d = prefs.getInt("dn",5);
  prefs.end();
  cam->set_brightness(cam,b);
  cam->set_contrast(  cam,c);
  cam->set_saturation(cam,s);
  cam->set_denoise(   cam,d);
}

void initCamera() {
  camera_config_t cfg{};
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.ledc_timer   = LEDC_TIMER_0;
  cfg.pin_d0       = Y2_GPIO_NUM;
  cfg.pin_d1       = Y3_GPIO_NUM;
  cfg.pin_d2       = Y4_GPIO_NUM;
  cfg.pin_d3       = Y5_GPIO_NUM;
  cfg.pin_d4       = Y6_GPIO_NUM;
  cfg.pin_d5       = Y7_GPIO_NUM;
  cfg.pin_d6       = Y8_GPIO_NUM;
  cfg.pin_d7       = Y9_GPIO_NUM;
  cfg.pin_xclk     = XCLK_GPIO_NUM;
  cfg.pin_pclk     = PCLK_GPIO_NUM;
  cfg.pin_vsync    = VSYNC_GPIO_NUM;
  cfg.pin_href     = HREF_GPIO_NUM;
  cfg.pin_sscb_sda = SIOD_GPIO_NUM;
  cfg.pin_sscb_scl = SIOC_GPIO_NUM;
  cfg.pin_pwdn     = PWDN_GPIO_NUM;
  cfg.pin_reset    = RESET_GPIO_NUM;
  cfg.xclk_freq_hz = 20000000;
  cfg.pixel_format = PIXFORMAT_JPEG;
  if (psramFound()) {
    cfg.frame_size   = FRAMESIZE_HD;
    cfg.fb_count     = 2;
    cfg.jpeg_quality = 12;
  } else {
    cfg.frame_size   = FRAMESIZE_SVGA;
    cfg.fb_count     = 1;
    cfg.jpeg_quality = 15;
  }
  if (esp_camera_init(&cfg)!=ESP_OK) {
    logLine("[CAM] init fail");
    delay(500);
    ESP.restart();
  }
  cam = esp_camera_sensor_get();
  cam->set_hmirror(cam,1);
  cam->set_vflip( cam,0);
  applyDayParams();
  camReady = true;
  logLine("[CAM] ready");
}

// ───────── WIFI & CLOUD AUTH ─────────
bool wifiConnect(uint8_t retries) {
  WiFi.mode(WIFI_STA);
  for (uint8_t i=0;i<retries;i++) {
    logLine("[NET] connecting...");
    WiFi.begin(ssid.c_str(), pass.c_str());
    for (uint8_t t=0;t<50;t++) {
      if (WiFi.status()==WL_CONNECTED) {
        logLine(("[NET] IP "+WiFi.localIP().toString()).c_str());
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
  prefs.begin("cam_cfg",false);
  camId = prefs.getString("camId","");
  apPass = prefs.getString("apPass","configme");
  prefs.end();
  if (camId.length()) return;
  configTime(0,0,"pool.ntp.org","time.google.com");
  struct tm tm; char buf[32]="CAM";
  if (getLocalTime(&tm,4000))
    strftime(buf,sizeof(buf),"CAM_%Y%m%d_%H%M%S",&tm);
  camId = String(buf) + "_" + String((uint32_t)esp_random(),HEX);
  storeString("camId", camId);
}

bool cloudAuthenticate() {
  HTTPClient cli;
  String url = String("http://")+BACKEND_HOST+AUTH_PATH;
  cli.begin(url.c_str());
  cli.addHeader("Content-Type","application/json");
  StaticJsonDocument<128> d; d["device_id"]=camId; d["cloud_key"]=CLOUD_KEY;
  String body; serializeJson(d,body);
  int code = cli.POST(body);
  String resp = cli.getString(); cli.end();
  if (code!=200) { logLine("[AUTH] fail"); return false; }
  StaticJsonDocument<256> rd; if (deserializeJson(rd,resp)||!rd["token"].is<const char*>()) return false;
  jwt = rd["token"].as<const char*>(); storeString("token",jwt);
  activated=true; storeBool("activated",true);
  logLine("[AUTH] success");
  return true;
}

// ───────── FRAME UPLOAD ─────────
bool sendFrame() {
  if (!camReady) return false;
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return false;
  String url = String("http://")+BACKEND_HOST+UPLOAD_PRFX+camId+(nightMode?"/night":"/day");
  HTTPClient cli; cli.begin(url.c_str());
  cli.addHeader("Content-Type","image/jpeg");
  if (jwt.length()) cli.addHeader("Authorization","Bearer "+jwt);
  int code = cli.POST(fb->buf, fb->len);
  cli.end(); esp_camera_fb_return(fb);
  if (code==401) {
    jwt=""; storeString("token",""); activated=false; storeBool("activated",false);
  }
  return (code>=200 && code<300);
}

// ───────── LDR DAY/NIGHT SWITCH ─────────
uint8_t ldrRing[5]={1,1,1,1,1}, ldrIdx=0;
void updateLDR() {
  ldrRing[ldrIdx++] = digitalRead(PIN_LDR);
  if (ldrIdx>=5) ldrIdx=0;
  uint8_t dark=0; for (auto v:ldrRing) if (v==LOW) ++dark;
  if (dark>=4 && !nightMode) {
    nightMode=true; digitalWrite(PIN_IRLED,HIGH);
    cam->set_whitebal(cam,0); cam->set_awb_gain(cam,0);
    cam->set_brightness(cam,2); cam->set_contrast(cam,2);
    cam->set_saturation(cam,-1);cam->set_denoise(cam,7);
  }
  else if (dark<=1 && nightMode) {
    nightMode=false; digitalWrite(PIN_IRLED,LOW);
    applyDayParams(); cam->set_whitebal(cam,1); cam->set_awb_gain(cam,1);
  }
}

// ───────── BUTTON HANDLER ─────────
void handleButton() {
  static unsigned long down=0;
  bool pressed = (digitalRead(BTN_CONFIG)==LOW);
  if (pressed && !down) down=millis();
  if (!pressed && down) {
    unsigned long held=millis()-down; down=0;
    if (held>=3000) {
      prefs.begin("cam_cfg",false); prefs.clear(); prefs.end();
      logLine("[BTN] factory reset"); delay(500); ESP.restart();
    }
    else if (held>=100) portalStart();
  }
}

// ───────── OTA PULL ─────────
void checkCloudOta() {
  if (!WiFi.isConnected()||!activated) return;
  HTTPClient cli;
  String chk = String("http://")+BACKEND_HOST+UPDATE_CHECK_PRFX+camId;
  cli.begin(chk.c_str()); int code=cli.GET();
  if (code!=200){ logLine("[OTA] check fail"); cli.end(); return; }
  String r=cli.getString(); cli.end();
  StaticJsonDocument<256> d; if (deserializeJson(d,r)){ logLine("[OTA] bad JSON"); return; }
  if (!d["update_available"]){ logLine("[OTA] up-to-date"); return; }
  String bin = d["download_url"].as<const char*>(); logLine(("[OTA] pull "+bin).c_str());
  cli.begin(bin.c_str()); code=cli.GET(); if (code!=200){ logLine("[OTA] no bin"); cli.end(); return; }
  int len = cli.getSize(); WiFiClient* stream = cli.getStreamPtr();
  if (!Update.begin(len?len:UPDATE_SIZE_UNKNOWN)){ Update.printError(Serial); cli.end(); return; }
  uint8_t buf[256]; size_t written=0;
  while(cli.connected()&&(written<len||len==0)){
    size_t avail=stream->available();
    if(avail){
      size_t rd=stream->readBytes(buf,min(avail,sizeof(buf)));
      Update.write(buf,rd); written+=rd;
    }
    delay(1);
  }
  bool ok=Update.end()&&Update.isFinished();
  logLine(ok?"[OTA] SUCCESS":"[OTA] FAIL");
  cli.end(); if(ok){ delay(400); ESP.restart(); }
}

// ───────── SETUP ─────────
void setup(){
  Serial.begin(115200);
  pinMode(LED_STATUS, OUTPUT);
  digitalWrite(LED_STATUS, LOW);
  pinMode(BTN_CONFIG, INPUT_PULLUP);
  pinMode(PIN_LDR, INPUT_PULLUP);
  pinMode(PIN_IRLED, OUTPUT);
  digitalWrite(PIN_IRLED, LOW);

  prefs.begin("cam_cfg", false);
  ssid      = prefs.getString("ssid",""); 
  pass      = prefs.getString("pass",""); 
  apPass    = prefs.getString("apPass","configme");
  jwt       = prefs.getString("token",""); 
  activated = prefs.getBool("activated",false);
  prefs.end();

  generateCameraId();

  if (ssid.length() && wifiConnect()){
    if (!activated)              activated = cloudAuthenticate();
    if (activated && !camReady)  initCamera();
    if (activated) {
      checkCloudOta();
      lastUpdateChk = millis();
    }
  }
  portalStart();
}

// ───────── LOOP ─────────
void loop(){
  handleButton();
  updateLDR();
  beatLED();

  if (WiFi.status()!=WL_CONNECTED &&
      millis()-lastWifiTry>WIFI_RETRY_MS) {
    lastWifiTry = millis();
    logLine("[NET] retry");
    if (wifiConnect() && activated && !camReady)
      initCamera();
  }

  if (activated && WiFi.status()==WL_CONNECTED &&
      camReady && millis()-lastFrameTx>FRAME_IVL_MS) {
    bool ok = sendFrame();
    logLine(ok?"[TX] ok":"[TX] fail");
    lastFrameTx = millis();
  }

  if (activated && WiFi.status()==WL_CONNECTED &&
      millis()-lastUpdateChk>UPDATE_IVL_MS) {
    lastUpdateChk = millis();
    checkCloudOta();
  }

  dns.processNextRequest();
  server.handleClient();
}
