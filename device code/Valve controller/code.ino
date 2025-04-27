/*****************************************************************************************
 *  HYDROLEAF VALVE-CTL  –  Production Firmware  v1.0  (28-Apr-2025)
 *  Target  : ESP8266 (4 MB flash)
 *  Author  : ChatGPT (OpenAI o4-mini)
 *
 *  • Device-ID auto-generated once (chip ID).
 *  • SSID/Pass/Host/Port persisted in EEPROM.
 *  • AP SSID = Device-ID, never user-configurable.
 *  • AP password persisted and resettable via captive portal.
 *  • Always-on captive portal (AP+STA) at 192.168.4.1 for Wi-Fi/Host/Port & AP-password reset.
 *  • Full production-grade: 4 valves, cloud heartbeat, chunked OTA (local & cloud).
 *****************************************************************************************/

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <DNSServer.h>
#include <EEPROM.h>
#include <ArduinoJson.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266httpUpdate.h>
#include <Ticker.h>

#pragma GCC optimize("Os")

// ───────── Configuration ───────────────────────────────────────────────────────
static const uint8_t  VALVE_COUNT     = 4;
static const uint8_t  valvePins[VALVE_COUNT] = { D1, D2, D5, D6 };
static const char*    DEFAULT_AP_PWD  = "configme";
static const byte     DNS_PORT        = 53;
static const unsigned long UPDATE_INTERVAL = 60*60UL;  // hourly cloud-check

#define EEPROM_SIZE       512
#define ADDR_SSID         0
#define ADDR_PASS         64
#define ADDR_HOST         128
#define ADDR_PORT         192
#define ADDR_AP_PWD       194
#define ADDR_DEVICE_ID    256
#define ADDR_TOKEN        288
#define ADDR_VALVE_STATE  336

// ───────── Libraries & Globals ────────────────────────────────────────────────
ESP8266WebServer http(80);
DNSServer           dns;
Ticker              heartbeatTicker;

String  g_ssid, g_pass;
String  g_host; 
uint16_t g_port;
String  g_apPass;
String  g_deviceId, g_token;
bool    g_valveState[VALVE_COUNT];

WiFiClient wifiClient;
bool        otaSuccess = false;
unsigned long lastCloudCheck = 0;

// ───────── Helpers: EEPROM ↔ String ────────────────────────────────────────────
void saveString(int addr, const String &s, int maxLen){
  for(int i=0;i<maxLen;i++){
    char c = i < s.length() ? s[i] : '\0';
    EEPROM.write(addr + i, c);
  }
}

String loadString(int addr, int maxLen){
  String s;
  for(int i=0;i<maxLen;i++){
    char c = EEPROM.read(addr + i);
    if(!c) break;
    s += c;
  }
  return s;
}

void commitConfig(){
  EEPROM.commit();
}

// ───────── Heartbeat LED (reuse D0) ───────────────────────────────────────────
const uint32_t BLINK_MS=3000;
uint32_t lastBlink=0;
void beatLED(){
  uint32_t now=millis();
  if(now-lastBlink<BLINK_MS) return;
  lastBlink=now;
  digitalWrite(LED_BUILTIN,LOW);
  delay(20);
  digitalWrite(LED_BUILTIN,HIGH);
}

// ───────── HTML + CSS header ────────────────────────────────────────────────
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

// ───────── Captive-Portal Handlers ────────────────────────────────────────────

// Main menu
void handleMenu(){
  String p=htmlHeader("Config Menu");
  p+="<h2>Settings</h2><ul>"
     "<li><a href='/network'>Wi-Fi / Host & Port</a></li>"
     "<li><a href='/ap_password'>Reset AP Password</a></li>"
     "</ul></body></html>";
  http.send(200,"text/html",p);
}

// Wi-Fi + Backend form
void handleNetworkPage(){
  String p=htmlHeader("Network Setup");
  p+="<h2>Wi-Fi & Backend</h2><form action='/save_network'>"
     "Wi-Fi SSID:<input name='ssid' value='"+g_ssid+"'>"
     "Wi-Fi Pass:<input type='password' name='pass' value='"+g_pass+"'>"
     "Host:<input name='host' value='"+g_host+"'>"
     "Port:<input name='port' value='"+String(g_port)+"'>"
     "<button type='submit'>Save &amp; Restart</button></form>"
     "<a href='/'>← Back</a></body></html>";
  http.send(200,"text/html",p);
}

void handleSaveNetwork(){
  if(!http.hasArg("ssid")||!http.hasArg("pass")
  || !http.hasArg("host")||!http.hasArg("port")){
    http.send(400,"text/plain","Missing fields");return;
  }
  g_ssid = http.arg("ssid");
  g_pass = http.arg("pass");
  g_host = http.arg("host");
  g_port = http.arg("port").toInt();
  saveString(ADDR_SSID, g_ssid,   64);
  saveString(ADDR_PASS, g_pass,   64);
  saveString(ADDR_HOST, g_host,   64);
  EEPROM.write(ADDR_PORT, (g_port>>8)&0xFF);
  EEPROM.write(ADDR_PORT+1, g_port&0xFF);
  commitConfig();
  http.send(200,"text/html","<h1>Saved! Restarting…</h1>");
  delay(1000); ESP.restart();
}

