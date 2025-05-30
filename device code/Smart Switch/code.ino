#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <Update.h>      // For OTA updates
#include <WiFiClient.h>  // For HTTPClient

// Firmware version and device type
static const char* FW_VERSION = "0.0.1";
static const char* DEVICE_TYPE = "smart_switch"; // Ensure this matches DeviceType.SMART_SWITCH

// ───── Relay pins (active-LOW) ───────────────────────────────────────────
const uint8_t RELAY_PINS[8] = {2, 4, 16, 17, 5, 18, 19, 21};

// ───── Backend endpoints ─────────────────────────────────────────────────
static const char BACKEND_HOST[]    = "cloud.hydroleaf.in";
static const char AUTH_PATH[]       = "/api/v1/cloud/authenticate"; // For existing auth
static const char CMD_PATH_FMT[]    = "/api/v1/device_comm/switch/%s/commands"; // For existing commands
static const char EVT_PATH[]        = "/api/v1/device_comm/switch_event";   // For existing events
static const char HEARTBEAT_PATH[] = "/api/v1/device_comm/heartbeat";
static const char UPDATE_CHECK_PATH[] = "/api/v1/device_comm/update";

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
WiFiClient  client; // Global WiFiClient for HTTPClient
HTTPClient  http;   // Global HTTPClient

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
void sendHeartbeat();    // OTA & Heartbeat
void checkForUpdate();   // OTA
void performUpdate(const char* download_url); // OTA

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
      // After successful WiFi and cloud auth, send initial heartbeat and check for update
      sendHeartbeat();
      checkForUpdate();
    }
  }
}

// -----------------------------------------------------------------------------
// HEARTBEAT IMPLEMENTATION
// -----------------------------------------------------------------------------
void sendHeartbeat() {
  if (apMode || cloudKey.isEmpty() || WiFi.status() != WL_CONNECTED) {
    Serial.println("[HEARTBEAT] Conditions not met (AP mode, no cloudKey, or WiFi disconnected). Skipping.");
    return;
  }

  Serial.println("[HEARTBEAT] Sending heartbeat...");
  String url = String("http://") + BACKEND_HOST + HEARTBEAT_PATH;
  
  http.begin(client, url); // Use global client and http objects
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + cloudKey); // Use cloudKey for OTA auth

  StaticJsonDocument<192> doc;
  doc["device_id"] = deviceId;
  doc["type"] = DEVICE_TYPE;
  doc["version"] = FW_VERSION;

  String requestBody;
  serializeJson(doc, requestBody);

  int httpCode = http.POST(requestBody);
  if (httpCode > 0) {
    String payload = http.getString();
    Serial.print("[HEARTBEAT] Response code: ");
    Serial.println(httpCode);
    // Serial.print("[HEARTBEAT] Response: "); // Uncomment for full response
    // Serial.println(payload);
  } else {
    Serial.print("[HEARTBEAT] POST failed, error: ");
    Serial.println(http.errorToString(httpCode).c_str());
  }
  http.end();
}

// -----------------------------------------------------------------------------
// OTA UPDATE CHECK IMPLEMENTATION
// -----------------------------------------------------------------------------
void checkForUpdate() {
  if (apMode || cloudKey.isEmpty() || WiFi.status() != WL_CONNECTED) {
    Serial.println("[OTA] Conditions not met for update check (AP mode, no cloudKey, or WiFi disconnected). Skipping.");
    return;
  }

  Serial.println("[OTA] Checking for updates...");
  String url = String("http://") + BACKEND_HOST + UPDATE_CHECK_PATH;
  
  http.begin(client, url); // Use global client and http objects
  http.addHeader("Authorization", "Bearer " + cloudKey); // Use cloudKey for OTA auth
  
  int httpCode = http.GET();
  if (httpCode == HTTP_CODE_OK) {
    String payload = http.getString();
    Serial.print("[OTA] Update check response: ");
    Serial.println(payload);
    
    StaticJsonDocument<512> doc; // Adjust size as needed
    DeserializationError error = deserializeJson(doc, payload);

    if (error) {
      Serial.print("[OTA] Failed to parse update check JSON: ");
      Serial.println(error.c_str());
      http.end();
      return;
    }

    bool update_available = doc["update_available"].as<bool>();
    if (update_available) {
      const char* download_url = doc["download_url"].as<const char*>();
      if (download_url) {
        Serial.print("[OTA] Update available. Download URL: ");
        Serial.println(download_url);
        performUpdate(download_url);
      } else {
        Serial.println("[OTA] Update available but no download URL provided.");
      }
    } else {
      Serial.println("[OTA] No update available.");
    }
  } else {
    Serial.print("[OTA] Update check GET failed, error code: ");
    Serial.print(httpCode);
    Serial.print(" Msg: ");
    Serial.println(http.errorToString(httpCode).c_str());
  }
  http.end();
}

