#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <DNSServer.h>
#include <EEPROM.h>
#include <ArduinoJson.h>
// #include <Update.h> // Update.h is for ESP32, ESP8266 uses ESP8266httpUpdate.h
#include <ESP8266HTTPClient.h>
#include <ESP8266httpUpdate.h>
#include <WiFiClient.h> // Recommended for ESP8266HTTPClient

#pragma GCC optimize("Os")

// Firmware version and device type
static const char* FW_VERSION = "0.0.1";
static const char* DEVICE_TYPE = "valve_controller";

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
#define ADDR_DEVICE_ID  256 // Assuming 32 bytes for device_id (ends at 256+32=288)
#define ADDR_VALVE_STATE 288 // 4 bytes (1 per valve) (ends at 288+4=292)
#define ADDR_ACTIVATION_KEY 292 // 64 bytes for key (ends at 292+64=356)
// EEPROM_SIZE needs to be at least 356. Default is 512, so it's fine.


// Backend paths for OTA and Heartbeat
// g_host and g_port are already used for the cloud connection
static const char HEARTBEAT_PATH[] = "/api/v1/device_comm/heartbeat";
static const char UPDATE_CHECK_PATH[] = "/api/v1/device_comm/update";

// ───── globals ──────────────────────────────────────────────────────────────
ESP8266WebServer server(80);
DNSServer          dns;
WiFiClient         client; // For HTTPClient general use, and ESPhttpUpdate

String  g_ssid, g_pass, g_host, g_deviceId, g_apPass, g_activationKey;
uint16_t g_port = 80;
bool    g_valveState[VALVE_COUNT];
// bool    otaOK = false; // This was for the old push OTA, can be removed or repurposed

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
  <label>Activation Key<input name=akey value="%s"></label>
  <button>Save & Reboot</button>
</form>
)rawliteral";

// ───── HTTP handlers ───────────────────────────────────────────────────────
void handleRoot(){
  char buf[600]; // Increased buffer size for new field
  snprintf(buf,sizeof(buf), FORM_HTML,
           g_ssid.c_str(),
           g_pass.c_str(),
           g_host.c_str(),
           g_port,
           g_apPass.c_str(),
           g_activationKey.c_str()); // Added activation key
  server.send(200,"text/html",buf);
}

void handleSave(){
  if(!server.hasArg("ssid")||!server.hasArg("pass")
  || !server.hasArg("host")||!server.hasArg("port")
  || !server.hasArg("apwd")||!server.hasArg("akey")){ // Added akey check
    server.send(400,"text/plain","Missing fields"); return;
  }
  g_ssid  = server.arg("ssid");
  g_pass  = server.arg("pass");
  g_host  = server.arg("host");
  g_port  = server.arg("port").toInt();
  g_apPass= server.arg("apwd");
  g_activationKey = server.arg("akey"); // Get activation key

  saveString(ADDR_SSID,  g_ssid,    64);
  saveString(ADDR_PASS,  g_pass,    64);
  saveString(ADDR_HOST,  g_host,    64);
  EEPROM.write(ADDR_PORT, (g_port>>8)&0xFF);
  EEPROM.write(ADDR_PORT+1, g_port&0xFF);
  saveString(ADDR_AP_PWD,g_apPass,  32);
  saveString(ADDR_ACTIVATION_KEY, g_activationKey, 64); // Save activation key
  commitCfg();
  server.send(200,"text/html","<h3>Saved. Rebooting…</h3>");
  delay(1500); ESP.restart();
}

// Forward declarations for new OTA functions
void sendHeartbeat();
void checkForUpdate();
void performUpdate(const char* download_url);

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

// Removed handleOTA() and handleOTAUpload() as they are part of the old push OTA mechanism.

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
  // server.on("/update_firmware", HTTP_POST, handleOTA, handleOTAUpload); // Removed OTA push route
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
  g_activationKey = loadString(ADDR_ACTIVATION_KEY, 64); // Load activation key
  }

  // device ID + portal
  genDeviceId();
  startPortal();

  // try join Wi-Fi
  if(g_ssid.length() && joinWiFi()){
    Serial.printf("Joined %s\n", g_ssid.c_str());
    // After successful WiFi, send initial heartbeat and check for update
    sendHeartbeat();
    checkForUpdate();
  }
}

