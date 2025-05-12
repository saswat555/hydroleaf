#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <DNSServer.h>
#include <EEPROM.h>
#include <ArduinoJson.h>
#include <Update.h>

#pragma GCC optimize("Os")

// ───── hardware ─────────────────────────────────────────────────────────────
static const uint8_t VALVE_COUNT   = 4;
static const uint8_t valvePins[VALVE_COUNT] = { D1, D2, D5, D6 };

// ───── EEPROM layout ────────────────────────────────────────────────────────
#define EEPROM_SIZE     512
#define ADDR_SSID       0
#define ADDR_PASS       64
#define ADDR_HOST       128
#define ADDR_PORT       192
#define ADDR_AP_PWD     194
#define ADDR_DEVICE_ID  256
#define ADDR_VALVE_STATE 336

// ───── globals ──────────────────────────────────────────────────────────────
ESP8266WebServer server(80);
DNSServer          dns;

String  g_ssid, g_pass, g_host, g_deviceId, g_apPass;
uint16_t g_port = 80;
bool    g_valveState[VALVE_COUNT];
bool    otaOK = false;

// ───── EEPROM helpers ───────────────────────────────────────────────────────
void saveString(int addr, const String &s, int maxLen){
  for(int i=0;i<maxLen;i++){
    EEPROM.write(addr + i, i < s.length() ? s[i] : '\0');
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
void commitCfg(){ EEPROM.commit(); }

// ───── captive‐portal HTML ──────────────────────────────────────────────────
const char FORM_HTML[] PROGMEM = R"rawliteral(
<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Valve­CTL Setup</title>
<style>
 body{font-family:sans-serif;padding:10px}
 label,input,button{display:block;width:100%%;margin:8px 0}
 button{background:#007bff;color:#fff;border:none;padding:10px;border-radius:4px}
</style>
<h2>Configure Valve­CTL</h2>
<form action=/save method=POST>
  <label>Wi-Fi SSID<input  name=ssid value="%s"></label>
  <label>Wi-Fi Pass<input type=password  name=pass value="%s"></label>
  <label>Backend Host<input name=host value="%s"></label>
  <label>Backend Port<input name=port value="%u"></label>
  <label>AP Password<input type=password name=apwd value="%s"></label>
  <button>Save & Reboot</button>
</form>
)rawliteral";

// ───── HTTP handlers ───────────────────────────────────────────────────────
void handleRoot(){
  char buf[512];
  snprintf(buf,sizeof(buf), FORM_HTML,
           g_ssid.c_str(),
           g_pass.c_str(),
           g_host.c_str(),
           g_port,
           g_apPass.c_str());
  server.send(200,"text/html",buf);
}

void handleSave(){
  if(!server.hasArg("ssid")||!server.hasArg("pass")
  || !server.hasArg("host")||!server.hasArg("port")
  || !server.hasArg("apwd")){
    server.send(400,"text/plain","Missing fields"); return;
  }
  g_ssid  = server.arg("ssid");
  g_pass  = server.arg("pass");
  g_host  = server.arg("host");
  g_port  = server.arg("port").toInt();
  g_apPass= server.arg("apwd");
  saveString(ADDR_SSID,  g_ssid,    64);
  saveString(ADDR_PASS,  g_pass,    64);
  saveString(ADDR_HOST,  g_host,    64);
  EEPROM.write(ADDR_PORT, (g_port>>8)&0xFF);
  EEPROM.write(ADDR_PORT+1, g_port&0xFF);
  saveString(ADDR_AP_PWD,g_apPass,  32);
  commitCfg();
  server.send(200,"text/html","<h3>Saved. Rebooting…</h3>");
  delay(1500); ESP.restart();
}

void handleState(){
  StaticJsonDocument<128> doc;
  doc["device_id"] = g_deviceId;
  auto arr = doc.createNestedArray("valves");
  for(uint8_t i=0;i<VALVE_COUNT;i++){
    JsonObject v = arr.createNestedObject();
    v["id"]    = i+1;
    v["state"] = g_valveState[i] ? "on":"off";
  }
  String out; serializeJson(doc,out);
  server.send(200,"application/json",out);
}

void handleToggle(){
  if(server.method()!=HTTP_POST){
    server.send(405);
    return;
  }
  StaticJsonDocument<64> req;
  if(deserializeJson(req, server.arg("plain"))!=DeserializationError::Ok
    || !req.containsKey("valve_id")){
    server.send(400,"application/json","{\"error\":\"bad payload\"}");
    return;
  }
  int v = req["valve_id"];
  if(v<1||v>VALVE_COUNT){
    server.send(400,"application/json","{\"error\":\"valve_id out of range\"}");
    return;
  }
  uint8_t idx = v-1;
  g_valveState[idx] = !g_valveState[idx];
  digitalWrite(valvePins[idx], g_valveState[idx]?LOW:HIGH);
  EEPROM.write(ADDR_VALVE_STATE+idx, g_valveState[idx]);
  commitCfg();

  StaticJsonDocument<64> res;
  res["valve_id"]=v;
  res["new_state"] = g_valveState[idx]?"on":"off";
  String o; serializeJson(res,o);
  server.send(200,"application/json",o);
}

void handleOTA(){
  // end response
  if(otaOK) server.send(200,"text/plain","OK");
  else      server.send(500,"text/plain","FAIL");
  delay(100);
  ESP.restart();
}
void handleOTAUpload(){
  HTTPUpload &up = server.upload();
  if(up.status==UPLOAD_FILE_START){
    uint32_t maxSz = (ESP.getFreeSketchSpace() - 0x1000) & 0xFFFFF000;
    Update.begin(maxSz);
  }
  else if(up.status==UPLOAD_FILE_WRITE){
    Update.write(up.buf, up.currentSize);
  }
  else if(up.status==UPLOAD_FILE_END){
    otaOK = Update.end(true);
  }
}

// redirect all others to portal
void handleNotFound(){
  server.sendHeader("Location", "http://192.168.4.1", true);
  server.send(302);
}

void setupRoutes(){
  server.on("/",             HTTP_GET,  handleRoot);
  server.on("/save",         HTTP_POST, handleSave);
  server.on("/state",        HTTP_GET,  handleState);
  server.on("/toggle",       HTTP_POST, handleToggle);
  server.on("/update_firmware", HTTP_POST, handleOTA,
            handleOTAUpload);
  server.onNotFound(handleNotFound);
  server.begin();
}

// ───── Wi-Fi & captive portal ─────────────────────────────────────────────
bool joinWiFi(){
  WiFi.mode(WIFI_AP_STA);
  WiFi.begin(g_ssid, g_pass);
  for(int i=0;i<40;i++){
    if(WiFi.status()==WL_CONNECTED) return true;
    delay(250);
  }
  return false;
}
void startPortal(){
  WiFi.softAP(g_deviceId.c_str(), g_apPass.c_str());
  dns.start(DNS_PORT,"*",WiFi.softAPIP());
  setupRoutes();
}

// ───── init device ID & restore config ────────────────────────────────────
void genDeviceId(){
  g_deviceId = loadString(ADDR_DEVICE_ID,32);
  if(g_deviceId.length()) return;
  g_deviceId = "valve-" + String(ESP.getChipId(), HEX);
  saveString(ADDR_DEVICE_ID, g_deviceId, 32);
  commitCfg();
}

// ───── Arduino setup & loop ──────────────────────────────────────────────
void setup(){
  Serial.begin(115200);
  EEPROM.begin(EEPROM_SIZE);

  // load
  g_ssid   = loadString(ADDR_SSID,  64);
  g_pass   = loadString(ADDR_PASS,  64);
  g_host   = loadString(ADDR_HOST,  64);
  uint16_t pH = EEPROM.read(ADDR_PORT);
  uint16_t pL = EEPROM.read(ADDR_PORT+1);
  g_port    = (pH<<8)|pL;
  g_apPass  = loadString(ADDR_AP_PWD,32);

  // restore valves
  for(uint8_t i=0;i<VALVE_COUNT;i++){
    pinMode(valvePins[i], OUTPUT);
    bool st = EEPROM.read(ADDR_VALVE_STATE+i);
    g_valveState[i]=st;
    digitalWrite(valvePins[i], st?LOW:HIGH);
  }

  // device ID + portal
  genDeviceId();
  startPortal();

  // try join Wi-Fi
  if(g_ssid.length() && joinWiFi()){
    // optionally: sync time or notify cloud if you want
    Serial.printf("Joined %s\n", g_ssid.c_str());
  }
}

void loop(){
  dns.processNextRequest();
  server.handleClient();
}
