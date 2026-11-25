/*
 * TESA Top Gun Rally 2026 - Coffee bean moisture sensor (TGR-29)
 * Method 1: RC charge through 1M + LM393, Vref = 2.086 V (63.2 %)
 *
 * Model (works in "delta C" space so the empty-box offset is removed):
 *     dC = C_measured - C_empty
 *     M  = piecewise-linear curve through CAL_TABLE + offsetTrim
 *
 * CAL_TABLE comes from the newest calibration (run 3), measured directly:
 *     6.9 %  -> dC =  7.175 pF
 *    11.2 %  -> dC = 15.333 pF
 *    14.3 %  -> dC = 18.357 pF
 *   segment 1 (dC <= 15.333): M = 0.5271 * dC + 3.118   (also used below A)
 *   segment 2 (dC >  15.333): M = 1.0251 * dC - 4.518   (also used above B)
 *   Fits all 3 samples exactly. HC_C_EMPTY MUST stay 4.677 for this table.
 *
 * Serial commands (9600, end with Enter):
 *   e         save EMPTY box reference (RAM only in hardcoded mode)
 *   a 6.9     measure sample A, you MUST type the known moisture %
 *   b 14.3    measure sample B, you MUST type the known moisture %
 *   m         measure (same as button)
 *   t 11.2    trim: put a sample of known % in the box, shifts the whole curve
 *   p         print calibration
 *   x         clear trim / saved calibration
 *
 * WORKFLOW
 *   Practice : COMPETITION_MODE 0  -> e, a <%>, b <%>, m, t <%>
 *   Competition: COMPETITION_MODE 1 -> prints only TGR-29 + Moisture line
 *   Optional at venue: press 'e' on the empty box to re-zero drift,
 *   then 't <known %>' with a known sample to cancel any constant shift.
 */
#include <Arduino.h>
#include <Preferences.h>
#include <algorithm>
#include <math.h>
#include "soc/soc.h"
#include "soc/gpio_reg.h"

// ============================== Mode switches ==============================
#define TEAM_ID            "TGR-29"
#define CAL_MODE           0   // 1 = print raw timing details
#define COMPETITION_MODE   1   // 1 = print ONLY team id + moisture line
#define USE_HARDCODED_CAL  1   // 1 = use constants below instead of flash
#define MOIST_FMT          "Moisture Level = %.2f%%\n"   // change to match official format

// ============================== Calibration constants ==============================
const float HC_C_EMPTY = 6.220f;    // pF, empty box reading (table below is relative to this)

// ---- Calibration table: dC (pF) -> moisture (%). Keep sorted by dC ----
struct CalPt { float dC; float m; };
const CalPt CAL_TABLE[] = {
  {  7.100f,  6.9f },   // sample A
  { 15.434f, 11.2f },   // sample Mid
  { 18.437f, 14.3f },   // sample B
};
const int N_CAL = sizeof(CAL_TABLE) / sizeof(CAL_TABLE[0]);

float offsetTrim = 0.0f;            // venue trim set with 't <known %>'

// ============================== Pins ==============================
const int BUTTON_PIN = 32;
const int LED_PIN    = 2;
const int CHARGE_PIN = 25;
const int SENSE_PIN  = 27;    // must be 0-31 (read from GPIO_IN_REG)

// ============================== Circuit ==============================
const float R_CHARGE_OHM = 1000000.0;
const float V_HIGH       = 3.30;
const float V_REF        = 2.086;
const float C_OPEN_PF    = 13.90;   // from capacitor challenge
const float GAIN         = 1.0;

// ============================== Plate geometry ==============================
const float EPS0        = 8.854e-12;
const float PLATE_W_M   = 0.05;
const float PLATE_L_M   = 0.10;
const float PLATE_GAP_M = 0.01;
const float C_AIR_PF    = EPS0 * PLATE_W_M * PLATE_L_M / PLATE_GAP_M * 1e12;  // 4.43 pF

// ============================== Measurement ==============================
const int      N_SAMPLES          = 101;
const int      TRIM               = 25;
const uint32_t TIMEOUT_US         = 5000;
const uint32_t MIN_DISCHARGE_US   = 1000;
const uint32_t PROBE_DISCHARGE_US = 40000;

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
  float dA = NAN, mA = NAN;      // dC (pF) at calibration, % moisture
  float dB = NAN, mB = NAN;
} cal;

// ============================== Moisture model ==============================
// Piecewise-linear interpolation through CAL_TABLE (extends end segments outside the table)
float interpMoisture(float d) {
  int i = 0;
  if (d >= CAL_TABLE[N_CAL - 1].dC) i = N_CAL - 2;           // above last point: extend last segment
  else while (i < N_CAL - 2 && d > CAL_TABLE[i + 1].dC) i++; // find segment
  const CalPt &p = CAL_TABLE[i], &q = CAL_TABLE[i + 1];
  return p.m + (d - p.dC) * (q.m - p.m) / (q.dC - p.dC);
}

bool moisturePercent(float c, float &m) {
  if (isnan(cal.cEmpty)) return false;
  m = interpMoisture(c - cal.cEmpty) + offsetTrim;
  if (m < 0) m = 0;
  return true;
}

