// ESP32-S3, NimBLE-Arduino 2.x (API verified against 2.5.1).
#include <Arduino.h>
#include <NimBLEDevice.h>

constexpr char TARGET_NAME[] = "Sbeppi's S22+";
constexpr char BEACON_UUID[] = "c7a10001-5a1e-4b1e-9a11-4d494e495341";
// No observations are emitted until the host selects a target.
enum Target { UNCONFIGURED, PHONE, BEACON };
Target target = UNCONFIGURED;
uint32_t scanStartedAt = 0;
constexpr uint32_t SCAN_INTERVAL_MS = 100;
constexpr uint32_t SCAN_WINDOW_MS = 60;
constexpr uint32_t REPORT_MS = 150;
constexpr uint32_t HEARTBEAT_MS = 1000;
constexpr uint32_t GONE_AFTER_MS = 4000;

portMUX_TYPE sampleMux = portMUX_INITIALIZER_UNLOCKED;
bool pending = false;
uint32_t seenAt = 0;
int latestRSSI = 0;
NimBLEScan* scanner;

class ScanCallbacks : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* device) override {
    portENTER_CRITICAL(&sampleMux);
    Target selected = target;
    portEXIT_CRITICAL(&sampleMux);
    bool matches = selected == PHONE
        ? device->haveName() && device->getName() == TARGET_NAME
        : selected == BEACON && device->isAdvertisingService(NimBLEUUID(BEACON_UUID));
    if (!matches) return;
    const int rssi = device->getRSSI();
    if (rssi < -127 || rssi > 20) return;
    portENTER_CRITICAL(&sampleMux);
    if (selected != target) {
      portEXIT_CRITICAL(&sampleMux);
      return;
    }
    latestRSSI = rssi;
    seenAt = millis();
    pending = true;
    portEXIT_CRITICAL(&sampleMux);
  }
} callbacks;

void readCommands() {
  static char command[40];
  static size_t length = 0;
  static bool dropping = false;
  // Bounded work so malformed USB traffic cannot starve scan reporting.
  for (int i = 0; i < 80 && Serial.available(); ++i) {
    char c = Serial.read();
    if (c == '\n') {
      command[length] = 0;
      Target next = !dropping && strcmp(command, "TARGET:PHONE") == 0 ? PHONE
          : !dropping && strcmp(command, "TARGET:BEACON") == 0 ? BEACON : UNCONFIGURED;
      if (next == UNCONFIGURED) Serial.println("TARGET:ERROR");
      else {
        if (next != target) {
          scanner->stop();
          portENTER_CRITICAL(&sampleMux);
          target = next;
          pending = false;
          seenAt = millis();
          portEXIT_CRITICAL(&sampleMux);
          scanStartedAt = millis();
          scanner->setActiveScan(next == PHONE);
          scanner->start(0);
        }
        Serial.printf("TARGET:OK:%s\n", next == PHONE ? "PHONE" : "BEACON");
      }
      length = 0;
      dropping = false;
    } else if (c != '\r' && !dropping) {
      if (length < sizeof(command) - 1) command[length++] = c;
      else dropping = true;
    }
  }
}

void setup() {
  Serial.begin(115200);
  NimBLEDevice::init("");
  scanner = NimBLEDevice::getScan();
  scanner->setScanCallbacks(&callbacks, true); // Report repeated advertisements.
  scanner->setActiveScan(true); // Name may arrive in a scan response.
  scanner->setInterval(SCAN_INTERVAL_MS);
  scanner->setWindow(SCAN_WINDOW_MS);
  scanner->setMaxResults(0); // Callback-only: no growing device list.
  // Host configuration starts the asynchronous, indefinite scan.
}

void loop() {
  static uint32_t reportAt = 0, heartbeatAt = 0;
  static uint32_t retryAt = 0;
  readCommands();
  if (target == UNCONFIGURED) { delay(10); return; }
  uint32_t now = millis();
  if (!scanner->isScanning()) {
    // No healthy heartbeat or absence evidence while the radio is stopped.
    if (now - retryAt >= 1000) {
      retryAt = now;
      if (scanner->start(0)) scanStartedAt = now;
    }
    delay(10);
    return;
  }
  bool fresh = false;
  uint32_t lastSeen;
  int rssi;
  portENTER_CRITICAL(&sampleMux);
  now = millis(); // Sample the clock under the lock, after any callback update.
  lastSeen = seenAt;
  rssi = latestRSSI;
  if (pending && now - reportAt >= REPORT_MS) {
    fresh = true;
    pending = false;
  }
  portEXIT_CRITICAL(&sampleMux);
  // Unsigned subtraction is safe across millis() wrap (~49 days).
  if (fresh && now - lastSeen < GONE_AFTER_MS) {
    Serial.printf("PRESENCE:PRESENT:%d\n", rssi);
    reportAt = now;
  }
  if (now - heartbeatAt >= HEARTBEAT_MS) {
    heartbeatAt = now;
    Serial.println("SCAN:OK");
    if (now - scanStartedAt >= GONE_AFTER_MS && now - lastSeen >= GONE_AFTER_MS)
      Serial.println("PRESENCE:GONE");
  }
  delay(10); // Yield to BLE/USB tasks; no multi-second blind pause.
}
