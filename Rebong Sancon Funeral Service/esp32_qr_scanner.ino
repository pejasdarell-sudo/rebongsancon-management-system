/*
 =========================================================================================
  RebongSancon Funeral Services - Smart QR Scanner ESP32 Gateway
  Laguna University Capstone - Group 15 (Pascual, Pejas, Pineda)
 =========================================================================================
  
  Features:
  - Connects to Wi-Fi
  - Reads QR Codes via Serial/UART from GM65, Waveshare Barcode Scanner, Grow, or USB-Serial
  - Can also be tested by typing QR codes into the Arduino Serial Monitor (115200 baud)
  - Sends scanned package code via HTTP POST to Flask API: /api/pos/scan
  - Synchronizes with Firebase Firestore:
      -> The server receives the scan and updates Firestore collection `esp32_active_scan/latest`
      -> The mobile app scanner scans unique QR codes and queries Firestore `caskets/{casket_code}`
  - Visual & Sound Feedback: Built-in LED (GPIO 2) and optional Buzzer (GPIO 4)
 =========================================================================================
*/

#include <WiFi.h>
#include <HTTPClient.h>

// 1. Wi-Fi Configuration (Replace with your local Wi-Fi credentials)
const char* ssid = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// 2. Server Configuration (Replace with the IP address of your Flask server)
// e.g., "http://192.168.1.100:5000/api/pos/scan"
const char* serverEndpoint = "http://192.168.1.50:5000/api/pos/scan";

// 3. Pin Assignments
#define LED_PIN 2        // Built-in Blue LED on most ESP32 Dev Boards
#define BUZZER_PIN 4     // Optional active buzzer (set to -1 if not used)
#define RXD2 16          // Hardware Serial2 RX pin for GM65 / Barcode reader
#define TXD2 17          // Hardware Serial2 TX pin for GM65 / Barcode reader

// Hardware Serial for external QR Scanner Module (GM65 / Waveshare / Grow)
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

  // Initialize Hardware Serial 2 for QR Module (9600 baud is standard for GM65/Waveshare)
  qrSerial.begin(9600, SERIAL_8N1, RXD2, TXD2);

  Serial.println("\n=======================================================");
  Serial.println(" RebongSancon Funeral Services - ESP32 Smart QR Gateway");
  Serial.println("=======================================================");
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 25) {
    delay(500);
    Serial.print(".");
    digitalWrite(LED_PIN, !digitalRead(LED_PIN)); // Blink while connecting
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(LED_PIN, HIGH);
    Serial.println("\n[SUCCESS] Wi-Fi Connected!");
    Serial.print("ESP32 IP Address: ");
    Serial.println(WiFi.localIP());
    beepSuccess();
  } else {
    digitalWrite(LED_PIN, LOW);
    Serial.println("\n[WARNING] Wi-Fi not connected. Check SSID/Password.");
    Serial.println("You can still test offline by typing QR codes into the Serial Monitor.");
  }

  Serial.println("\nReady to scan QR Codes!");
  Serial.println("1. Scan physical package QR tag using the QR scanner module, OR");
  Serial.println("2. Type a package code (e.g. 'CSK-OAK-01') into Serial Monitor and hit Enter.\n");
}

void loop() {
  // Check if Wi-Fi disconnected and attempt reconnection
  if (WiFi.status() != WL_CONNECTED) {
    digitalWrite(LED_PIN, LOW);
  } else {
    digitalWrite(LED_PIN, HIGH);
  }

  // 1. Read from external Hardware Serial QR Scanner Module (GM65 / Waveshare)
  if (qrSerial.available()) {
    String scannedCode = qrSerial.readStringUntil('\r');
    scannedCode.trim();
    if (scannedCode.length() > 0) {
      Serial.print("\n[MODULE SCAN] Detected QR: ");
      Serial.println(scannedCode);
      handleScannedCode(scannedCode);
    }
  }

  // 2. Read from Serial Monitor (for testing / manual input)
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

  HTTPClient http;
  http.begin(serverEndpoint);
  http.addHeader("Content-Type", "application/json");

  // Construct JSON payload matching the Flask endpoint
  String jsonPayload = "{\"qrCode\":\"" + qrCode + "\"}";

  Serial.print("Sending POST to: ");
  Serial.println(serverEndpoint);
  Serial.print("Payload: ");
  Serial.println(jsonPayload);

  int httpResponseCode = http.POST(jsonPayload);

  if (httpResponseCode > 0) {
    String response = http.getString();
    Serial.print("[HTTP ");
    Serial.print(httpResponseCode);
    Serial.println("] Response from Server:");
    Serial.println(response);

    if (httpResponseCode == 200 || httpResponseCode == 201) {
      beepSuccess();
      Serial.println("[SUCCESS] Scanned package synced to Flask and Firebase Firestore!");
    } else {
      beepError();
    }
  } else {
    Serial.print("[ERROR] HTTP POST failed, error: ");
    Serial.println(http.errorToString(httpResponseCode).c_str());
    beepError();
  }

  http.end();
}

// Visual & Audio Indicator Helpers
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

