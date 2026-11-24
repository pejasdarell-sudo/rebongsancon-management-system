/*
 =========================================================================================
  RebongSancon Funeral Services - ESP32 Smart QR Projector & Display Gateway
  Laguna University Capstone - Group 15 (Pascual, Pejas, Pineda)
 =========================================================================================
  
  Features:
  - Connects to Local Wi-Fi (Configurable SSID & Password)
  - Runs an HTTP Web Server on Port 80
  - Listens for POST /display from Flask POS:
      Payload: {"casket_code": "CSK-OAK-01", "name": "...", "base_price": 85000, "qr_payload": "CSK-OAK-01"}
  - Displays package details and projects the QR code for customer mobile app scanning
  - Supports Serial Output (115200 baud) for hardware projector / HDMI / TFT displays
  - Status endpoints:
      -> GET  /         : Device status and active projected package
      -> POST /display  : Receive and project new package QR code
      -> POST /clear    : Clear projected display
 =========================================================================================
*/

#include <WiFi.h>
#include <WebServer.h>

// -----------------------------------------------------------------------------
// 1. Wi-Fi Configuration (Replace with your local Wi-Fi credentials)
// -----------------------------------------------------------------------------
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// -----------------------------------------------------------------------------
// 2. Hardware Pin Assignments & Peripherals
// -----------------------------------------------------------------------------
#define LED_PIN     2     // Built-in Blue LED for status indication
#define BUZZER_PIN  4     // Optional active buzzer pin (set to -1 if not connected)

// -----------------------------------------------------------------------------
// 3. Web Server on Port 80
// -----------------------------------------------------------------------------
WebServer server(80);

// Global state holding the currently projected package
String activeCasketCode = "NONE";
String activePackageName = "Ready for Projection";
float  activeBasePrice   = 0.0;
String activeQrPayload   = "NONE";
unsigned long lastProjectedTime = 0;

// Helper: Sound short beep on buzzer
void beepSuccess() {
  if (BUZZER_PIN != -1) {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(100);
    digitalWrite(BUZZER_PIN, LOW);
  }
}

// -----------------------------------------------------------------------------
// 4. Projection & Display Rendering Hooks
// -----------------------------------------------------------------------------
void renderProjectionDisplay(String code, String name, float price, String payload) {
  // Serial Terminal Display Projection Output
  Serial.println("\n*************************************************************");
  Serial.println("  [ESP32 PROJECTOR] NEW QR CODE PROJECTION DISPLAY UPDATED  ");
  Serial.println("*************************************************************");
  Serial.print("  Package Code : "); Serial.println(code);
  Serial.print("  Package Name : "); Serial.println(name);
  Serial.print("  Base Price   : PHP "); Serial.println(price, 2);
  Serial.print("  QR Payload   : "); Serial.println(payload);
  Serial.println("-------------------------------------------------------------");
  Serial.println("  >> Ready for Customer Mobile App Camera Scanning <<        ");
  Serial.println("*************************************************************\n");

  /*
    OPTIONAL HARDWARE DISPLAY ATTACHMENT:
    If using an SSD1306 OLED, ST7789 TFT screen, or HDMI/Composite projector:
    
    // Example for Adafruit_SSD1306:
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("RebongSancon POS");
    display.println(name);
    display.printf("PHP %.2f\n", price);
    // Draw QR bitmap / matrix:
    // qrcode.render(&display, payload);
    display.display();
  */
}

// -----------------------------------------------------------------------------
// 5. HTTP Request Handlers
// -----------------------------------------------------------------------------

