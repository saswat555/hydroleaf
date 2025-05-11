#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>

// ─── CONFIG ────────────────────────────────────────────────────────────
// Your Wi-Fi credentials:
const char *WIFI_SSID = "YOUR_SSID";
const char *WIFI_PASSWORD = "YOUR_PASS";

// Your Hydroleaf Cloud host (HTTP) & valve-device token:
const char *CLOUD_HOST = "http://cloud.hydroleaf.in";
const char *DEVICE_TOKEN = "YOUR_VALVE_DEVICE_TOKEN";

// Unique ID for this switch (must match backend registration):
// e.g. "SWITCH_ABC123"
const char *DEVICE_ID = "SWITCH_<unique_id>";

// HTTP server port
const uint16_t HTTP_PORT = 80;

// ─── HARDWARE ──────────────────────────────────────────────────────────
// Eight relay pins (active-low)
const uint8_t RELAY_PINS[8] = {2, 4, 5, 12, 13, 14, 15, 16};

WebServer server(HTTP_PORT);
Preferences prefs;

// ─── HELPERS ──────────────────────────────────────────────────────────
void setRelay(uint8_t idx, bool on)
{
    // idx: 1–8
    uint8_t pin = RELAY_PINS[idx - 1];
    digitalWrite(pin, on ? LOW : HIGH);
}

bool reportValveEvent(uint8_t vid, const char *state)
{
    HTTPClient http;
    String url = String(CLOUD_HOST) + "/api/v1/device_comm/valve_event";
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

    StaticJsonDocument<128> doc;
    doc["device_id"] = DEVICE_ID;
    doc["valve_id"] = vid;
    doc["state"] = state;
    String body;
    serializeJson(doc, body);

    int code = http.POST(body);
    http.end();
    return (code >= 200 && code < 300);
}

// ─── HTTP HANDLERS ────────────────────────────────────────────────────

// GET /discovery
void handleDiscovery()
{
    StaticJsonDocument<256> doc;
    doc["device_id"] = DEVICE_ID;
    doc["name"] = "Hydroleaf 8-Ch Switch";
    doc["type"] = "valve_controller";
    doc["version"] = "1.0.0";
    doc["status"] = "online";
    doc["ip"] = WiFi.localIP().toString();
    String out;
    serializeJson(doc, out);
    server.send(200, "application/json", out);
}

// GET /state
void handleState()
{
    StaticJsonDocument<256> doc;
    doc["device_id"] = DEVICE_ID;
    JsonArray arr = doc.createNestedArray("valves");
    for (uint8_t i = 1; i <= 8; i++)
    {
        JsonObject v = arr.createNestedObject();
        v["id"] = i;
        v["state"] = (digitalRead(RELAY_PINS[i - 1]) == LOW ? "on" : "off");
    }
    String out;
    serializeJson(doc, out);
    server.send(200, "application/json", out);
}

// POST /toggle
void handleToggle()
{
    if (!server.hasArg("plain"))
    {
        server.send(400, "application/json", "{\"detail\":\"Missing JSON\"}");
        return;
    }
    StaticJsonDocument<128> req;
    auto err = deserializeJson(req, server.arg("plain"));
    if (err)
    {
        server.send(400, "application/json", "{\"detail\":\"Bad JSON\"}");
        return;
    }
    uint8_t vid = req["valve_id"] | 0;
    if (vid < 1 || vid > 8)
    {
        server.send(400, "application/json", "{\"detail\":\"Invalid valve_id\"}");
        return;
    }
    // toggle
    bool nowOn = (digitalRead(RELAY_PINS[vid - 1]) == HIGH);
    setRelay(vid, !nowOn);

    // build response
    StaticJsonDocument<128> resp;
    resp["device_id"] = DEVICE_ID;
    resp["valve_id"] = vid;
    resp["new_state"] = (!nowOn ? "on" : "off");
    String out;
    serializeJson(resp, out);
    server.send(200, "application/json", out);

    // report event upstream (best-effort)
    reportValveEvent(vid, !nowOn ? "on" : "off");
}

// 404
void handleNotFound()
{
    server.send(404, "application/json", "{\"detail\":\"Not found\"}");
}

// ─── SETUP & LOOP ──────────────────────────────────────────────────────
void setup()
{
    Serial.begin(115200);

    // Relay pins
    for (auto p : RELAY_PINS)
    {
        pinMode(p, OUTPUT);
        digitalWrite(p, HIGH); // OFF
    }

    // Wi-Fi
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("Wi-Fi connecting");
    while (WiFi.status() != WL_CONNECTED)
    {
        delay(500);
        Serial.print('.');
    }
    Serial.println("\nWi-Fi up: " + WiFi.localIP().toString());

    // HTTP routes
    server.on("/discovery", HTTP_GET, handleDiscovery);
    server.on("/state", HTTP_GET, handleState);
    server.on("/toggle", HTTP_POST, handleToggle);
    server.onNotFound(handleNotFound);
    server.begin();
}

void loop()
{
    server.handleClient();
}
