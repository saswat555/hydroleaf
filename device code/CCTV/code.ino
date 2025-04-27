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

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <esp_camera.h>
#include <time.h>
#include <Update.h>
#include <HTTPClient.h>
#include <ESPHTTPUpdate.h>

// ───────── GPIO Map (AI-Thinker) ───────────────────────────────────────────
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
#define LED_STATUS        33  // heartbeat
#define BTN_CONFIG         4  // LOW = pressed
#define PIN_LDR           14  // LDR input
#define PIN_IRLED         12  // IR LED output

// ───────── Backend & Portal Defaults ───────────────────────────────────────
static const char*   BACKEND_HOST      = "cloud.hydroleaf.in";
static const uint16_t BACKEND_PORT     = 80;
static const char*   BACKEND_UPLOAD    = "/api/v1/cameras/upload/" + g_camId + "/day";
static const char*   AUTH_PATH         = "/api/v1/cloud/authenticate";
static const char*   UPDATE_PULL_PATH  = "/api/v1/device_comm/update/pull?device_id=";

static const char*   DEFAULT_AP_PWD    = "configme";
static const byte    DNS_PORT          = 53;
static bool          otaSuccess        = false;

Preferences prefs;
WebServer   http(80);
DNSServer   dns;
sensor_t*   cam = nullptr;

String  g_ssid, g_pass, g_camId, g_apPass, g_subKey;
bool    g_night    = false;
bool    g_activated = false;

// ───────── Forward Declarations ────────────────────────────────────────────
void initCamera();
bool wifiConnect(uint8_t tries=5);
void generateCameraId();
bool sendFrame();
void updateLDR();
void handleButton();
String htmlHeader(const char* title);
void setupRoutes();
void portalStart();
bool validateServerKey();
void checkCloudUpdate();

// ───────── Helper: commit NVS String ───────────────────────────────────────
inline void commitPref(const char* key, const String& val) {
  prefs.begin("cam_cfg", false);
  prefs.putString(key, val);
  prefs.end();
}

// ───────── Heartbeat LED ───────────────────────────────────────────────────
const uint32_t BLINK_MS=3000;
uint32_t lastBlink=0;
inline void beatLED(){
  uint32_t now=millis();
  if(now-lastBlink<BLINK_MS) return;
  lastBlink=now;
  digitalWrite(LED_STATUS,HIGH);
  delay(20);
  digitalWrite(LED_STATUS,LOW);
}