// GET / - Root Status Check
void handleRoot() {
  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>ESP32 QR Projector</title></head>";
  html += "<body style='font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:30px;text-align:center;'>";
  html += "<h2 style='color:#eab308;'>RebongSancon Funeral Services</h2>";
  html += "<h3>ESP32 Smart QR Projector Unit</h3>";
  html += "<p><strong>IP Address:</strong> " + WiFi.localIP().toString() + "</p>";
  html += "<p><strong>Current Projected Package:</strong> " + activePackageName + " (" + activeCasketCode + ")</p>";
  html += "<p><strong>QR Payload:</strong> <code>" + activeQrPayload + "</code></p>";
  html += "<p style='color:#10b981;'><strong>Status:</strong> Online &amp; Listening on /display</p>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

// Simple JSON field parser for environments without ArduinoJson library
String extractJsonField(String body, String fieldName) {
  String searchKey = "\"" + fieldName + "\"";
  int keyIndex = body.indexOf(searchKey);
  if (keyIndex == -1) return "";
  
  int colonIndex = body.indexOf(":", keyIndex + searchKey.length());
  if (colonIndex == -1) return "";
  
  int startQuote = body.indexOf("\"", colonIndex);
  if (startQuote != -1) {
    int endQuote = body.indexOf("\"", startQuote + 1);
    if (endQuote != -1) {
      return body.substring(startQuote + 1, endQuote);
    }
  } else {
    // Numeric value
    int endIndex = body.indexOf(",", colonIndex);
    if (endIndex == -1) endIndex = body.indexOf("}", colonIndex);
    if (endIndex != -1) {
      String val = body.substring(colonIndex + 1, endIndex);
      val.trim();
      return val;
    }
  }
  return "";
}

// POST /display - Receive package QR data from Smart POS
void handleDisplay() {
  if (server.method() != HTTP_POST) {
    server.send(405, "application/json", "{\"status\":\"error\",\"message\":\"Method Not Allowed. Use POST.\"}");
    return;
  }

  String body = server.arg("plain");
  if (body.length() == 0) {
    server.send(400, "application/json", "{\"status\":\"error\",\"message\":\"Empty request body.\"}");
    return;
  }

  // Parse JSON fields
  String code    = extractJsonField(body, "casket_code");
  String name    = extractJsonField(body, "name");
  String priceStr = extractJsonField(body, "base_price");
  String payload = extractJsonField(body, "qr_payload");

  if (code.length() == 0) {
    code = extractJsonField(body, "code");
  }
  if (payload.length() == 0) {
    payload = code; // Fallback to code as QR payload
  }
  if (name.length() == 0) {
    name = "Funeral Package (" + code + ")";
  }
  float price = priceStr.toFloat();

  // Update active state
  activeCasketCode  = code;
  activePackageName = name;
  activeBasePrice   = price;
  activeQrPayload   = payload;
  lastProjectedTime = millis();

  // Visual & Sound Feedback
  digitalWrite(LED_PIN, HIGH);
  beepSuccess();

  // Render on projection display
  renderProjectionDisplay(activeCasketCode, activePackageName, activeBasePrice, activeQrPayload);

  // Return success response to Flask POS
  String response = "{\"status\":\"success\",\"message\":\"QR projected on ESP32\",\"casket_code\":\"" + code + "\"}";
  server.send(200, "application/json", response);
}

// POST /clear - Clear projection
void handleClear() {
  activeCasketCode  = "NONE";
  activePackageName = "Projection Cleared";
  activeBasePrice   = 0.0;
  activeQrPayload   = "NONE";

  Serial.println("\n[ESP32 PROJECTOR] Display projection cleared.");
  server.send(200, "application/json", "{\"status\":\"success\",\"message\":\"Projection cleared.\"}");
}

// -----------------------------------------------------------------------------
// 6. Setup & Main Loop
// -----------------------------------------------------------------------------
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
  Serial.println(" RebongSancon Funeral Services - ESP32 QR Projector");
  Serial.println("=======================================================");
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    digitalWrite(LED_PIN, !digitalRead(LED_PIN)); // Blink while connecting
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(LED_PIN, HIGH);
    Serial.println("\n[SUCCESS] Wi-Fi Connected!");
    Serial.print("ESP32 Local IP Address: ");
    Serial.println(WiFi.localIP());
    Serial.println("Set this IP in your Flask server's config.json as 'ESP32_IP'.");
    beepSuccess();
  } else {
    digitalWrite(LED_PIN, LOW);
    Serial.println("\n[WARNING] Wi-Fi connection timed out. Check SSID & password.");
  }

  // Register WebServer routes
  server.on("/", HTTP_GET, handleRoot);
  server.on("/display", HTTP_POST, handleDisplay);
  server.on("/clear", HTTP_POST, handleClear);
  server.begin();

  Serial.println("\nHTTP WebServer started on port 80.");
  Serial.println("Listening for QR projection payloads at POST /display");
}

void loop() {
  server.handleClient();
  delay(5);
}
