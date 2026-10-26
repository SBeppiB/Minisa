#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>

const char* SSID = "BK19840-C122ADA67613-88299B";

IPAddress CAMERA(192,168,10,1);

constexpr uint16_t CMD_PORT   = 10104;
constexpr uint16_t LOCAL_PORT = 40000;
constexpr size_t MAX_JPEG     = 512 * 1024;

WiFiUDP udp;

uint8_t* jpegBuf = nullptr;
size_t jpegSize = 0;

int currentFrame = -1;
int expectedChunk = 0;
bool collecting = false;

const uint8_t GET_INFO[] = {
  '1','T','E','G',
  0x0B,0x00,0x0C,0x00,
  0x01,0x00,0x00,0x00,
  0,0,0,0,0,0,0,0
};

const uint8_t GET_STREAM[] = {
  '2','T','E','G',
  0x01,0x00,0x1C,0x00,
  0x01,0,0,0,
  0,0,0,0,
  0x01,0,0,0,
  0x08,0,0,0,
  0,0,0,0,
  0,0,0,0,
  0x01,0,0,0
};

void sendPacket(uint16_t port, const uint8_t* data, size_t len) {
  udp.beginPacket(CAMERA, port);
  udp.write(data, len);
  udp.endPacket();
}

// A completed JPEG stays in PSRAM until reset. A host can request it again.
// Never write binary data until an explicit request proves a reader is present.
bool frameReady = false;
char hostCommand[24] = {0};
size_t hostCommandSize = 0;

bool requestedFrame() {
  bool requested = false;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      hostCommand[hostCommandSize] = 0;
      if (strcmp(hostCommand, "MNSA:GET") == 0) requested = true;
      hostCommandSize = 0;
    } else if (c != '\r') {
      if (hostCommandSize < sizeof(hostCommand) - 1)
        hostCommand[hostCommandSize++] = c;
      else hostCommandSize = 0;
    }
  }
  return requested;
}

bool writeAll(const uint8_t* data, size_t length, uint32_t started) {
  size_t sent = 0;
  while (sent < length) {
    if (!Serial || millis() - started >= 8000) return false;
    size_t count = length - sent;
    if (count > 256) count = 256;
    size_t wrote = Serial.write(data + sent, count);
    if (wrote > count) return false;
    sent += wrote;
    if (!wrote) delay(1);
  }
  return true;
}

bool sendJPEGOverUSB() {
  uint32_t length = (uint32_t)jpegSize;
  const uint8_t header[8] = {'M','N','S','A',
    (uint8_t)length, (uint8_t)(length >> 8),
    (uint8_t)(length >> 16), (uint8_t)(length >> 24)};
  uint32_t started = millis();
  // No diagnostic bytes between header and payload, including failure paths.
  return writeAll(header, sizeof(header), started) &&
         writeAll(jpegBuf, jpegSize, started);
}

void finishFrame() {
  if (!collecting || jpegSize < 4) return;
  Serial.printf("FRAME_COMPLETE id=%d chunks=%d bytes=%u\n",
                currentFrame, expectedChunk, (unsigned)jpegSize);
  if (jpegBuf[0] != 0xFF || jpegBuf[1] != 0xD8) {
    Serial.println("JPEG:BAD_HEADER");
    return;
  }
  Serial.println("JPEG:VALID_HEADER");
  // Observed camera trailer: 00 followed by the JPEG length (uint32 LE).
  // Keep TEG assembly unchanged; omit this metadata only from USB JPEG output.
  if (jpegSize >= 9) {
    size_t end = jpegSize - 5;
    uint32_t declared = (uint32_t)jpegBuf[end+1] |
      ((uint32_t)jpegBuf[end+2] << 8) |
      ((uint32_t)jpegBuf[end+3] << 16) |
      ((uint32_t)jpegBuf[end+4] << 24);
    if (jpegBuf[end] == 0 && declared == end &&
        jpegBuf[end-2] == 0xFF && jpegBuf[end-1] == 0xD9) {
      jpegSize = end;
      Serial.printf("JPEG:TRAILER_REMOVED bytes=5 jpeg=%u\n", (unsigned)jpegSize);
    }
  }
  if (jpegBuf[jpegSize-2] != 0xFF || jpegBuf[jpegSize-1] != 0xD9) {
    Serial.println("JPEG:BAD_END");
    return;
  }
  frameReady = true;
  collecting = false;
  Serial.println("USB:FRAME_READY");
}

bool streamStarted = false;
void startCamera();

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(10);
  delay(1500);

  Serial.println("\nMINISA EYES v2 / binary USB");

  jpegBuf = (uint8_t*)ps_malloc(MAX_JPEG);

  if (!jpegBuf) {
    Serial.println("PSRAM:ALLOC_FAILED");
    return;
  }

  Serial.printf(
    "PSRAM:OK free=%u\n",
    (unsigned)ESP.getFreePsram()
  );

  startCamera();
}