// -----------------------------------------------------------------------------
// OTA PERFORM UPDATE IMPLEMENTATION
// -----------------------------------------------------------------------------
void performUpdate(const char* download_url) {
  if (apMode || cloudKey.isEmpty() || WiFi.status() != WL_CONNECTED) {
    Serial.println("[OTA] Conditions not met for performing update. Skipping.");
    return;
  }
  Serial.print("[OTA] Starting update from URL: ");
  Serial.println(download_url);

  http.begin(client, download_url); // Use global client and http objects
  http.addHeader("Authorization", "Bearer " + cloudKey); // Auth for the pull endpoint

  int httpCode = http.GET();
  if (httpCode == HTTP_CODE_OK) {
    int contentLength = http.getSize();
    if (contentLength <= 0) {
      Serial.println("[OTA] Content length error or zero.");
      http.end();
      return;
    }
    Serial.print("[OTA] Update size: ");
    Serial.print(contentLength);
    Serial.println(" bytes.");

    if (!Update.begin(contentLength)) {
      Serial.print("[OTA] Not enough space to begin OTA. Error: ");
      Serial.println(Update.errorString());
      http.end();
      return;
    }
    Serial.println("[OTA] Update.begin() successful.");

    WiFiClient& stream = http.getStream(); // Get stream from global http object
    size_t written = Update.writeStream(stream);

    if (written == contentLength) {
      Serial.println("[OTA] Update written successfully.");
    } else {
      Serial.print("[OTA] Update write failed. Wrote ");
      Serial.print(written);
      Serial.print("/");
      Serial.print(contentLength);
      Serial.print(" bytes. Error: ");
      Serial.println(Update.errorString());
      // Update.abort(); // Not strictly necessary before Update.end(false)
      http.end(); // End http connection before attempting to end Update or restarting
      Update.end(false); // End the update (false indicates failure)
      return;
    }

    if (!Update.end(true)) { // true to set boot partition to new sketch
      Serial.print("[OTA] Error occurred in Update.end(): ");
      Serial.println(Update.errorString());
    } else {
      Serial.println("[OTA] Update successful! Rebooting...");
      delay(1000); // Short delay for serial message to send
      ESP.restart();
    }
  } else {
    Serial.print("[OTA] Download failed, HTTP error: ");
    Serial.println(http.errorToString(httpCode).c_str());
  }
  http.end(); // Ensure http connection is closed in all paths
}

void loop(){
  dns.processNextRequest();
  web.handleClient();

  // poll every 5 s
  if(millis() - lastPoll > 5000 && !apMode){ // Only poll if not in AP mode
    lastPoll = millis();
    pollCommands();
  }

  // Periodic Heartbeat and Update Check
  static unsigned long lastHeartbeat = 0;
  static unsigned long lastUpdateCheck = 0;
  const unsigned long heartbeatInterval = 5 * 60 * 1000; // 5 minutes
  const unsigned long updateCheckInterval = 60 * 60 * 1000; // 1 hour

  if (!apMode) { // Only run if connected to WiFi and authenticated (not in AP mode)
      if (millis() - lastHeartbeat > heartbeatInterval) {
          lastHeartbeat = millis();
          sendHeartbeat();
      }
      if (millis() - lastUpdateCheck > updateCheckInterval) {
          lastUpdateCheck = millis();
          checkForUpdate();
      }
  }
}
