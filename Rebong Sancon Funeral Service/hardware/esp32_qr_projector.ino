/*
 =========================================================================================
  RebongSancon Funeral Services - ESP32 Smart QR Projector (Firebase Cloud HTTPS Polling)
  Laguna University Capstone - Group 15 (Pascual, Pejas, Pineda)
 =========================================================================================

  Why Cloud Polling?
  - Firebase Cloud servers cannot send direct HTTP POSTs to a private LAN IP (192.168.x.x).
  - Instead, this ESP32 polls `GET https://<PROJECT_ID>.web.app/api/pos/qr/current` every
    2.5 seconds using HTTPS + X-API-Key, renders any new projected QR package, and confirms
    hardware rendering via `POST https://<PROJECT_ID>.web.app/api/pos/qr/ack`.
  - Also keeps local HTTP status endpoint on port 80 for local diagnostics.
 =========================================================================================
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>

// 1. Wi-Fi Configuration
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// 2. Firebase Cloud Configuration
const char* cloudCurrentQrUrl = "https://rebongsancondb.web.app/api/pos/qr/current";
const char* cloudAckQrUrl     = "https://rebongsancondb.web.app/api/pos/qr/ack";
const char* apiKey            = "REPLACE_WITH_YOUR_MOBILE_API_KEY";

const unsigned long POLL_INTERVAL_MS = 2500;
unsigned long lastPollMs = 0;

// 3. Hardware Pins
#define LED_PIN     2
#define BUZZER_PIN  4

WebServer server(80);

String activeQrId        = "";
String activeCasketCode  = "NONE";
String activePackageName = "Ready for Projection";
String activeAmount      = "";
String activeQrPayload   = "NONE";

void beepSuccess() {
  if (BUZZER_PIN != -1) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(100);
    digitalWrite(BUZZER_PIN, LOW);
  }
}

void renderProjectionDisplay(String qrId, String code, String name, String amount, String payload) {
  Serial.println("\n*************************************************************");
  Serial.println("  [ESP32 CLOUD PROJECTOR] QR CODE PROJECTION DISPLAY UPDATED ");
  Serial.println("*************************************************************");
  Serial.print("  QR ID        : "); Serial.println(qrId);
  Serial.print("  Package Code : "); Serial.println(code);
  Serial.print("  Package Name : "); Serial.println(name);
  Serial.print("  Price        : "); Serial.println(amount);
  Serial.print("  QR Payload   : "); Serial.println(payload);
  Serial.println("-------------------------------------------------------------");
  Serial.println("  >> Ready for Customer Mobile App Camera Scanning <<        ");
  Serial.println("*************************************************************\n");
}

String extractJsonField(String body, String fieldName) {
  String searchKey = "\"" + fieldName + "\"";
  int keyIndex = body.indexOf(searchKey);
  if (keyIndex == -1) return "";

  int colonIndex = body.indexOf(":", keyIndex + searchKey.length());
  if (colonIndex == -1) return "";

  int pos = colonIndex + 1;
  while (pos < (int)body.length() && (body[pos] == ' ' || body[pos] == '\t')) pos++;

  if (pos < (int)body.length() && body[pos] == '"') {
    int endQuote = body.indexOf("\"", pos + 1);
    if (endQuote != -1) return body.substring(pos + 1, endQuote);
  } else {
    int endIndex = body.indexOf(",", pos);
    int braceIndex = body.indexOf("}", pos);
    if (endIndex == -1 || (braceIndex != -1 && braceIndex < endIndex)) endIndex = braceIndex;
    if (endIndex != -1) {
      String val = body.substring(pos, endIndex);
      val.trim();
      return val;
    }
  }
  return "";
}

void sendHardwareAck(String qrId) {
  if (WiFi.status() != WL_CONNECTED || qrId.length() == 0) return;

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  if (!http.begin(client, cloudAckQrUrl)) return;

  http.setTimeout(8000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", apiKey);

  String payload = "{\"qr_id\":\"" + qrId + "\",\"client_ip\":\"" + WiFi.localIP().toString() + "\",\"device\":\"ESP32-Projector\"}";
  int code = http.POST(payload);
  Serial.print("[ESP32 ACK] HTTP "); Serial.println(code);
  http.end();
}

void pollCloudDisplayQr() {
  if (WiFi.status() != WL_CONNECTED) return;

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  if (!http.begin(client, cloudCurrentQrUrl)) return;

  http.setTimeout(8000);
  http.addHeader("X-API-Key", apiKey);

  int httpCode = http.GET();
  if (httpCode == 200) {
    String body = http.getString();
    String hasQr = extractJsonField(body, "has_qr");
    String qrId  = extractJsonField(body, "qr_id");

    if (hasQr == "true" && qrId.length() > 0 && qrId != activeQrId) {
      activeQrId        = qrId;
      activeQrPayload   = extractJsonField(body, "qr_text");
      activeCasketCode  = activeQrPayload;
      activePackageName = extractJsonField(body, "title");
      activeAmount      = extractJsonField(body, "amount");

      digitalWrite(LED_PIN, HIGH);
      beepSuccess();
      renderProjectionDisplay(activeQrId, activeCasketCode, activePackageName, activeAmount, activeQrPayload);
      sendHardwareAck(activeQrId);
    } else if (hasQr == "false" && activeQrId.length() > 0) {
      activeQrId        = "";
      activeCasketCode  = "NONE";
      activePackageName = "SYSTEM READY (Standby)";
      activeAmount      = "";
      activeQrPayload   = "NONE";
      Serial.println("[ESP32 CLOUD PROJECTOR] Display returned to Standby mode.");
    }
  }
  http.end();
}

void handleRoot() {
  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>ESP32 Cloud QR Projector</title></head>";
  html += "<body style='font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:30px;text-align:center;'>";
  html += "<h2 style='color:#eab308;'>RebongSancon Funeral Services</h2>";
  html += "<h3>ESP32 Cloud QR Projector Unit</h3>";
  html += "<p><strong>Local IP:</strong> " + WiFi.localIP().toString() + "</p>";
  html += "<p><strong>Cloud Endpoint:</strong> <code>" + String(cloudCurrentQrUrl) + "</code></p>";
  html += "<p><strong>Current Projected Package:</strong> " + activePackageName + " (" + activeCasketCode + ")</p>";
  html += "<p><strong>QR Payload:</strong> <code>" + activeQrPayload + "</code></p>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  if (BUZZER_PIN != -1) {
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);
  }

  Serial.println("\n=======================================================");
  Serial.println(" RebongSancon Funeral Services - ESP32 Cloud Projector ");
  Serial.println("=======================================================");
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(LED_PIN, HIGH);
    Serial.println("\n[SUCCESS] Wi-Fi Connected! IP: " + WiFi.localIP().toString());
    beepSuccess();
  } else {
    Serial.println("\n[WARNING] Wi-Fi connection timed out.");
  }

  server.on("/", HTTP_GET, handleRoot);
  server.begin();
}

void loop() {
  server.handleClient();
  unsigned long now = millis();
  if (now - lastPollMs >= POLL_INTERVAL_MS) {
    lastPollMs = now;
    pollCloudDisplayQr();
  }
  delay(5);
}
