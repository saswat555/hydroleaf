/**
 * Production‐Grade ESP32 Firmware
 * • Always‐On Hotspot + STA
 * • LVGL UI on TFT_eSPI: AP Info, Status & Log
 * • In-Memory Circular Log Buffer
 * • AsyncWebServer: three pages (Status, Live pH/TDS, Configure Wi-Fi + Cloud Key)
 * • Serial Monitor logging at each step
 */

#include <lvgl.h>
#include <TFT_eSPI.h>
#include <WiFi.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <LittleFS.h>
#include <ArduinoJson.h>

// ---- Display & LVGL ----
TFT_eSPI tft = TFT_eSPI();
static lv_disp_draw_buf_t draw_buf;
static lv_color_t buf1[LV_HOR_RES_MAX * 40];

// ---- Webserver ----
AsyncWebServer server(80);

// ---- UI Objects ----
lv_obj_t* apLabel;
lv_obj_t* apInfoLabel;
lv_obj_t* statusLabel;
lv_obj_t* ipLabel;
lv_obj_t* logArea;

// ---- In-Memory Log Buffer ----
#define LOG_LINES 10
String logBuffer[LOG_LINES];
int    logIndex = 0;

// ---- Forward Declarations ----
void initLVGL();
void setupWebPortal();
void showAPScreen(const String& apSSID);
void showStatusScreen();
void addLog(const String& msg);
float readPH();
float readTDS();
String readCloudKey();

// ---- SETUP ----
void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\n\n=== Booting ESP32 Production Firmware ===");

  // 1) Mount LittleFS
  if (!LittleFS.begin()) {
    Serial.println("⚠️ LittleFS mount failed!");
    addLog("⚠ FS Mount Failed");
  } else {
    Serial.println("✔️ LittleFS mounted");
    addLog("FS mounted");
  }

  // 2) Init LVGL + TFT
  initLVGL();
  Serial.println("✔️ LVGL + TFT initialized");
  addLog("UI initialized");

  // 3) Always-On Hotspot
  String apSSID = "Device-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(apSSID);
  Serial.printf("✔️ Hotspot ON: %s\n", apSSID.c_str());
  addLog("AP ON: " + apSSID);

  // 4) Try STA connect w/ stored creds
  Serial.println("ℹ️ Attempting STA connect...");
  addLog("STA connect...");
  WiFi.begin();
  uint32_t start = millis();
  while (millis() - start < 10000) {
    lv_timer_handler(); delay(5);
    if (WiFi.status() == WL_CONNECTED) break;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("✔️ STA Connected to %s, IP: %s\n",
                  WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());
    addLog("STA OK: " + WiFi.SSID());
    showStatusScreen();
  } else {
    Serial.println("⚠️ STA connect failed → Provisioning mode");
    addLog("STA FAILED");
    setupWebPortal();
    showAPScreen(apSSID);
    // block here until /save triggers reboot
    while (true) { lv_timer_handler(); delay(5); }
  }
}

// ---- MAIN LOOP ----
void loop() {
  lv_timer_handler();
  delay(5);

  static uint32_t last = 0;
  if (millis() - last > 1000) {
    last = millis();
    // update IP display
    String ip = WiFi.localIP().toString();
    lv_label_set_text(ipLabel, ip.c_str());
    addLog("IP: " + ip);
  }
}

// ---- LVGL INIT ----
void initLVGL() {
  lv_init();
  tft.begin(); tft.setRotation(1);
  lv_disp_draw_buf_init(&draw_buf, buf1, NULL, LV_HOR_RES_MAX * 40);

  static lv_disp_drv_t disp_drv;
  lv_disp_drv_init(&disp_drv);
  disp_drv.flush_cb = [](lv_disp_drv_t* disp, const lv_area_t* area, lv_color_t* color_p){
    tft.startWrite();
    tft.setAddrWindow(area->x1, area->y1,
                      area->x2-area->x1+1,
                      area->y2-area->y1+1);
    uint32_t size = (area->x2-area->x1+1)*(area->y2-area->y1+1);
    tft.pushColors((uint16_t*)&color_p->full, size, true);
    tft.endWrite();
    lv_disp_flush_ready(disp);
  };
  disp_drv.draw_buf = &draw_buf;
  disp_drv.hor_res  = tft.width();
  disp_drv.ver_res  = tft.height();
  lv_disp_drv_register(&disp_drv);
}

