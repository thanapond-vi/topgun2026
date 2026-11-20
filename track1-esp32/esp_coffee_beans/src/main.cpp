/*
 * TESA Top Gun Rally 2026 - Coffee bean moisture sensor
 * Cx = two copper plates 5 x 10 cm, 1 cm apart, beans in between
 * Measurement: Method 1 (RC charge through 1M + LM393, Vref = 2.086 V = 63.2%)
 *
 * Wiring (same as before):
 *   GPIO25 --[1M]--+-----------> LM393 pin 3 (IN+)
 *                  |
 *               PLATE 1  (short wire!)
 *                 beans
 *               PLATE 2 -----> GND
 *   Pot 10k (3.3V-GND) wiper -> LM393 pin 2 (IN-) = 2.086 V
 *   LM393 pin 1 (OUT) -> GPIO27, 2k pull-up to 3.3V
 *   LM393 pin 8 -> 5V, pin 4 -> GND, pins 5,6 -> GND
 *   Button GPIO32 -> GND, LED GPIO2
 *
 * Serial commands (9600, end with Enter):
 *   e        save EMPTY box reference
 *   a 9.5    save sample A, known moisture 9.5 %
 *   b 13.0   save sample B, known moisture 13.0 %
 *   m        measure (same as button)
 *   p        print calibration
 *   x        clear calibration
 */
#include <Arduino.h>
#include <Preferences.h>
#include <algorithm>
#include <math.h>
#include "soc/soc.h"
#include "soc/gpio_reg.h"

// ============================== Team ==============================
#define TEAM_ID  "TGR-29"
#define CAL_MODE 0            // 1 = print raw timing details

// ============================== Pins ==============================
const int BUTTON_PIN = 32;
const int LED_PIN    = 2;
const int CHARGE_PIN = 25;
const int SENSE_PIN  = 27;    // must be 0-31 (read from GPIO_IN_REG)

// ============================== Circuit ==============================
const float R_CHARGE_OHM = 1000000.0;
const float V_HIGH       = 3.30;
const float V_REF        = 2.086;

// Meter calibration (from the capacitor challenge)
const float C_OPEN_PF = 8.14;
const float GAIN      = 0.976;

// ============================== Plate sensor geometry ==============================
const float EPS0          = 8.854e-12;   // F/m
const float PLATE_W_M     = 0.05;        // 5 cm
const float PLATE_L_M     = 0.10;        // 10 cm
const float PLATE_GAP_M   = 0.01;        // 1 cm
const float C_AIR_PF      = EPS0 * PLATE_W_M * PLATE_L_M / PLATE_GAP_M * 1e12;  // 4.43 pF

// ============================== Measurement ==============================
const int      N_SAMPLES        = 201;
const int      TRIM             = 50;
const uint32_t TIMEOUT_US       = 5000;
const uint32_t MIN_DISCHARGE_US = 1000;
const uint32_t PROBE_DISCHARGE_US = 40000;   // 8 x timeout, worst case

// ============================== Button / LED ==============================
const unsigned long LED_ON_MS   = 200;
const unsigned long DEBOUNCE_MS = 50;
const unsigned long LOCKOUT_MS  = 500;

// ============================== Internal ==============================
static portMUX_TYPE timingMux = portMUX_INITIALIZER_UNLOCKED;
const uint32_t CHARGE_MASK = 1UL << CHARGE_PIN;
const uint32_t SENSE_MASK  = 1UL << SENSE_PIN;

float    lnFactor      = 1.0;
uint32_t cyclesPerUs   = 240;
uint32_t timeoutCycles = 0;
uint32_t dischargeUs   = MIN_DISCHARGE_US;

int stableState = HIGH, lastReading = HIGH;
unsigned long lastChangeMs = 0, lastMeasureMs = 0;

Preferences prefs;
struct SensorCal {
  float cEmpty = NAN;            // pF, empty box
  float cA = NAN, mA = NAN;      // pF, % moisture
  float cB = NAN, mB = NAN;
} cal;