// ============================== Calibration storage ==============================
void loadCal() {
  prefs.begin("beancal", true);
  cal.cEmpty = prefs.getFloat("cE", NAN);
  cal.dA = prefs.getFloat("dA", NAN);  cal.mA = prefs.getFloat("mA", NAN);
  cal.dB = prefs.getFloat("dB", NAN);  cal.mB = prefs.getFloat("mB", NAN);
  prefs.end();
}
void saveCal() {
  prefs.begin("beancal", false);
  prefs.putFloat("cE", cal.cEmpty);
  prefs.putFloat("dA", cal.dA);  prefs.putFloat("mA", cal.mA);
  prefs.putFloat("dB", cal.dB);  prefs.putFloat("mB", cal.mB);
  prefs.end();
}
void printCal() {
  Serial.printf("C_air (theory) = %.2f pF\n", C_AIR_PF);
  Serial.printf("cEmpty = %.3f pF\n", cal.cEmpty);
  Serial.printf("A: dC = %.3f pF @ %.2f %%\n", cal.dA, cal.mA);
  Serial.printf("B: dC = %.3f pF @ %.2f %%\n", cal.dB, cal.mB);
  Serial.println("active curve (CAL_TABLE):");
  for (int i = 0; i < N_CAL; i++)
    Serial.printf("  dC = %.3f pF -> %.2f %%\n", CAL_TABLE[i].dC, CAL_TABLE[i].m);
  Serial.printf("offsetTrim = %.3f %%\n\n", offsetTrim);
}

// ============================== Button / LED ==============================
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
  REG_WRITE(GPIO_OUT_W1TC_REG, CHARGE_MASK);
  delayMicroseconds(dischargeUs);
  if (REG_READ(GPIO_IN_REG) & SENSE_MASK) return 0;

  uint32_t start, elapsed = 0;
  bool tripped = false;

  portENTER_CRITICAL(&timingMux);
  start = ESP.getCycleCount();
  REG_WRITE(GPIO_OUT_W1TS_REG, CHARGE_MASK);
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

// ============================== Output ==============================
void printResult(float c) {
  Serial.println(TEAM_ID);
  float m;
  bool ok = !isnan(c) && moisturePercent(c, m);

#if COMPETITION_MODE
  if (ok) Serial.printf(MOIST_FMT, m);
  else    Serial.println("Moisture = ERR");
  Serial.println();
#else
  if (isnan(c)) {
    Serial.println("Error: no reading (wiring / beans too wet = leakage)");
    Serial.println();
    return;
  }
  Serial.printf("Capacitance = %.3f pF\n", c);
  if (!isnan(cal.cEmpty)) Serial.printf("dC = %.3f pF\n", c - cal.cEmpty);
  if (ok) {
    Serial.printf(MOIST_FMT, m);
    float lo = CAL_TABLE[0].m, hi = CAL_TABLE[N_CAL - 1].m;
    if (m < lo - 1 || m > hi + 1) Serial.println("(outside calibration range)");
  } else {
    Serial.println("Moisture = N/A (do 'e' on the empty box first)");
  }
  Serial.println();
#endif
}

// ============================== Serial commands ==============================
void handleSerial() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;
  char cmd = tolower(line[0]);
  float val = line.length() > 1 ? line.substring(1).toFloat() : NAN;

  switch (cmd) {
    case 'm':
      ledPulse();
      printResult(measureCapacitancePF());
      break;
    case 'e': {
      float c = measureCapacitancePF();
      if (isnan(c)) { Serial.println("Empty cal failed\n"); break; }
      cal.cEmpty = c;
#if !USE_HARDCODED_CAL
      saveCal();
#endif
      Serial.printf("Empty saved: %.3f pF (theory air %.2f pF + leads/fringe)\n\n", c, C_AIR_PF);
      printCal();
      break;
    }
    case 'a':
    case 'b': {
      if (isnan(val)) { Serial.printf("Type the known moisture, e.g. %c 9.5\n\n", cmd); break; }
      if (isnan(cal.cEmpty)) { Serial.println("Do 'e' (empty box) first\n"); break; }
      float c = measureCapacitancePF();
      if (isnan(c)) { Serial.println("Sample cal failed\n"); break; }
      float d = c - cal.cEmpty;
      if (cmd == 'a') { cal.dA = d; cal.mA = val; } else { cal.dB = d; cal.mB = val; }
#if !USE_HARDCODED_CAL
      saveCal();
#endif
      Serial.printf("Sample %c: C = %.3f pF, dC = %.3f pF @ %.2f %%  (curve predicts %.2f %%)\n\n",
                    toupper(cmd), c, d, val, interpMoisture(d));
      break;
    }
    case 't': {   // venue trim: shift the curve so this sample reads 'val'
      if (isnan(val)) { Serial.println("Usage: t <known %>   e.g. t 11.2\n"); break; }
      if (isnan(cal.cEmpty)) { Serial.println("Do 'e' (empty box) first\n"); break; }
      float c = measureCapacitancePF();
      if (isnan(c)) { Serial.println("Trim failed\n"); break; }
      float raw = interpMoisture(c - cal.cEmpty);
      offsetTrim = val - raw;
      Serial.printf("Trim set: curve read %.2f %%, target %.2f %%, offsetTrim = %.3f %%\n\n",
                    raw, val, offsetTrim);
      break;
    }
    case 'p': printCal(); break;
    case 'x':
      cal = SensorCal();
      offsetTrim = 0.0f;
#if USE_HARDCODED_CAL
      cal.cEmpty = HC_C_EMPTY;
#else
      saveCal();
#endif
      Serial.println("Cleared (trim reset)\n");
      printCal();
      break;
    default:  Serial.println("Commands: e | a <%> | b <%> | m | t <%> | p | x\n");
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

#if USE_HARDCODED_CAL
  cal.cEmpty = HC_C_EMPTY;
#else
  loadCal();
#endif
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
