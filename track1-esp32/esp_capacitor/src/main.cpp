/*
 * TESA Top Gun Rally 2026 - Track 1 : เครื่องวัดตัวเก็บประจุระดับ pF
 * วิธีที่ 1 : RC charging + LM393 comparator  (ส่งผลทาง Serial อย่างเดียว)
 * =====================================================================
 *
 * หลักการ
 *   กดปุ่ม -> LED ติด 200 ms -> ชาร์จ Cx ผ่าน R 1M -> จับเวลาจนแรงดัน Cx ถึง Vref
 *   -> LM393 เปลี่ยน OUT เป็น HIGH -> ได้เวลา t -> คำนวณ C -> พิมพ์ทาง Serial
 *
 *   V(t) = Vhigh * (1 - e^(-t/RC))   ->   C = t / ( R * ln( 1 / (1 - Vref/Vhigh) ) )
 *   ถ้า Vref = 63.2% ของ Vhigh  ->  ln(...) = 1  ->  C = t / R
 *   R = 1M : เวลา 1 us = 1 pF  (10 pF ชาร์จแค่ ~10 us -> ต้องจับเวลาแบบละเอียดมาก)
 *
 * การต่อวงจร (LM393 ใช้ comparator ตัว A : ขา 1,2,3)
 *
 *   GPIO25 ---[ R 1M ]---+---------------> LM393 ขา 3 (IN+ A)
 *                        |
 *                       [Cx]  ตัวเก็บประจุที่วัด (สายสั้นที่สุด)
 *                        |
 *                       GND
 *
 *   3.3V ---[ pot 10k ]--- GND
 *               |ขากลาง --------------> LM393 ขา 2 (IN- A)   หมุนให้ได้ ~2.086 V
 *
 *   LM393 ขา 1 (OUT A) ---+-----------> GPIO27
 *                         |
 *                      [ R 2k ]  pull-up (LM393 เป็น open-collector ต้องมี)
 *                         |
 *                       3.3V    <-- ต่อ 3.3V เท่านั้น ห้าม 5V (ESP32 ไม่ทน 5V)
 *
 *   LM393 ขา 8 (VCC) -> 5V / VIN ของบอร์ด  (ดูเหตุผลด้านล่าง)
 *   LM393 ขา 4 (GND) -> GND
 *   ขา 5,6 (comparator B ที่ไม่ใช้) -> ต่อ GND ทั้งคู่ กันสัญญาณรบกวน
 *
 *   ปุ่ม: GPIO32 --- [สวิตช์] --- GND   (ใช้ pull-up ภายใน ไม่ต้องมีตัวต้านทาน)
 *   LED : LED บนบอร์ด (GPIO2)
 *
 *   ทำไม LM393 ต้องใช้ไฟ 5V: ขา input ของ LM393 รับแรงดันได้ถึงแค่ VCC - 1.5 V
 *   ถ้าเลี้ยงด้วย 3.3V จะรับได้แค่ 1.8 V ซึ่งต่ำกว่า Vref 2.086 V -> วัดเพี้ยน
 *   เลี้ยง 5V จะรับได้ถึง 3.5 V และขา OUT ยัง pull-up ไป 3.3V ได้ปกติ
 *
 * ทำไมไม่ใช้ interrupt จับเวลา
 *   interrupt ของ ESP32 หน่วง ~2 us และแกว่งไม่แน่นอน = ผิดไป ~2 pF ต่อครั้ง
 *   เลยใช้ "วนอ่านขาตรงจาก register + นับรอบสัญญาณนาฬิกา CPU" (1 รอบ = 4.2 ns ที่ 240 MHz)
 *   ระหว่างจับเวลาปิด interrupt ไว้ชั่วคราว (ไม่เกิน 5 ms) ให้ค่าไม่แกว่ง
 */
#include <Arduino.h>
#include <algorithm>
#include "soc/soc.h"
#include "soc/gpio_reg.h"

// ============================== ตั้งค่าทีม ==============================
#define TEAM_ID  "TGR-01"   // <-- เปลี่ยนเป็นรหัสทีม
#define CAL_MODE 0          // 1 = พิมพ์ค่าดิบไว้คาลิเบรต / 0 = รูปแบบแข่งจริงเท่านั้น

// ============================== ขา ==============================
const int BUTTON_PIN = 32;  // สวิตช์เริ่มวัด
const int LED_PIN    = 2;   // LED บนบอร์ด
const int CHARGE_PIN = 25;  // จ่ายไฟชาร์จผ่าน R 1M
const int SENSE_PIN  = 27;  // รับ OUT ของ LM393 (ต้องเป็นขา 0-31 เพื่ออ่านจาก GPIO_IN_REG)

