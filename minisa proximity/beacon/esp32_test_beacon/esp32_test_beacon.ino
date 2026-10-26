// ESP32-S3; Espressif Arduino 3.3.12, NimBLE-Arduino 2.5.1.
#include <Arduino.h>
#include <NimBLEDevice.h>

constexpr char BEACON_UUID[] = "c7a10001-5a1e-4b1e-9a11-4d494e495341";
NimBLEAdvertising* advertising;
bool configured = false;

void setup() {
  Serial.begin(115200); // Never wait for USB: a power bank is sufficient.
  NimBLEDevice::init("Minisa-01");
  advertising = NimBLEDevice::getAdvertising();
  NimBLEAdvertisementData payload;
  // 3 flag bytes + 18 UUID bytes + 11 name bytes = 32: omit flags.
  // Non-connectable, non-scannable advertising does not need discoverability flags.
  configured = payload.addServiceUUID(BEACON_UUID) && payload.setName("Minisa-01");
  configured = NimBLEDevice::setPower(0, NimBLETxPowerType::Advertise) && configured;
  configured = advertising->setConnectableMode(BLE_GAP_CONN_MODE_NON) && configured;
  configured = advertising->setDiscoverableMode(BLE_GAP_DISC_MODE_NON) && configured;
  advertising->enableScanResponse(false);
  advertising->setMinInterval(160); // 100 ms in 0.625 ms units.
  advertising->setMaxInterval(160);
  configured = advertising->setAdvertisementData(payload) && configured;
  if (configured) advertising->start(0); // Indefinite; all three primary channels.
}

void loop() {
  static uint32_t checkedAt = 0;
  if (millis() - checkedAt >= 1000) {
    checkedAt = millis();
    if (configured && !advertising->isAdvertising()) advertising->start(0);
    Serial.println(!configured ? "BEACON:ERROR:CONFIG" :
        advertising->isAdvertising() ? "BEACON:OK:Minisa-01:100ms:0dBm" : "BEACON:ERROR:START");
  }
  delay(10); // Yield only; no Wi-Fi, pairing, GATT server, or sleep cycle.
}