// ---- WEB PORTAL ----
void setupWebPortal() {
  Serial.println("ℹ️ Starting Web Portal...");
  addLog("WebPortal start");

  // serve index.html
  server.on("/", HTTP_GET, [](AsyncWebServerRequest* req){
    req->send(LittleFS, "/index.html", "text/html");
  });

  // STATUS page
  server.on("/status", HTTP_GET, [](AsyncWebServerRequest* req){
    req->send(LittleFS, "/status.html", "text/html");
  });
  server.on("/status_api", HTTP_GET, [](AsyncWebServerRequest* req){
    DynamicJsonDocument j(256);
    j["ssid"]      = WiFi.SSID();
    j["ip"]        = WiFi.localIP().toString();
    j["cloud_key"] = readCloudKey();
    String s; serializeJson(j, s);
    req->send(200, "application/json", s);
  });

  // pH/TDS page
  server.on("/ph_tds", HTTP_GET, [](AsyncWebServerRequest* req){
    req->send(LittleFS, "/ph_tds.html", "text/html");
  });
  server.on("/ph_tds_api", HTTP_GET, [](AsyncWebServerRequest* req){
    DynamicJsonDocument j(128);
    j["ph"]  = readPH();
    j["tds"] = readTDS();
    String s; serializeJson(j, s);
    req->send(200, "application/json", s);
  });

  // Save Wi-Fi + Cloud Key
  server.on("/save", HTTP_POST, [](AsyncWebServerRequest* req){
    if (req->hasParam("ssid", true) && req->hasParam("pass", true) && req->hasParam("cloud", true)) {
      String ss = req->getParam("ssid", true)->value();
      String pw = req->getParam("pass", true)->value();
      String ck = req->getParam("cloud", true)->value();
      File f = LittleFS.open("/wifi.txt","w");
      if (f) {
        f.println(ss); f.println(pw); f.println(ck);
        f.close();
        req->send(200, "text/plain", "OK - Saved");
        Serial.println("✔️ Credentials + Cloud Key saved");
        addLog("Saved WiFi+Cloud");
        delay(1000); ESP.restart();
        return;
      }
    }
    req->send(400,"text/plain","Bad Request");
  });

  server.begin();
  Serial.println("✔️ Web Portal running on 80");
}

// ---- AP INFO SCREEN ----
void showAPScreen(const String& apSSID) {
  lv_scr_load(nullptr);
  lv_obj_t* scr = lv_obj_create(NULL);

  apLabel = lv_label_create(scr);
  lv_label_set_text(apLabel, "🔧 Provisioning Mode");
  lv_obj_align(apLabel, LV_ALIGN_TOP_MID, 0, 10);

  apInfoLabel = lv_label_create(scr);
  String info = "Connect to:\n" + apSSID + "\nOpen http://192.168.4.1";
  lv_label_set_text(apInfoLabel, info.c_str());
  lv_label_set_long_mode(apInfoLabel, LV_LABEL_LONG_WRAP);
  lv_obj_set_width(apInfoLabel, lv_pct(80));
  lv_obj_align(apInfoLabel, LV_ALIGN_CENTER, 0, 0);

  lv_scr_load(scr);
}

// ---- STATUS & LOG SCREEN ----
void showStatusScreen() {
  lv_scr_load(nullptr);
  lv_obj_t* scr = lv_obj_create(NULL);

  statusLabel = lv_label_create(scr);
  lv_label_set_text(statusLabel, "📶 Device Status");
  lv_obj_align(statusLabel, LV_ALIGN_TOP_MID, 0, 5);

  lv_obj_t* lbl = lv_label_create(scr);
  lv_label_set_text(lbl, "IP:");
  lv_obj_align(lbl, LV_ALIGN_TOP_LEFT, 5, 30);

  ipLabel = lv_label_create(scr);
  lv_label_set_text(ipLabel, "0.0.0.0");
  lv_obj_align(ipLabel, LV_ALIGN_TOP_LEFT, 30, 30);

  logArea = lv_textarea_create(scr);
  lv_obj_set_size(logArea, lv_pct(90), lv_pct(40));
  lv_obj_align(logArea, LV_ALIGN_BOTTOM_MID, 0, -10);
  lv_textarea_set_text(logArea, "");
  lv_textarea_set_cursor_hidden(logArea, true);

  lv_scr_load(scr);
}

// ---- LOGGING ----
void addLog(const String& msg) {
  Serial.println("LOG: " + msg);
  logBuffer[logIndex] = msg;
  logIndex = (logIndex + 1) % LOG_LINES;
  String out;
  for (int i = 0; i < LOG_LINES; i++) {
    int idx = (logIndex + i) % LOG_LINES;
    if (logBuffer[idx].length()) {
      out += logBuffer[idx] + "\n";
    }
  }
  if (logArea) lv_textarea_set_text(logArea, out.c_str());
}

// ---- SENSOR & CLOUD KEY STUBS ----
// Replace with your actual sensor code and secure key storage
float readPH() {
  // TODO: implement actual pH sensor read
  return 7.0 + (sin(millis()/60000.0) * 0.1);
}
float readTDS() {
  // TODO: implement actual TDS sensor read
  return 500 + (cos(millis()/60000.0) * 20);
}
String readCloudKey() {
  File f = LittleFS.open("/wifi.txt","r");
  if (!f) return "";
  f.readStringUntil('\n'); // ssid
  f.readStringUntil('\n'); // pass
  String ck = f.readStringUntil('\n');
  f.close();
  return ck;
}