// -----------------------------------------------------------------------------
// HEARTBEAT IMPLEMENTATION
// -----------------------------------------------------------------------------
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED || g_activationKey.isEmpty()) {
    Serial.println(F("[HEARTBEAT] Conditions not met (WiFi disconnected or no Activation Key). Skipping."));
    return;
  }

  Serial.println(F("[HEARTBEAT] Sending heartbeat..."));
  HTTPClient http;
  String url = "http://" + g_host + ":" + String(g_port) + HEARTBEAT_PATH;
  
  http.begin(client, url); // Global client
  http.addHeader(F("Content-Type"), F("application/json"));
  http.addHeader(F("Authorization"), "Bearer " + g_activationKey);

  StaticJsonDocument<192> doc;
  doc["device_id"] = g_deviceId;
  doc["type"] = DEVICE_TYPE;
  doc["version"] = FW_VERSION;

  String requestBody;
  serializeJson(doc, requestBody);

  int httpCode = http.POST(requestBody);
  if (httpCode > 0) {
    String payload = http.getString();
    Serial.print(F("[HEARTBEAT] Response code: "));
    Serial.println(httpCode);
    // Serial.print(F("[HEARTBEAT] Response: ")); // Uncomment for full response
    // Serial.println(payload);
  } else {
    Serial.print(F("[HEARTBEAT] POST failed, error: "));
    Serial.println(http.errorToString(httpCode).c_str());
  }
  http.end();
}

// -----------------------------------------------------------------------------
// OTA UPDATE CHECK IMPLEMENTATION
// -----------------------------------------------------------------------------
void checkForUpdate() {
  if (WiFi.status() != WL_CONNECTED || g_activationKey.isEmpty()) {
    Serial.println(F("[OTA] Conditions not met for update check. Skipping."));
    return;
  }

  Serial.println(F("[OTA] Checking for updates..."));
  HTTPClient http;
  String url = "http://" + g_host + ":" + String(g_port) + UPDATE_CHECK_PATH;
  
  http.begin(client, url); // Global client
  http.addHeader(F("Authorization"), "Bearer " + g_activationKey);
  
  int httpCode = http.GET();
  if (httpCode == HTTP_CODE_OK) {
    String payload = http.getString();
    Serial.print(F("[OTA] Update check response: "));
    Serial.println(payload);
    
    StaticJsonDocument<512> doc; // Adjust size as needed
    DeserializationError error = deserializeJson(doc, payload);

    if (error) {
      Serial.print(F("[OTA] Failed to parse update check JSON: "));
      Serial.println(error.c_str());
      http.end();
      return;
    }

    bool update_available = doc["update_available"].as<bool>();
    if (update_available) {
      const char* download_url = doc["download_url"].as<const char*>();
      if (download_url && strlen(download_url) > 0) {
        Serial.print(F("[OTA] Update available. Download URL: "));
        Serial.println(download_url);
        performUpdate(download_url);
      } else {
        Serial.println(F("[OTA] Update available but no/invalid download URL provided."));
      }
    } else {
      Serial.println(F("[OTA] No update available."));
    }
  } else {
    Serial.print(F("[OTA] Update check GET failed, error code: "));
    Serial.print(httpCode);
    Serial.print(F(" Msg: "));
    Serial.println(http.errorToString(httpCode).c_str());
  }
  http.end();
}

// -----------------------------------------------------------------------------
// OTA PERFORM UPDATE IMPLEMENTATION (ESP8266 specific)
// -----------------------------------------------------------------------------
void performUpdate(const char* download_url) {
  if (WiFi.status() != WL_CONNECTED || g_activationKey.isEmpty()) {
    Serial.println(F("[OTA] Conditions not met for performing update. Skipping."));
    return;
  }
  Serial.print(F("[OTA] Starting update from URL: "));
  Serial.println(download_url);

  // ESP8266httpUpdate.setLedPin(LED_BUILTIN, HIGH); // Optional: LED indication
  ESPhttpUpdate.rebootOnUpdate(true); // Automatically reboot on success
  ESPhttpUpdate.setAuthorization(("Bearer " + g_activationKey).c_str()); // Set Bearer token for download
  
  // It's good practice to set a timeout for the update process.
  // ESPhttpUpdate.setClientTimeout(10000); // 10 seconds, for example. Default is 5000.

  // The client here is the global WiFiClient client;
  t_httpUpdate_return ret = ESPhttpUpdate.update(client, download_url, FW_VERSION);

  switch (ret) {
    case HTTP_UPDATE_FAILED:
      Serial.printf("[OTA] Update failed. Error (%d): %s\n", ESPhttpUpdate.getLastError(), ESPhttpUpdate.getLastErrorString().c_str());
      break;
    case HTTP_UPDATE_NO_UPDATES:
      Serial.println(F("[OTA] No updates found (already on latest version or version check failed)."));
      break;
    case HTTP_UPDATE_OK:
      Serial.println(F("[OTA] Update successful! (Should reboot automatically)"));
      // ESP.restart() is usually handled by rebootOnUpdate(true)
      break;
    default:
      Serial.printf("[OTA] Unknown update status: %d\n", ret);
      break;
  }
}

void loop(){
  dns.processNextRequest();
  server.handleClient();

  // Periodic Heartbeat and Update Check
  static unsigned long lastHeartbeat = 0;
  static unsigned long lastUpdateCheck = 0;
  const unsigned long heartbeatInterval = 5 * 60 * 1000; // 5 minutes
  const unsigned long updateCheckInterval = 60 * 60 * 1000; // 1 hour

  if (WiFi.status() == WL_CONNECTED) {
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