void startCamera() {
  udp.stop(); // Retry the same recovered handshake after discovery failure.
  WiFi.mode(WIFI_STA);
  WiFi.begin(SSID);

  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(250);
  }

  Serial.println("\nWIFI:OK");

  udp.begin(LOCAL_PORT);

  sendPacket(CMD_PORT, GET_INFO, sizeof(GET_INFO));

  uint16_t streamPort = 0;
  uint32_t started = millis();

  while (millis() - started < 3000) {
    int n = udp.parsePacket();

    if (n >= 36) {
      uint8_t buf[256];
      int got = udp.read(buf, sizeof(buf));

      if (
        got >= 36 &&
        memcmp(buf, "1TEG", 4) == 0
      ) {
        streamPort =
          ((uint16_t)buf[34] << 8) |
          buf[35];

        break;
      }
    }

    delay(2);
  }

  if (!streamPort) {
    Serial.println("DISCOVERY:FAILED");
    return;
  }

  Serial.printf("STREAM_PORT:%u\n", streamPort);

  uint8_t setInfo[48] = {0};

  memcpy(setInfo, "1TEG", 4);

  setInfo[4] = 0x0D;
  setInfo[6] = 40;

  setInfo[26] = LOCAL_PORT >> 8;
  setInfo[27] = LOCAL_PORT & 0xFF;

  IPAddress ip = WiFi.localIP();

  setInfo[28] = ip[0];
  setInfo[29] = ip[1];
  setInfo[30] = ip[2];
  setInfo[31] = ip[3];

  setInfo[32] = 0x09;

  memcpy(&setInfo[33], "9e8040834b3a", 12);

  sendPacket(streamPort, setInfo, sizeof(setInfo));

  delay(100);

  sendPacket(streamPort, GET_STREAM, sizeof(GET_STREAM));

  Serial.println("STREAM:REQUESTED");
  streamStarted = true;
}

void loop() {
  if (!jpegBuf) { delay(1000); return; }
  if (!streamStarted) {
    delay(3000);
    Serial.println("DISCOVERY:RETRY");
    startCamera();
    return;
  }

  if (frameReady) {
    if (requestedFrame() && Serial) {
      if (sendJPEGOverUSB()) Serial.println("CAPTURE:DONE");
    }
    delay(1);
    return;
  }

  int n = udp.parsePacket();

  if (n <= 0) {
    delay(1);
    return;
  }

  static uint8_t packet[1600];

  if (n > (int)sizeof(packet)) {
    while (udp.available())
      udp.read();

    return;
  }

  int got = udp.read(packet, sizeof(packet));

  if (got < 24)
    return;

  if (memcmp(packet, "2TEG", 4) != 0)
    return;

  uint16_t cmd =
    packet[4] |
    ((uint16_t)packet[5] << 8);

  if (cmd != 3)
    return;

  uint16_t dataLen =
    packet[6] |
    ((uint16_t)packet[7] << 8);

  uint16_t frame =
    packet[16] |
    ((uint16_t)packet[17] << 8);

  uint16_t chunk =
    packet[18] |
    ((uint16_t)packet[19] << 8);

  uint32_t imageLen =
    packet[20] |
    ((uint32_t)packet[21] << 8) |
    ((uint32_t)packet[22] << 16) |
    ((uint32_t)packet[23] << 24);

  int offset = 8 + dataLen - imageLen;

  if (
    offset < 24 ||
    offset + imageLen > (uint32_t)got
  ) {
    if (imageLen == 0 || imageLen > (uint32_t)got)
      return;

    offset = got - imageLen;
  }

  if (frame != currentFrame) {
    if (collecting)
      finishFrame();

    if (frameReady) return; // Keep completed bytes intact for USB retries.

    currentFrame = frame;
    expectedChunk = 0;
    jpegSize = 0;
    collecting = true;
  }

  // UDP can lose/reorder packets.
  // For our first capture only accept a perfectly ordered frame.
  if (chunk != expectedChunk) {
    Serial.printf(
      "FRAME_DROP id=%u expected=%d got=%u\n",
      frame,
      expectedChunk,
      chunk
    );

    collecting = false;
    return;
  }

  if (jpegSize + imageLen > MAX_JPEG) {
    Serial.println("FRAME_DROP:TOO_LARGE");
    collecting = false;
    return;
  }

  memcpy(
    jpegBuf + jpegSize,
    packet + offset,
    imageLen
  );

  jpegSize += imageLen;
  expectedChunk++;
}
