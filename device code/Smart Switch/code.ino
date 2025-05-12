#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <DNSServer.h>
#include <ESP8266HTTPClient.h>
#include <ArduinoJson.h>
#include <LittleFS.h>

// ───── configuration ────────────────────────────────────────────────
static const char BACKEND_HOST[]   = "cloud.hydroleaf.in";
static const char AUTH_PATH[]      = "/api/v1/cloud/authenticate";
static const char CMD_PATH_FMT[]   = "/api/v1/device_comm/valve/%s/commands";
static const char EVT_PATH[]       = "/api/v1/device_comm/valve_event";

// ───── pins (active-LOW) ─────────────────────────────────────────────
const uint8_t RELAY_PINS[8] = { D1, D2, D5, D6, D7, D0, D3, D8 };

// ───── globals ───────────────────────────────────────────────────────
String deviceId, ssid, pass, cloudKey, jwtToken;
ESP8266WebServer  http(80);
DNSServer          dns;
unsigned long      lastPoll = 0;

// ───── LittleFS KV ───────────────────────────────────────────────────
String getKV(const char *path) {
  File f = LittleFS.open(path, "r");
  if(!f) return "";
  String s = f.readString(); f.close();
  s.trim(); return s;
}
void putKV(const char *path, const String &v) {
  File f = LittleFS.open(path, "w");
  if(!f) return;
  f.println(v); f.close();
}

// ───── captive-portal HTML ───────────────────────────────────────────
const char HTML_FORM[] PROGMEM = R"rawliteral(
<!doctype html>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Setup HL-SWITCH</title>
<style>
 body{font-family:sans-serif;padding:10px}
 label,input,button{display:block;width:100%%;margin:8px 0}
 button{background:#007bff;color:#fff;border:none;padding:10px;border-radius:4px}
</style>
<h2>Configure</h2>
<form action="/save" method="POST">
  <label>Wi-Fi SSID<input name="ssid" value="%s"></label>
  <label>Wi-Fi Pass<input type="password" name="pass" value="%s"></label>
  <label>Cloud Key<input name="key" value="%s"></label>
  <button>Save & Restart</button>
</form>
)rawliteral";

// ───── Web handlers ──────────────────────────────────────────────────
void handleRoot() {
  char buf[512];
  snprintf(buf,sizeof(buf),HTML_FORM,
           ssid.c_str(), pass.c_str(), cloudKey.c_str());
  http.send(200, "text/html", buf);
}
void handleSave() {
  ssid     = http.arg("ssid");
  pass     = http.arg("pass");
  cloudKey = http.arg("key");
  putKV("/ssid", ssid);
  putKV("/pass", pass);
  putKV("/key",  cloudKey);
  http.send(200,"text/html","<h3>Saved. Rebooting…</h3>");
  delay(1500);
  ESP.restart();
}
void setupWeb() {
  http.on("/",        HTTP_GET,  handleRoot);
  http.on("/save",    HTTP_POST, handleSave);
  http.onNotFound([](){ http.send(404,"text/plain","Not found"); });
  http.begin();
}

// ───── start AP + DNS for captive portal ─────────────────────────────
void startCaptive() {
  String ap = "HL-SWITCH-" + deviceId.substring(deviceId.length()-4);
  WiFi.softAP(ap, "configme");
  dns.start(53, "*", WiFi.softAPIP());
}

// ───── join Wi-Fi in parallel with AP ────────────────────────────────
bool joinWiFi() {
  WiFi.begin(ssid, pass);
  for(int i=0;i<40;i++){
    if(WiFi.status()==WL_CONNECTED) return true;
    delay(250);
  }
  return false;
}

// ───── cloud authenticate → JWT ──────────────────────────────────────
bool cloudAuth() {
  HTTPClient c;
  String url = String("http://") + BACKEND_HOST + AUTH_PATH;
  c.begin(url);
  c.addHeader("Content-Type","application/json");
  StaticJsonDocument<128> req;
  req["device_id"] = deviceId;
  req["cloud_key"] = cloudKey;
  String body; serializeJson(req,body);
  if(c.POST(body) != 200){
    c.end();
    return false;
  }
  String resp = c.getString(); c.end();
  StaticJsonDocument<256> d;
  if(deserializeJson(d,resp)!=DeserializationError::Ok) return false;
  jwtToken = d["token"].as<String>();
  putKV("/token", jwtToken);
  return true;
}

// ───── execute a single toggle ───────────────────────────────────────
void execToggle(uint8_t vid) {
  if(vid<1||vid>8) return;
  uint8_t pin = RELAY_PINS[vid-1];
  bool on = digitalRead(pin)==LOW;
  digitalWrite(pin, on?HIGH:LOW);
}

// ───── poll cloud for pending toggles ───────────────────────────────
void pollCommands() {
  if(WiFi.status()!=WL_CONNECTED || jwtToken.isEmpty()) return;
  HTTPClient c;
  char p[80];
  snprintf(p,sizeof(p),CMD_PATH_FMT,deviceId.c_str());
  String url = String("http://") + BACKEND_HOST + p + "?device_id=" + deviceId;
  c.begin(url);
  c.addHeader("Authorization","Bearer "+jwtToken);
  if(c.GET()==200){
    String rsp = c.getString();
    c.end();
    StaticJsonDocument<256> doc;
    if(deserializeJson(doc,rsp)!=DeserializationError::Ok) return;
    for(auto cmd: doc["commands"].as<JsonArray>()){
      uint8_t vid = cmd["valve_id"];
      execToggle(vid);
      // report back
      HTTPClient r;
      String u2 = String("http://") + BACKEND_HOST + EVT_PATH;
      r.begin(u2);
      r.addHeader("Content-Type","application/json");
      r.addHeader("Authorization","Bearer "+jwtToken);
      StaticJsonDocument<128> ev;
      ev["device_id"]=deviceId;
      ev["valve_id"]=vid;
      ev["state"]  = (digitalRead(RELAY_PINS[vid-1])==LOW?"on":"off");
      String b2; serializeJson(ev,b2);
      r.POST(b2);
      r.end();
    }
  } else {
    c.end();
  }
}

void setup() {
  Serial.begin(115200);
  LittleFS.begin();

  // init relays OFF
  for(auto p:RELAY_PINS){
    pinMode(p,OUTPUT);
    digitalWrite(p,HIGH);
  }

  // load/generate IDs
  deviceId = getKV("/id");
  if(deviceId.isEmpty()){
    deviceId = "SW_"+String(ESP.getChipId(),HEX);
    putKV("/id",deviceId);
  }
  ssid     = getKV("/ssid");
  pass     = getKV("/pass");
  cloudKey = getKV("/key");
  jwtToken = getKV("/token");

  // start AP/captive + web UI
  WiFi.mode(WIFI_AP_STA);
  startCaptive();
  setupWeb();

  // attempt join + auth
  if(ssid.length() && joinWiFi()){
    cloudAuth();
  }
}

void loop() {
  dns.processNextRequest();
  http.handleClient();
  if(millis() - lastPoll > 5000){
    lastPoll = millis();
    pollCommands();
  }
}
