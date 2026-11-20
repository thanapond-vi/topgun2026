#include <WiFi.h>
#include "secrets.h"   // คัดลอกจาก include/secrets.example.h แล้วใส่ชื่อและรหัส Wi-Fi
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

//------------------- 1. WiFi Config --------------------------
const char* ssid = WIFI_SSID;          // ตั้งใน include/secrets.h (ไม่ขึ้น Git)
const char* password = WIFI_PASSWORD;  // ตั้งใน include/secrets.h (ไม่ขึ้น Git)

//----------- Connect MQTT ------------------
const char* mqtt_server = "192.168.1.112";
const int mqtt_port = 1883;

// Topics สำหรับ Publish, Subscribe และ Last Will (LWT) ตามคู่มือ HiveMQ
const char* publish_topic   = "esp32/sensor_data/Light_Sensor";
const char* subscribe_topic = "esp32/control/led";
const char* will_topic      = "esp32/status";

// ตั้งค่า Last Will and Testament (LWT)
const byte willQoS          = 0;
const boolean willRetain    = true;
const char* willMessage     = "offline";

WiFiClient espClient;
PubSubClient mqttClient(espClient);

//------------ 3. Board Pins (ESP32_CAN_RTOS_KUSFEN_2) --------
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

const int LDR_PIN     = 35; // LDR (IO35)
const int POT_PIN     = 32; // Potentiometer (IO32)
const int VSENSOR_PIN = 36; // Voltage Sensor (IO36)
const int ledPin      = 5; // LED5 (IO5)

unsigned long lastMsg = 0;
String lastReceivedMsg = "None";

//---------------- 4. MQTT Callback (รับข้อความขาเข้า) ----------
void callback(char* topic, byte* payload, unsigned int length) {
  Serial.print("Message arrived [");
  Serial.print(topic);
  Serial.print("] ");

  String message = "";
  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  Serial.println(message);
  lastReceivedMsg = message;

  // ตัวอย่างการสั่งงาน LED5 (IO5) ผ่าน MQTT Subscribe
  if (String(topic) == subscribe_topic) {
    if (message.equalsIgnoreCase("ON")) {
      analogWrite(ledPin, 0);
    } else if (message.equalsIgnoreCase("OFF")) {
      analogWrite(ledPin, 255);
    } else {
      // ถ้ารับค่าเป็นตัวเลข 0-255 ให้ปรับความสว่างตามตัวเลขนั้น
      int pwmVal = constrain(message.toInt(), 0, 255);
      analogWrite(ledPin, pwmVal);
    }
  }
}

//---------------- 5. WiFi Connect ----------------------------
void setup_wifi() {
  delay(10);
  Serial.print("Connecting to WiFi: ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected! IP address: ");
  Serial.println(WiFi.localIP());
}

//---------------- 6. Reconnect (พร้อม LWT + Subscribe) --------
void reconnect() {
  while (!mqttClient.connected()) {
    Serial.print("Attempting MQTT connection...");
    String clientId = "ESP32Client-" + String(random(0xffff), HEX);

    // เชื่อมต่อพร้อมตั้งค่า Last Will and Testament (LWT) ตามบทความ HiveMQ
    if (mqttClient.connect(clientId.c_str(), will_topic, willQoS, willRetain, willMessage)) {
      Serial.println("connected");

      // 1. ส่ง Retained Message แจ้งสถานะ "online" ทันทีที่เชื่อมต่อสำเร็จ
      const char* onlineMsg = "online";
      mqttClient.publish(will_topic, (const byte*)onlineMsg, strlen(onlineMsg), true);

      // 2. Subscribe หัวข้อควบคุมที่ระดับ QoS 1
      mqttClient.subscribe(subscribe_topic, 1);
      Serial.print("Subscribed to: ");
      Serial.println(subscribe_topic);

    } else {
      Serial.print("failed, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" try again in 5 seconds");
      delay(5000);
    }
  }
}

void setup() {
  Serial.begin(9600);
  pinMode(ledPin, OUTPUT);

  // เริ่มต้น I2C ที่ขา IO21 (SDA), IO22 (SCL)
  Wire.begin(21, 22);

  // เริ่มต้นจอ OLED
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("SSD1306 allocation failed");
    for (;;);
  }
  display.clearDisplay();
  display.setTextColor(WHITE);

  setup_wifi();

  // กำหนดขนาด Buffer เป็น 256 bytes (ค่าเริ่มต้นของ PubSubClient คือ 128 bytes)
  mqttClient.setBufferSize(256);

  // ตั้งค่า Server และ Callback Function ตามคู่มือ HiveMQ
  mqttClient.setServer(mqtt_server, mqtt_port);
  mqttClient.setCallback(callback);
}

void loop() {
  // รักษาการเชื่อมต่อและตรวจสอบข้อความขาเข้า
  if (!mqttClient.connected()) {
    reconnect();
  }
  mqttClient.loop();

  // ส่งข้อมูลทุกๆ 10 วินาที
  unsigned long now = millis();
  if (now - lastMsg > 10000) {
    lastMsg = now;

    // 1. อ่านค่า Analog บนบอร์ดจริง
    int ldr_raw     = analogRead(LDR_PIN);     // LDR (IO35)
    int pot_raw     = analogRead(POT_PIN);     // Potentiometer (IO32)
    int vsensor_raw = analogRead(VSENSOR_PIN); // Voltage Sensor (IO36)
    float v_in      = (vsensor_raw / 4095.0) * 3.3 * ((30.0 + 7.5) / 7.5);

    // 2. สร้าง JSON Payload
    JsonDocument doc;
    doc["photo_ldr"]  = ldr_raw;
    doc["pot_val"]    = pot_raw;
    doc["voltage_in"] = round(v_in * 100.0) / 100.0;

    char jsonBuffer[256];
    serializeJson(doc, jsonBuffer);

    // 3. Publish ข้อมูลและตรวจสอบสถานะการส่ง (rc)
    boolean rc = mqttClient.publish(publish_topic, jsonBuffer);
    if (rc) {
      Serial.print("Published to [");
      Serial.print(publish_topic);
      Serial.print("]: ");
      Serial.println(jsonBuffer);
    } else {
      Serial.println("Publish failed! Check connection or MQTT_MAX_PACKET_SIZE.");
    }

    // 4. แสดงผลบนหน้าจอ OLED
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println("MQTT: Connected");
    display.print("LDR : "); display.println(ldr_raw);
    display.print("POT : "); display.println(pot_raw);
    display.print("VIN : "); display.print(v_in, 2); display.println(" V");
    display.print("LED  : "); display.println(lastReceivedMsg);
    display.display();
  }
}