// ============================== Calibration storage (survives reset) ==============================
void loadCal() {
  prefs.begin("beancal", true);
  cal.cEmpty = prefs.getFloat("cE", NAN);
  cal.cA = prefs.getFloat("cA", NAN);  cal.mA = prefs.getFloat("mA", NAN);
  cal.cB = prefs.getFloat("cB", NAN);  cal.mB = prefs.getFloat("mB", NAN);
  prefs.end();
}
void saveCal() {
  prefs.begin("beancal", false);
  prefs.putFloat("cE", cal.cEmpty);
  prefs.putFloat("cA", cal.cA);  prefs.putFloat("mA", cal.mA);
  prefs.putFloat("cB", cal.cB);  prefs.putFloat("mB", cal.mB);
  prefs.end();
}
void printCal() {
  Serial.printf("C_air (theory) = %.2f pF\n", C_AIR_PF);
  Serial.printf("Empty  : %.2f pF\n", cal.cEmpty);
  Serial.printf("Sample A: %.2f pF @ %.2f %%\n", cal.cA, cal.mA);
  Serial.printf("Sample B: %.2f pF @ %.2f %%\n", cal.cB, cal.mB);
  Serial.println();
}

// ============================== Button ==============================
bool buttonPressed() {
  int reading = digitalRead(BUTTON_PIN);
  if (reading != lastReading) { lastChangeMs = millis(); lastReading = reading; }
  if (millis() - lastChangeMs > DEBOUNCE_MS && reading != stableState) {
    stableState = reading;
    return stableState == LOW;
  }
  return false;
}

void ledPulse() {
  digitalWrite(LED_PIN, HIGH);
  delay(LED_ON_MS);
  digitalWrite(LED_PIN, LOW);
}

// ============================== One charge cycle ==============================
uint32_t measureOnceCycles() {
  REG_WRITE(GPIO_OUT_W1TC_REG, CHARGE_MASK);          // discharge
  delayMicroseconds(dischargeUs);
  if (REG_READ(GPIO_IN_REG) & SENSE_MASK) return 0;   // still above Vref

  uint32_t start, elapsed = 0;
  bool tripped = false;

  portENTER_CRITICAL(&timingMux);
  start = ESP.getCycleCount();
  REG_WRITE(GPIO_OUT_W1TS_REG, CHARGE_MASK);          // start charging
  while (true) {
    if (REG_READ(GPIO_IN_REG) & SENSE_MASK) {
      elapsed = ESP.getCycleCount() - start;
      tripped = true;
      break;
    }
    if (ESP.getCycleCount() - start > timeoutCycles) break;
  }
  portEXIT_CRITICAL(&timingMux);

  REG_WRITE(GPIO_OUT_W1TC_REG, CHARGE_MASK);
  return tripped ? elapsed : 0;
}

// ============================== Capacitance (pF), NAN on error ==============================
float measureCapacitancePF() {
  // Probe once with a long discharge so dischargeUs matches THIS load
  dischargeUs = PROBE_DISCHARGE_US;
  uint32_t probe = measureOnceCycles();
  dischargeUs = probe ? max<uint32_t>(MIN_DISCHARGE_US, (probe / cyclesPerUs) * 8)
                      : MIN_DISCHARGE_US;

  static uint32_t samples[N_SAMPLES];
  int good = 0;
  for (int i = 0; i < N_SAMPLES; i++) {
    uint32_t c = measureOnceCycles();
    if (c > 0) samples[good++] = c;
  }
  if (good < N_SAMPLES / 2) {
#if CAL_MODE
    Serial.printf("[CAL] ERROR: only %d/%d valid\n", good, N_SAMPLES);
#endif
    return NAN;
  }

  std::sort(samples, samples + good);
  int trim = (good > 2 * TRIM + 1) ? TRIM : good / 4;
  double sum = 0;
  for (int i = trim; i < good - trim; i++) sum += samples[i];
  double meanCycles = sum / (good - 2 * trim);

  double tUs   = meanCycles / cyclesPerUs;
  double rawPF = (tUs * 1e-6) / (R_CHARGE_OHM * lnFactor) * 1e12;
  double pF    = GAIN * (rawPF - C_OPEN_PF);

#if CAL_MODE
  double spreadUs = (double)(samples[good - 1 - trim] - samples[trim]) / cyclesPerUs;
  Serial.printf("[CAL] ok=%d/%d t=%.3f us spread=%.3f us raw=%.3f pF\n",
                good, N_SAMPLES, tUs, spreadUs, rawPF);
#endif
  return (float)pF;
}

