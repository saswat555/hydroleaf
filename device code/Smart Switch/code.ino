#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>

// ───── Relay pins (active-LOW) ───────────────────────────────────────────
const uint8_t RELAY_PINS[8] = {2, 4, 16, 17, 5, 18, 19, 21};

// ───── Backend endpoints ─────────────────────────────────────────────────
static const char BACKEND_HOST[]    = "cloud.hydroleaf.in";
static const char AUTH_PATH[]       = "/api/v1/cloud/authenticate";
static const char CMD_PATH_FMT[]    = "/api/v1/device_comm/switch/%s/commands";
static const char EVT_PATH[]        = "/api/v1/device_comm/switch_event";

// ───── Captive-portal HTML (PROGMEM) ─────────────────────────────────────
const char FORM_HTML[] PROGMEM = R"rawliteral(
<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>HL-Switch Setup</title>
<style>body{font-family:sans-serif;padding:10px}label,input,button{display:block;width:100%%;margin:8px 0}button{background:#007bff;color:#fff;border:none;padding:10px;border-radius:4px}</style>
<h2>Configure Hydroleaf Switch</h2>
<form action="/save" method="POST">
  <label>Wi-Fi SSID<input name="ssid" value="%s"></label>
  <label>Password<input type="password" name="pass" value="%s"></label>
  <label>Cloud Key<input name="key" value="%s"></label>
  <button>Save & Restart</button>
</form>
)rawliteral";

// ───── Globals ─────────────────────────────────────────────────────────
WebServer   web(80);
DNSServer   dns;
Preferences prefs;

String  deviceId, ssid, pass, cloudKey, jwtToken;
bool    apMode     = true;
uint32_t lastPoll  = 0;

// ───── Forward decls ────────────────────────────────────────────────────
void startAP();
bool joinWiFi();
bool cloudAuthenticate();
void pollCommands();
void execToggle(uint8_t channel);
void reportEvent(uint8_t channel, const char* state);

// ───── KV Helpers ──────────────────────────────────────────────────────
template<typename T>
T getPref(const char* key, T defaultVal){
  if constexpr(std::is_same_v<T,String>){
    return prefs.getString(key, defaultVal);
  }
  return defaultVal;
}
template<typename T>
void putPref(const char* key, T v){
  if constexpr(std::is_same_v<T,String>){
    prefs.putString(key, v);
  }
}

// ───── Web Handlers ────────────────────────────────────────────────────
void handleRoot(){
  char buf[512];
  snprintf(buf, sizeof(buf), FORM_HTML,
           ssid.c_str(), pass.c_str(), cloudKey.c_str());
  web.send(200, "text/html", buf);
}
void handleSave(){
  ssid     = web.arg("ssid");
  pass     = web.arg("pass");
  cloudKey = web.arg("key");
  putPref<String>("ssid",     ssid);
  putPref<String>("pass",     pass);
  putPref<String>("cloudKey", cloudKey);
  web.send(200,"text/html","<h3>Saved, rebooting…</h3>");
  delay(1200);
  ESP.restart();
}
void setupWeb(){
  web.on("/", HTTP_GET,  handleRoot);
  web.on("/save", HTTP_POST, handleSave);
  web.onNotFound([](){ web.send(404,"text/plain","Not found"); });
  web.begin();
}

// ───── AP + DNS Captive ─────────────────────────────────────────────────
void startAP(){
  WiFi.mode(WIFI_AP_STA);
  String ap = "HL-SWITCH-" + deviceId;
  WiFi.softAP(ap.c_str(), "configme");
  dns.start(53, "*", WiFi.softAPIP());
}

// ───── STA Join ─────────────────────────────────────────────────────────
bool joinWiFi(){
  WiFi.begin(ssid.c_str(), pass.c_str());
  uint32_t start = millis();
  while(millis() - start < 20000){
    if(WiFi.status()==WL_CONNECTED) return true;
    delay(250);
  }
  WiFi.disconnect();
  return false;
}