// ============================== ค่าวงจร (วัดจริงด้วยมัลติมิเตอร์แล้วแก้) ==============================
const float R_CHARGE_OHM = 1000000.0;  // วัดค่า R 1M จริง (อาจเป็น 0.98M - 1.02M)
const float V_HIGH       = 3.30;       // แรงดันที่ GPIO25 ตอน HIGH
const float V_REF        = 2.086;      // แรงดันที่ขา 2 ของ LM393 (หมุน pot)

// ============================== คาลิเบรต ==============================
// C_จริง = GAIN * (C_ดิบ - C_OPEN_PF)
// C_OPEN_PF = ค่าดิบตอนไม่เสียบตัวเก็บประจุ (ความจุแฝงของสาย/บอร์ด/LM393)
const float C_OPEN_PF = 8.14;
const float GAIN      = 0.976;

// ============================== การวัด ==============================
const int      N_SAMPLES        = 101;   // วัดซ้ำกี่ครั้งต่อการกด 1 ครั้ง
const int      TRIM             = 25;    // ตัดค่าที่ต่ำสุด/สูงสุดทิ้งข้างละกี่ค่า แล้วเฉลี่ยที่เหลือ
const uint32_t TIMEOUT_US       = 5000;  // ชาร์จนานเกินนี้ = เกินช่วงวัด (~5000 pF)
const uint32_t MIN_DISCHARGE_US = 1000;  // เวลาคายประจุขั้นต่ำก่อนวัดรอบถัดไป

// ============================== ปุ่ม / LED ==============================
const unsigned long LED_ON_MS   = 200;   // กติกา 3.4
const unsigned long DEBOUNCE_MS = 50;
const unsigned long LOCKOUT_MS  = 500;

// ============================== ตัวแปรภายใน ==============================
static portMUX_TYPE timingMux = portMUX_INITIALIZER_UNLOCKED;
const uint32_t CHARGE_MASK = 1UL << CHARGE_PIN;
const uint32_t SENSE_MASK  = 1UL << SENSE_PIN;

float    lnFactor       = 1.0;   // ln(1 / (1 - Vref/Vhigh)) คำนวณใน setup()
uint32_t cyclesPerUs    = 240;
uint32_t timeoutCycles  = 0;
uint32_t dischargeUs    = MIN_DISCHARGE_US;

int stableState = HIGH, lastReading = HIGH;
unsigned long lastChangeMs = 0, lastMeasureMs = 0;

// ============================== ปุ่ม ==============================
// คืน true ครั้งเดียวตอนปุ่มถูกกดลง (กรองการเด้งแล้ว)
bool buttonPressed() {
  int reading = digitalRead(BUTTON_PIN);
  if (reading != lastReading) {
    lastChangeMs = millis();
    lastReading = reading;
  }
  if (millis() - lastChangeMs > DEBOUNCE_MS && reading != stableState) {
    stableState = reading;
    return stableState == LOW;
  }
  return false;
}

// ============================== LED ==============================
// กระพริบให้เสร็จก่อนวัด: LED ไม่ค้างเกิน 200 ms และไม่รบกวนการวัดระดับ pF
void ledPulse() {
  digitalWrite(LED_PIN, HIGH);
  delay(LED_ON_MS);
  digitalWrite(LED_PIN, LOW);
}

// ============================== วัด 1 ครั้ง ==============================
// คืนค่าจำนวนรอบนาฬิกา CPU ที่ใช้ชาร์จจนถึง Vref
// คืน 0 ถ้าผิดปกติ (คายประจุไม่หมด / ชาร์จนานเกิน timeout)
uint32_t measureOnceCycles() {
  // 1) คายประจุ: ให้ GPIO25 เป็น LOW แล้วรอ
  REG_WRITE(GPIO_OUT_W1TC_REG, CHARGE_MASK);
  delayMicroseconds(dischargeUs);
  if (REG_READ(GPIO_IN_REG) & SENSE_MASK) {
    return 0;                     // OUT ยัง HIGH = Cx ยังไม่หมดประจุ หรือต่อสายผิด
  }

  // 2) ชาร์จ + จับเวลา (ปิด interrupt ชั่วคราว ไม่ให้อะไรมาขัดจังหวะ)
  uint32_t start, elapsed = 0;
  bool tripped = false;

  portENTER_CRITICAL(&timingMux);
  start = ESP.getCycleCount();
  REG_WRITE(GPIO_OUT_W1TS_REG, CHARGE_MASK);            // GPIO25 = HIGH เริ่มชาร์จ
  while (true) {
    if (REG_READ(GPIO_IN_REG) & SENSE_MASK) {           // LM393 OUT = HIGH -> ถึง Vref แล้ว
      elapsed = ESP.getCycleCount() - start;
      tripped = true;
      break;
    }
    if (ESP.getCycleCount() - start > timeoutCycles) break;
  }
  portEXIT_CRITICAL(&timingMux);

  // 3) เริ่มคายประจุทันที
  REG_WRITE(GPIO_OUT_W1TC_REG, CHARGE_MASK);
  return tripped ? elapsed : 0;
}

