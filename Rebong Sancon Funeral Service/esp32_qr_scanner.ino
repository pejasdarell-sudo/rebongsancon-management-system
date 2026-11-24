/*
 =========================================================================================
  RebongSancon Funeral Services - Smart QR Scanner ESP32 Gateway (Firebase Cloud HTTPS)
  Laguna University Capstone - Group 15 (Pascual, Pejas, Pineda)
 =========================================================================================

  Features:
  - Connects to Wi-Fi
  - Reads QR Codes via Serial/UART from GM65, Waveshare Barcode Scanner, Grow, or USB-Serial
  - Sends scanned package code via HTTPS POST to Firebase Cloud:
      https://<YOUR_PROJECT_ID>.web.app/api/pos/scan
  - Authenticates with X-API-Key header matching Firebase Secret MOBILE_API_KEY
  - Visual & Sound Feedback: Built-in LED (GPIO 2) and optional Buzzer (GPIO 4)
 =========================================================================================
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>

// 1. Wi-Fi Configuration (Replace with your local Wi-Fi credentials)
const char* ssid = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// 2. Firebase Cloud Server Configuration
// Replace <PROJECT_ID> with your deployed Firebase Hosting domain (e.g., rebongsancondb)
// and set apiKey to the exact value provisioned via `firebase functions:secrets:set MOBILE_API_KEY`
const char* serverEndpoint = "https://rebongsancondb.web.app/api/pos/scan";
const char* apiKey = "REPLACE_WITH_YOUR_MOBILE_API_KEY";

// 3. Pin Assignments
#define LED_PIN 2        // Built-in Blue LED on most ESP32 Dev Boards
#define BUZZER_PIN 4     // Optional active buzzer (set to -1 if not used)
#define RXD2 16          // Hardware Serial2 RX pin for GM65 / Barcode reader
#define TXD2 17          // Hardware Serial2 TX pin for GM65 / Barcode reader

HardwareSerial qrSerial(2);

void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  if (BUZZER_PIN != -1) {
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);
  }

  qrSerial.begin(9600, SERIAL_8N1, RXD2, TXD2);

  Serial.println("\n=======================================================");
  Serial.println(" RebongSancon Funeral Services - ESP32 Cloud QR Gateway");
  Serial.println("=======================================================");
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 25) {
    delay(500);
    Serial.print(".");
    digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(LED_PIN, HIGH);
    Serial.println("\n[SUCCESS] Wi-Fi Connected!");
    Serial.print("ESP32 IP Address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Cloud Endpoint  : ");
    Serial.println(serverEndpoint);
    beepSuccess();
  } else {
    digitalWrite(LED_PIN, LOW);
    Serial.println("\n[WARNING] Wi-Fi not connected. Check SSID/Password.");
  }

  Serial.println("\nReady to scan QR Codes!");
  Serial.println("1. Scan physical package QR tag using the QR scanner module, OR");
  Serial.println("2. Type a package code (e.g. 'CSK-OAK-01') into Serial Monitor and hit Enter.\n");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    digitalWrite(LED_PIN, LOW);
  } else {
    digitalWrite(LED_PIN, HIGH);
  }

  if (qrSerial.available()) {
    String scannedCode = qrSerial.readStringUntil('\r');
    scannedCode.trim();
    if (scannedCode.length() > 0) {
      Serial.print("\n[MODULE SCAN] Detected QR: ");
      Serial.println(scannedCode);
      handleScannedCode(scannedCode);
    }
  }

  if (Serial.available()) {
    String manualCode = Serial.readStringUntil('\n');
    manualCode.trim();
    if (manualCode.length() > 0) {
      Serial.print("\n[MANUAL TEST SCAN] Input: ");
      Serial.println(manualCode);
      handleScannedCode(manualCode);
    }
  }

  delay(20);
}

void handleScannedCode(String qrCode) {
  qrCode.trim();
  qrCode.toUpperCase();

  beepScan();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[ERROR] Cannot send scan: Wi-Fi is not connected.");
    return;
  }

  WiFiClientSecure client;
  // For production hardening, replace setInsecure() with client.setCACert(GOOGLE_TRUST_SERVICES_ROOT_CA)
  client.setInsecure();

  HTTPClient http;
  if (!http.begin(client, serverEndpoint)) {
    Serial.println("[ERROR] Failed to initialize HTTPS connection.");
    beepError();
    return;
  }

  http.setTimeout(10000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", apiKey);

  String jsonPayload = "{\"qrCode\":\"" + qrCode + "\"}";

  Serial.print("Sending HTTPS POST to: ");
  Serial.println(serverEndpoint);
  Serial.print("Payload: ");
  Serial.println(jsonPayload);

  int httpResponseCode = http.POST(jsonPayload);

  if (httpResponseCode > 0) {
    String response = http.getString();
    Serial.print("[HTTPS ");
    Serial.print(httpResponseCode);
    Serial.println("] Response from Firebase Cloud:");
    Serial.println(response);

    if (httpResponseCode == 200 || httpResponseCode == 201) {
      beepSuccess();
      Serial.println("[SUCCESS] Scanned package synced to Firebase Cloud & Firestore!");
    } else {
      beepError();
    }
  } else {
    Serial.print("[ERROR] HTTPS POST failed, error: ");
    Serial.println(http.errorToString(httpResponseCode).c_str());
    beepError();
  }

  http.end();
}

void beepScan() {
  digitalWrite(LED_PIN, LOW);
  if (BUZZER_PIN != -1) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(80);
    digitalWrite(BUZZER_PIN, LOW);
  }
  delay(50);
  digitalWrite(LED_PIN, HIGH);
}

void beepSuccess() {
  if (BUZZER_PIN != -1) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(50);
    digitalWrite(BUZZER_PIN, LOW);
    delay(50);
    digitalWrite(BUZZER_PIN, HIGH);
    delay(100);
    digitalWrite(BUZZER_PIN, LOW);
  }
}

void beepError() {
  if (BUZZER_PIN != -1) {
    for (int i = 0; i < 3; i++) {
      digitalWrite(BUZZER_PIN, HIGH);
      delay(120);
      digitalWrite(BUZZER_PIN, LOW);
      delay(60);
    }
  }
}