// ============================== pF -> moisture ==============================
bool moisturePercent(float c, float &m) {
  if (isnan(cal.cA) || isnan(cal.cB) || fabsf(cal.cB - cal.cA) < 0.05f) return false;
  m = cal.mA + (c - cal.cA) * (cal.mB - cal.mA) / (cal.cB - cal.cA);
  return true;
}

// ============================== Output ==============================
void printResult(float c) {
  Serial.println(TEAM_ID);
  if (isnan(c)) {
    Serial.println("Error: no reading (wiring / beans too wet = leakage)");
    return;
  }

  float m;
  if (moisturePercent(c, m)) {
    Serial.printf("%.1f %%\n", m);
  } else {
    Serial.println("Moisture = N/A (calibrate with 'a' and 'b')");
  }
}

// ============================== Serial commands ==============================
void handleSerial() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.isEmpty()) return;
  char cmd = tolower(line[0]);
  float val = line.length() > 1 ? line.substring(1).toFloat() : NAN;

  switch (cmd) {
    case 'm': {
      ledPulse();
      printResult(measureCapacitancePF());
      break;
    }
    case 'e': {
      float c = measureCapacitancePF();
      if (isnan(c)) { Serial.println("Empty cal failed\n"); break; }
      cal.cEmpty = c; saveCal();
      Serial.printf("Empty saved: %.2f pF (theory %.2f pF + leads/fringe)\n\n", c, C_AIR_PF);
      break;
    }
    case 'a':
    case 'b': {
      if (isnan(val)) { Serial.println("Usage: a 9.5  (known moisture %)\n"); break; }
      float c = measureCapacitancePF();
      if (isnan(c)) { Serial.println("Sample cal failed\n"); break; }
      if (cmd == 'a') { cal.cA = c; cal.mA = val; } else { cal.cB = c; cal.mB = val; }
      saveCal();
      Serial.printf("Sample %c saved: %.2f pF @ %.2f %%\n\n", toupper(cmd), c, val);
      break;
    }
    case 'p': printCal(); break;
    case 'x': cal = SensorCal(); saveCal(); Serial.println("Calibration cleared\n"); break;
    default:  Serial.println("Commands: e | a <%> | b <%> | m | p | x\n");
  }
}

// ============================== setup / loop ==============================
void setup() {
  Serial.begin(9600);
  Serial.setTimeout(50);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);       digitalWrite(LED_PIN, LOW);
  pinMode(CHARGE_PIN, OUTPUT);    digitalWrite(CHARGE_PIN, LOW);
  pinMode(SENSE_PIN, INPUT);      // external 2k pull-up

  lnFactor      = log(1.0 / (1.0 - V_REF / V_HIGH));
  cyclesPerUs   = getCpuFrequencyMhz();
  timeoutCycles = TIMEOUT_US * cyclesPerUs;

  loadCal();
#if CAL_MODE
  Serial.printf("[CAL] CPU=%lu MHz lnFactor=%.4f\n", (unsigned long)cyclesPerUs, lnFactor);
  printCal();
#endif
}

void loop() {
  handleSerial();
  if (buttonPressed() && millis() - lastMeasureMs > LOCKOUT_MS) {
    ledPulse();
    printResult(measureCapacitancePF());
    lastMeasureMs = millis();
  }
}