// ============================== วัดหลายครั้ง แล้วคำนวณ pF ==============================
float measureCapacitancePF() {
  static uint32_t samples[N_SAMPLES];
  int good = 0;

  for (int i = 0; i < N_SAMPLES; i++) {
    uint32_t c = measureOnceCycles();
    if (c > 0) samples[good++] = c;
  }

  if (good < N_SAMPLES / 2) {                 // พลาดเกินครึ่ง = วัดไม่ได้
#if CAL_MODE
    Serial.printf("[CAL] ERROR: วัดสำเร็จแค่ %d/%d ครั้ง (เช็กสาย / Vref / ค่าเกินช่วง)\n", good, N_SAMPLES);
#endif
    dischargeUs = MIN_DISCHARGE_US;
    return 0.0;
  }

  // เรียงค่า ตัดหัวท้ายที่แกว่งทิ้ง แล้วเฉลี่ย (trimmed mean)
  std::sort(samples, samples + good);
  int trim = (good > 2 * TRIM + 1) ? TRIM : good / 4;
  double sum = 0;
  for (int i = trim; i < good - trim; i++) sum += samples[i];
  double meanCycles = sum / (good - 2 * trim);

  double tUs   = meanCycles / cyclesPerUs;                          // เวลาชาร์จ (us)
  double rawPF = (tUs * 1e-6) / (R_CHARGE_OHM * lnFactor) * 1e12;   // C = t / (R * ln)
  double pF    = GAIN * (rawPF - C_OPEN_PF);
  if (pF < 0) pF = 0;

  // ตัวใหญ่ชาร์จนาน ต้องคายประจุนานขึ้น (~8 เท่าของเวลาชาร์จ)
  dischargeUs = max<uint32_t>(MIN_DISCHARGE_US, (uint32_t)(tUs * 8));

#if CAL_MODE
  double spreadUs = (double)(samples[good - 1 - trim] - samples[trim]) / cyclesPerUs;
  Serial.printf("[CAL] ok=%d/%d  t=%.3f us  spread=%.3f us  raw=%.3f pF\n",
                good, N_SAMPLES, tUs, spreadUs, rawPF);
#endif
  return (float)pF;
}

// ============================== ส่งผลทาง Serial (กติกา 3.5) ==============================
void printResult(float pF) {
  Serial.println(TEAM_ID);
  Serial.printf("Capacitance = %.2f pF\n", pF);
  Serial.println();               // เว้น 1 บรรทัด
}

// ============================== setup / loop ==============================
void setup() {
  Serial.begin(9600);             // กติกา 3.2

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  pinMode(CHARGE_PIN, OUTPUT);
  digitalWrite(CHARGE_PIN, LOW);  // เริ่มต้นคายประจุไว้ก่อน
  pinMode(SENSE_PIN, INPUT);      // มี pull-up 2k ภายนอกแล้ว

  lnFactor      = log(1.0 / (1.0 - V_REF / V_HIGH));
  cyclesPerUs   = getCpuFrequencyMhz();
  timeoutCycles = TIMEOUT_US * cyclesPerUs;

#if CAL_MODE
  Serial.printf("[CAL] CPU=%lu MHz  lnFactor=%.4f  (Vref/Vhigh=%.3f)\n",
                (unsigned long)cyclesPerUs, lnFactor, V_REF / V_HIGH);
#endif
}

void loop() {
  if (buttonPressed() && millis() - lastMeasureMs > LOCKOUT_MS) {
    ledPulse();                           // กติกา 3.4
    float pF = measureCapacitancePF();    // ~0.1-0.3 s
    printResult(pF);                      // กติกา 3.5
    lastMeasureMs = millis();
  }
}