// ───────── HTML + CSS header ───────────────────────────────────────────────
String htmlHeader(const char* title){
  String h="<!doctype html><html><head><meta charset='utf-8'>";
  h+="<meta name='viewport' content='width=device-width,initial-scale=1'>";
  h+="<title>";h+=title;h+="</title><style>"
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

// Main menu
void handleMenu(){
  String p=htmlHeader("Config Menu");
  p+="<h2>Settings</h2><ul>"
     "<li><a href='/subscribe'>Activate Camera</a></li>"
     "<li><a href='/wifi'>Change Wi-Fi</a></li>"
     "<li><a href='/ap_password'>Reset AP Password</a></li>"
     "</ul></body></html>";
  http.send(200,"text/html",p);
}

// Subscription page
void handleSubscribePage(){
  String p=htmlHeader("Activate Camera");
  p+="<h2>Enter Subscription Key</h2><form action='/save_subscribe'>"
     "Key:<input name='key' value='"+g_subKey+"'>"
     "<button type='submit'>Activate</button></form>"
     "<a href='/'>← Back</a></body></html>";
  http.send(200,"text/html",p);
}

// Save & validate subscription key
void handleSubscribeSave(){
  if(!http.hasArg("key")){
    http.send(400,"text/plain","Missing subscription key"); return;
  }
  g_subKey = http.arg("key");
  commitPref("subKey", g_subKey);
  if(validateServerKey()){
    g_activated = true;
    http.send(200,"text/html","<h1>Activated! Restarting…</h1>");
    delay(1000); ESP.restart();
  } else {
    http.send(200,"text/html","<h1>Invalid key!</h1><a href='/subscribe'>Try again</a>");
  }
}

// Wi-Fi form
void handleWiFiPage(){
  String p=htmlHeader("Wi-Fi Setup");
  p+="<h2>Select Wi-Fi Network</h2><form action='/save_wifi'>"
     "SSID:<input name='ssid' value='"+g_ssid+"'>"
     "Password:<input type='password' name='pass' value='"+g_pass+"'>"
     "<button type='submit'>Save &amp; Restart</button></form>"
     "<a href='/'>← Back</a></body></html>";
  http.send(200,"text/html",p);
}

// AP-Password form
void handleAPPage(){
  String p=htmlHeader("Hotspot Password");
  p+="<h2>Reset AP Password</h2><form action='/reset_ap'>"
     "New Password:<input type='password' name='apass' value='"+g_apPass+"'>"
     "<button type='submit'>Update</button></form>"
     "<a href='/'>← Back</a></body></html>";
  http.send(200,"text/html",p);
}

// Save Wi-Fi & restart
void handleSaveWiFi(){
  if(!http.hasArg("ssid")||!http.hasArg("pass")){
    http.send(400,"text/plain","Missing fields");return;
  }
  g_ssid=http.arg("ssid"); commitPref("ssid",g_ssid);
  g_pass=http.arg("pass"); commitPref("pass",g_pass);
  http.send(200,"text/html","<h1>Saved! Restarting…</h1>");
  delay(1000); ESP.restart();
}

// Save AP-Password (no restart)
void handleResetAP(){
  if(!http.hasArg("apass")){
    http.send(400,"text/plain","Missing password");return;
  }
  g_apPass=http.arg("apass"); commitPref("apPass",g_apPass);
  dns.stop(); http.stop();
  portalStart();
  http.send(200,"text/html","<h1>AP Password Updated!</h1><a href='/'>Back</a>");
}

// Redirect all other paths to portal
void handleNotFound(){
  http.sendHeader("Location","http://192.168.0.1",true);
  http.send(302);
}

// Build routes
void setupRoutes(){
  http.on("/",               HTTP_GET,  handleMenu);
  http.on("/subscribe",      HTTP_GET,  handleSubscribePage);
  http.on("/save_subscribe", HTTP_GET,  handleSubscribeSave);
  http.on("/wifi",           HTTP_GET,  handleWiFiPage);
  http.on("/ap_password",    HTTP_GET,  handleAPPage);
  http.on("/save_wifi",      HTTP_GET,  handleSaveWiFi);
  http.on("/reset_ap",       HTTP_GET,  handleResetAP);

  // OTA upload endpoint (fixed IP)
  http.on("/update_firmware", HTTP_POST,
    [](){
      if(otaSuccess)      http.send(200,"text/plain","DONE");
      else                http.send(500,"text/plain","FAIL");
      otaSuccess=false;
      delay(100);
      ESP.restart();
    },
    [](){
      HTTPUpload& up = http.upload();
      switch(up.status){
        case UPLOAD_FILE_START:
          if(!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
          break;
        case UPLOAD_FILE_WRITE:
          if(Update.write(up.buf, up.currentSize)!=up.currentSize) Update.printError(Serial);
          break;
        case UPLOAD_FILE_END:
          otaSuccess = Update.end(true);
          break;
        case UPLOAD_FILE_ABORTED:
          otaSuccess = false;
          break;
      }
    }
  );

  http.onNotFound(handleNotFound);
  http.begin();
}

// Start captive portal (AP+STA) at fixed IP 192.168.0.1
void portalStart(){
  Serial.println("[CFG] starting portal");
  WiFi.mode(WIFI_AP_STA);

  // **FIXED AP IP** 
  IPAddress local_IP(192,168,0,1);
  IPAddress gateway(192,168,0,1);
  IPAddress subnet(255,255,255,0);
  WiFi.softAPConfig(local_IP, gateway, subnet);
  WiFi.softAP(g_camId.length()?g_camId.c_str():DEFAULT_AP_PWD, g_apPass.c_str());

  dns.start(DNS_PORT,"*",local_IP);
  setupRoutes();
}

// ───────── Camera & Streaming ──────────────────────────────────────────────
void initCamera(){
  camera_config_t cfg = {};
  cfg.ledc_channel = LEDC_CHANNEL_0;
  cfg.ledc_timer   = LEDC_TIMER_0;
  cfg.pin_d0=Y2_GPIO_NUM; cfg.pin_d1=Y3_GPIO_NUM;
  cfg.pin_d2=Y4_GPIO_NUM; cfg.pin_d3=Y5_GPIO_NUM;
  cfg.pin_d4=Y6_GPIO_NUM; cfg.pin_d5=Y7_GPIO_NUM;
  cfg.pin_d6=Y8_GPIO_NUM; cfg.pin_d7=Y9_GPIO_NUM;
  cfg.pin_xclk=XCLK_GPIO_NUM; cfg.pin_pclk=PCLK_GPIO_NUM;
  cfg.pin_vsync=VSYNC_GPIO_NUM; cfg.pin_href=HREF_GPIO_NUM;
  cfg.pin_sscb_sda=SIOD_GPIO_NUM; cfg.pin_sscb_scl=SIOC_GPIO_NUM;
  cfg.pin_pwdn=PWDN_GPIO_NUM; cfg.pin_reset=RESET_GPIO_NUM;
  cfg.xclk_freq_hz=20000000; cfg.pixel_format=PIXFORMAT_JPEG;
  if(psramFound()){
    cfg.frame_size=FRAMESIZE_HD; cfg.fb_count=2; cfg.jpeg_quality=12;
  } else {
    cfg.frame_size=FRAMESIZE_SVGA; cfg.fb_count=1; cfg.jpeg_quality=15;
  }
  if(esp_camera_init(&cfg)!=ESP_OK){
    Serial.println("[ERR] camera init failed"); delay(500); ESP.restart();
  }
  cam=esp_camera_sensor_get();
  cam->set_hmirror(cam,1);
  cam->set_vflip(cam,0);
}

// Wi-Fi with backoff
bool wifiConnect(uint8_t tries){
  WiFi.mode(WIFI_STA);
  while(tries--){
    Serial.printf("[NET] connect '%s'\n", g_ssid.c_str());
    WiFi.begin(g_ssid.c_str(), g_pass.c_str());
    for(int i=0;i<50;i++){
      if(WiFi.status()==WL_CONNECTED){
        Serial.printf("[NET] IP %s RSSI %d\n",
          WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
      }
      delay(200);
    }
    WiFi.disconnect(true); delay(200);
  }
  return false;
}

// Generate & persist Camera-ID once
void generateCameraId(){
  prefs.begin("cam_cfg",false);
  String stored = prefs.getString("camId","");
  if(stored.length()){ g_camId=stored; prefs.end(); return; }
  configTime(0,0,"pool.ntp.org","time.google.com");
  struct tm tm;
  if(!getLocalTime(&tm,5000)){
    g_camId="CAM_"+String((uint32_t)esp_random(),HEX);
  } else {
    char buf[32];
    strftime(buf,sizeof(buf),"CAM_%Y%m%d_%H%M%S",&tm);
    g_camId=String(buf)+"_"+String((uint32_t)esp_random(),HEX);
  }
  prefs.putString("camId",g_camId);
  prefs.end();
}

// Upload frame in chunks
bool sendFrame(){
  camera_fb_t* fb = esp_camera_fb_get();
  if(!fb){ Serial.println("[ERR] capture"); return false; }
  WiFiClient cli;
  if(!cli.connect(BACKEND_HOST,BACKEND_PORT)){
    Serial.println("[ERR] backend"); esp_camera_fb_return(fb); return false;
  }
  String hdr = String("POST ") + BACKEND_UPLOAD + "/" + g_camId +
               (g_night?"/night":"/day") + " HTTP/1.1\r\n" +
               "Host: " + BACKEND_HOST + "\r\n" +
               "Content-Type: image/jpeg\r\n" +
               "Content-Length: " + fb->len + "\r\n" +
               "Connection: close\r\n\r\n";
  cli.print(hdr);
  size_t off=0, chunk=psramFound()?4096:1024;
  while(off<fb->len){
    size_t w=min(chunk, fb->len-off);
    cli.write(fb->buf+off, w);
    off+=w;
  }
  unsigned long t0=millis();
  while(cli.connected() && millis()-t0<500) cli.read();
  cli.stop(); esp_camera_fb_return(fb);
  return true;
}

// Day/night via LDR
uint8_t bufLdr[5]={1,1,1,1,1}, idxL=0;
void updateLDR(){
  bufLdr[idxL++] = digitalRead(PIN_LDR); idxL%=5;
  int dark=0; for(auto v:bufLdr) if(v==LOW) dark++;
  if(dark>=4 && !g_night){
    g_night=true; digitalWrite(PIN_IRLED,HIGH);
    cam->set_whitebal(cam,0); cam->set_awb_gain(cam,0);
    cam->set_brightness(cam,2); cam->set_contrast(cam,2);
    cam->set_saturation(cam,-1);cam->set_denoise(cam,7);
  } else if(dark<=1 && g_night){
    g_night=false; digitalWrite(PIN_IRLED,LOW);
    prefs.begin("cam_cfg",false);
    cam->set_brightness(cam,prefs.getInt("bright",1));
    cam->set_contrast(cam,  prefs.getInt("contr",1));
    cam->set_saturation(cam,prefs.getInt("sat",1));
    cam->set_denoise(cam,   prefs.getInt("dn",5));
    prefs.end();
    cam->set_whitebal(cam,1); cam->set_awb_gain(cam,1);
  }
}

// Config button: short=portal toggle, long=factory reset
void handleButton(){
  static unsigned long down=0;
  bool pressed = digitalRead(BTN_CONFIG)==LOW;
  if(pressed && !down) down=millis();
  if(!pressed && down){
    unsigned long held=millis()-down; down=0;
    if(held>=3000){
      prefs.begin("cam_cfg",false); prefs.clear(); prefs.end();
      delay(500); ESP.restart();
    } else if(held>=100){
      portalStart();
    }
  }
}

// Validate subscription key with cloud
bool validateServerKey(){
  if(g_camId.isEmpty()||g_subKey.isEmpty()) return false;
  HTTPClient httpc;
  String url = String("http://") + BACKEND_HOST + AUTH_PATH;
  httpc.begin(url);
  httpc.addHeader("Content-Type","application/json");
  String body = String("{\"device_id\":\"") + g_camId +
                String("\",\"cloud_key\":\"") + g_subKey + "\"}";
  int code = httpc.POST(body);
  httpc.end();
  return (code==200);
}

// Check & perform OTA from cloud
void checkCloudUpdate(){
  if(!WiFi.isConnected() || !g_activated) return;
  t_httpUpdate_return ret = ESPhttpUpdate.update(
    BACKEND_HOST, BACKEND_PORT,
    String(UPDATE_PULL_PATH) + g_camId
  );
  if(ret==HTTP_UPDATE_NO_UPDATES){
    Serial.println("[OTA] no update");
  } else if(ret==HTTP_UPDATE_OK){
    // will reboot automatically
  } else {
    Serial.printf("[OTA] failed %d\n", ret);
  }
}

// ───────── Setup & Loop ─────────────────────────────────────────────────────
void setup(){
  Serial.begin(115200);
  pinMode(LED_STATUS,OUTPUT); digitalWrite(LED_STATUS,LOW);
  pinMode(BTN_CONFIG,INPUT_PULLUP);
  pinMode(PIN_LDR,INPUT_PULLUP);
  pinMode(PIN_IRLED,OUTPUT); digitalWrite(PIN_IRLED,LOW);

  // Load Wi-Fi, AP-pwd, Subscription Key
  prefs.begin("cam_cfg",false);
  g_ssid   = prefs.getString("ssid","");
  g_pass   = prefs.getString("pass","");
  g_apPass = prefs.getString("apPass",DEFAULT_AP_PWD);
  g_subKey = prefs.getString("subKey","");
  prefs.end();

  // Attempt Wi-Fi & subscription
  if(g_ssid.length() && wifiConnect()){
    generateCameraId();
    if(g_subKey.length() && validateServerKey()){
      g_activated = true;
      Serial.println("[ACT] subscription valid");
      initCamera();
      Serial.println("[SETUP] streaming...");
      checkCloudUpdate();
    } else {
      Serial.println("[ACT] subscription missing/invalid");
    }
  }

  // Always bring up AP+portal
  if(!g_ssid.length()||WiFi.status()!=WL_CONNECTED){
    Serial.println("[SETUP] AP-only mode");
  }
  portalStart();
}

void loop(){
  dns.processNextRequest();
  http.handleClient();
  handleButton();
  updateLDR();
  if(WiFi.status()==WL_CONNECTED && g_activated){
    sendFrame();
    beatLED();
  }
  yield();
}