// ───── Cloud Auth → JWT ────────────────────────────────────────────────
bool cloudAuthenticate(){
  HTTPClient http;
  String url = String("http://") + BACKEND_HOST + AUTH_PATH;
  http.begin(url);
  http.addHeader("Content-Type","application/json");
  StaticJsonDocument<128> req;
  req["device_id"] = deviceId;
  req["cloud_key"]  = cloudKey;
  String body; serializeJson(req, body);
  int code = http.POST(body);
  if(code!=200){ http.end(); return false; }
  String resp = http.getString();
  http.end();

  StaticJsonDocument<256> doc;
  auto err = deserializeJson(doc, resp);
  if(err || !doc["token"].is<const char*>()) return false;

  jwtToken = doc["token"].as<const char*>();
  putPref<String>("jwt", jwtToken);
  return true;
}

// ───── Poll backend for toggle commands ─────────────────────────────────
void pollCommands(){
  if(WiFi.status()!=WL_CONNECTED || jwtToken.isEmpty()) return;
  HTTPClient http;
  char path[100];
  snprintf(path,sizeof(path), CMD_PATH_FMT, deviceId.c_str());
  String url = String("http://") + BACKEND_HOST + path + "?device_id=" + deviceId;
  http.begin(url);
  http.addHeader("Authorization","Bearer " + jwtToken);
  int code = http.GET();
  if(code==200){
    String resp = http.getString();
    StaticJsonDocument<512> doc;
    if(!deserializeJson(doc, resp)){
      for(auto cmd: doc["commands"].as<JsonArray>()){
        uint8_t ch = cmd["channel"];
        execToggle(ch);
        const char* st = digitalRead(RELAY_PINS[ch-1])==LOW
                         ? "on":"off";
        reportEvent(ch, st);
      }
    }
  } else if(code==401){
    cloudAuthenticate();
  }
  http.end();
}

// ───── Toggle one channel ──────────────────────────────────────────────
void execToggle(uint8_t ch){
  if(ch<1||ch>8) return;
  uint8_t pin = RELAY_PINS[ch-1];
  bool on = digitalRead(pin)==LOW;
  digitalWrite(pin, on?HIGH:LOW);
}

// ───── Report switch_event back to backend ─────────────────────────────
void reportEvent(uint8_t ch, const char* state){
  HTTPClient http;
  String url = String("http://") + BACKEND_HOST + EVT_PATH;
  http.begin(url);
  http.addHeader("Content-Type","application/json");
  http.addHeader("Authorization","Bearer " + jwtToken);

  StaticJsonDocument<128> ev;
  ev["device_id"] = deviceId;
  ev["channel"]   = ch;
  ev["state"]     = state;
  String b; serializeJson(ev,b);
  http.POST(b);
  http.end();
}

// ───── Setup & Loop ────────────────────────────────────────────────────
void setup(){
  Serial.begin(115200);
  prefs.begin("hl-switch", false);

  // load prefs
  deviceId  = prefs.getString("deviceId", "");
  ssid      = prefs.getString("ssid", "");
  pass      = prefs.getString("pass", "");
  cloudKey  = prefs.getString("cloudKey", "");
  jwtToken  = prefs.getString("jwt", "");

  if(deviceId.isEmpty()){
    deviceId = String((uint32_t)ESP.getEfuseMac(), HEX);
    prefs.putString("deviceId", deviceId);
  }

  // init relays OFF (active-LOW)
  for(auto p:RELAY_PINS){
    pinMode(p, OUTPUT);
    digitalWrite(p, HIGH);
  }

  // start captive portal
  startAP();
  setupWeb();

  // try STA + auth
  if(ssid.length() && joinWiFi()){
    if(cloudAuthenticate()){
      apMode = false;
      WiFi.softAPdisconnect(true);
      dns.stop();
    }
  }
}

void loop(){
  dns.processNextRequest();
  web.handleClient();

  // poll every 5 s
  if(millis() - lastPoll > 5000){
    lastPoll = millis();
    if(!apMode) pollCommands();
  }
}