// AP-Password form
void handleAPPage(){
  String p=htmlHeader("AP Password");
  p+="<h2>Reset AP Password</h2><form action='/reset_ap'>"
     "Password:<input type='password' name='apwd' value='"+g_apPass+"'>"
     "<button type='submit'>Update</button></form>"
     "<a href='/'>← Back</a></body></html>";
  http.send(200,"text/html",p);
}

void handleResetAP(){
  if(!http.hasArg("apwd")){
    http.send(400,"text/plain","Missing");return;
  }
  g_apPass = http.arg("apwd");
  saveString(ADDR_AP_PWD, g_apPass, 32);
  commitConfig();
  dns.stop(); http.stop();
  // restart captive-portal
  WiFi.softAP(g_deviceId.c_str(),g_apPass.c_str());
  dns.start(DNS_PORT,"*",WiFi.softAPIP());
  setupRoutes();
  http.send(200,"text/html","<h1>AP Password Updated!</h1><a href='/'>Back</a>");
}

// Trap everything else
void handleNotFound(){
  http.sendHeader("Location","http://192.168.4.1",true);
  http.send(302);
}

// Build all routes
void setupRoutes(){
  http.on("/",            HTTP_GET,  handleMenu);
  http.on("/network",     HTTP_GET,  handleNetworkPage);
  http.on("/save_network",HTTP_GET,  handleSaveNetwork);
  http.on("/ap_password", HTTP_GET,  handleAPPage);
  http.on("/reset_ap",    HTTP_GET,  handleResetAP);

  // Get current valve states
  http.on("/state", HTTP_GET, [](){
    DynamicJsonDocument doc(256);
    doc["device_id"]=g_deviceId;
    auto arr=doc.createNestedArray("valves");
    for(uint8_t i=0;i<VALVE_COUNT;i++){
      JsonObject v=arr.createNestedObject();
      v["id"]=i+1; v["state"]=g_valveState[i];
    }
    String s; serializeJson(doc,s);
    http.send(200,"application/json",s);
  });

  // Toggle a valve
  http.on("/toggle", HTTP_POST, [](){
    DynamicJsonDocument in(128), out(128);
    if(deserializeJson(in,http.arg("plain"))||!in.containsKey("valve_id")){
      http.send(400,"application/json","{\"error\":\"invalid payload\"}"); return;
    }
    int vid=in["valve_id"];
    if(vid<1||vid>VALVE_COUNT){
      http.send(400,"application/json","{\"error\":\"valve_id out of range\"}"); return;
    }
    // flip
    uint8_t idx=vid-1;
    g_valveState[idx]=!g_valveState[idx];
    digitalWrite(valvePins[idx], g_valveState[idx]?HIGH:LOW);
    EEPROM.write(ADDR_VALVE_STATE+idx, g_valveState[idx]?1:0);
    commitConfig();
    // report upstream
    if(WiFi.status()==WL_CONNECTED && g_token.length()){
      HTTPClient  hc;
      String url=String("http://")+g_host+":"+g_port+"/valve_event";
      if(hc.begin(wifiClient,url)){
        hc.addHeader("Content-Type","application/json");
        hc.addHeader("Authorization","Bearer "+g_token);
        StaticJsonDocument<128> ev;
        ev["device_id"]=g_deviceId;
        ev["valve_id"]=vid;
        ev["state"]=g_valveState[idx];
        String body; serializeJson(ev,body);
        hc.POST(body);
        hc.end();
      }
    }
    out["device_id"]=g_deviceId;
    out["valve_id"]=vid;
    out["new_state"]=g_valveState[idx];
    String s; serializeJson(out,s);
    http.send(200,"application/json",s);
  });

  // Local OTA via multipart POST
  http.on("/update_firmware", HTTP_POST,
    [](){ // response after upload
      if(otaSuccess)   http.send(200,"text/plain","DONE");
      else             http.send(500,"text/plain","FAIL");
      otaSuccess=false;
      delay(100);
      ESP.restart();
    },
    [](){ // upload handler
      HTTPUpload &up = http.upload();
      if(up.status==UPLOAD_FILE_START){
        Serial.println("[OTA] Begin");
        WiFiClient::stopAll();
        uint32_t maxSz = (ESP.getFreeSketchSpace() - 0x1000) & 0xFFFFF000;
        if(!Update.begin(maxSz)) Update.printError(Serial);
      } else if(up.status==UPLOAD_FILE_WRITE){
        if(Update.write(up.buf, up.currentSize)!=up.currentSize)
          Update.printError(Serial);
      } else if(up.status==UPLOAD_FILE_END){
        otaSuccess = Update.end(true);
        Serial.println(otaSuccess? "[OTA] Success":"[OTA] Fail");
      } else if(up.status==UPLOAD_FILE_ABORTED){
        otaSuccess = false;
      }
    }
  );

  http.onNotFound(handleNotFound);
  http.begin();
}

// ───────── Networking & Capsule ───────────────────────────────────────────────
bool wifiConnect(uint8_t tries=5){
  WiFi.mode(WIFI_STA);
  while(tries--){
    WiFi.begin(g_ssid.c_str(),g_pass.c_str());
    for(int i=0;i<50;i++){
      if(WiFi.status()==WL_CONNECTED) return true;
      delay(200);
    }
    WiFi.disconnect(true);
    delay(200);
  }
  return false;
}

void portalStart(){
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(g_deviceId.c_str(), g_apPass.c_str());
  dns.start(DNS_PORT,"*",WiFi.softAPIP());
  setupRoutes();
}

// ───────── Device-ID & Config init ──────────────────────────────────────────
void generateDeviceId(){
  g_deviceId = loadString(ADDR_DEVICE_ID,32);
  if(!g_deviceId.length()){
    uint32_t chip=ESP.getChipId();
    g_deviceId = "valve-"+String(chip,HEX);
    saveString(ADDR_DEVICE_ID,g_deviceId,32);
    commitConfig();
  }
}

// ───────── Cloud Registration & OTA ─────────────────────────────────────────
void cloudRegister(){
  if(g_token.length()) return;
  HTTPClient hc;
  String url = String("http://")+g_host+":"+g_port+"/register";
  if(hc.begin(wifiClient,url)){
    hc.addHeader("Content-Type","application/json");
    StaticJsonDocument<128> j; j["device_id"]=g_deviceId;
    String b; serializeJson(j,b);
    if(hc.POST(b)==200){
      StaticJsonDocument<128> resp;
      deserializeJson(resp,hc.getString());
      if(resp.containsKey("token")){
        g_token = resp["token"].as<String>();
        saveString(ADDR_TOKEN,g_token,48);
        commitConfig();
      }
    }
    hc.end();
  }
}

void checkCloudUpdate(){
  if(WiFi.status()!=WL_CONNECTED||!g_token.length()) return;
  if(millis()<lastCloudCheck+UPDATE_INTERVAL) return;
  lastCloudCheck=millis();
  HTTPClient hc;
  String url=String("http://")+g_host+":"+g_port+"/update?device_id="+g_deviceId;
  if(hc.begin(wifiClient,url)){
    hc.addHeader("Authorization","Bearer "+g_token);
    if(hc.GET()==200){
      StaticJsonDocument<256> doc;
      deserializeJson(doc,hc.getString());
      if(doc["update_available"]){
        String uri = "/update/pull?device_id="+g_deviceId;
        ESPhttpUpdate.setAuthorization("Bearer "+g_token);
        ESPhttpUpdate.rebootOnUpdate(true);
        t_httpUpdate_return r = ESPhttpUpdate.update(wifiClient,g_host,cfg_port,uri);
        Serial.printf("[OTA] cloud pull: %d\n",r);
      }
    }
    hc.end();
  }
}

// Heartbeat
void sendHeartbeat(){
  if(WiFi.status()!=WL_CONNECTED||!g_token.length())return;
  HTTPClient hc;
  String url=String("http://")+g_host+":"+g_port+"/heartbeat";
  if(hc.begin(wifiClient,url)){
    hc.addHeader("Content-Type","application/json");
    hc.addHeader("Authorization","Bearer "+g_token);
    StaticJsonDocument<128> h;
    h["device_id"]=g_deviceId;
    h["status"]="ok";
    h["ts"]=millis();
    String b; serializeJson(h,b);
    hc.POST(b);
    hc.end();
  }
}

// ───────── Setup & Loop ─────────────────────────────────────────────────────
void setup(){
  Serial.begin(115200);
  pinMode(LED_BUILTIN,OUTPUT); digitalWrite(LED_BUILTIN,HIGH);

  EEPROM.begin(EEPROM_SIZE);
  // load config
  g_ssid   = loadString(ADDR_SSID,64);
  g_pass   = loadString(ADDR_PASS,64);
  g_host   = loadString(ADDR_HOST,64);
  g_port   = (EEPROM.read(ADDR_PORT)<<8)|EEPROM.read(ADDR_PORT+1);
  g_apPass = loadString(ADDR_AP_PWD,32);
  if(!g_apPass.length()) g_apPass=DEFAULT_AP_PWD;

  // restore valves
  for(uint8_t i=0;i<VALVE_COUNT;i++){
    pinMode(valvePins[i],OUTPUT);
    bool st = EEPROM.read(ADDR_VALVE_STATE+i)==1;
    g_valveState[i]=st;
    digitalWrite(valvePins[i], st?HIGH:LOW);
  }

  // device ID & network
  generateDeviceId();
  if(g_ssid.length() && wifiConnect()){
    cloudRegister();
  }

  // start portal + server
  portalStart();

  // schedule heartbeat
  heartbeatTicker.attach(30, sendHeartbeat);
}

void loop(){
  dns.processNextRequest();
  http.handleClient();
  beatLED();
  if(WiFi.status()==WL_CONNECTED){
    checkCloudUpdate();
  }
}
