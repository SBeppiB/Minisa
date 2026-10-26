# Minisa camera repair — completed and tested on COM9

On 2026-10-06, recovered the original camera v2, built and flashed the binary replacement, and captured a real 640 x 480 RGB JPEG. BLE v1/v2/v3 files are unchanged (22 SHA256 checks).

## Recovered source

The original was in Arduino's cached compiler input:

`C:/Users/endle/AppData/Local/arduino/sketches/A6E81AD9D9E1533AB427F000969631D1/sketch/her_eyes.ino.cpp`

Its original sketch path was `C:/Users/endle/OneDrive/Documents/Arduino/ouuu/her_eyes/her_eyes.ino`. The current file at that path had already been overwritten with the binary rewrite; the build cache preserved Base64 v2. Matching older source was also found in Arduino language-server temporary builds. Searches covered the accessible project/OneDrive and Arduino sketch directories, Codex history, Arduino build caches, and Arduino temporary builds. The BLE `v2` directory was not used as camera source.

All ten requested strings are present in the recovered source. Its SHA256 is:

`2002daa55cdaef669a9f6a83007f1f88a71d889617c3e02a386ac900774d8668`

Preserved artifacts:

- `camera_firmware/recovered_v2/her_eyes.ino.cpp.txt`: exact cached compiler input.
- `camera_firmware/recovered_v2/her_eyes.ino.txt`: source with generated line directives/prototypes removed.
- `camera_firmware/recovered_v2/build.options.json`: original board settings.
- `camera_firmware/recovered_v2/binary_changes.diff`: complete diff against the recovered sketch.
- `camera_firmware/minisa_eyes/minisa_eyes.ino`: complete replacement camera sketch.

The external Arduino sketch files remain unchanged. The complete replacement is in this writable workspace and is already flashed on COM9.

## Confirmed causes

1. The broken minisa_eyes build used `PSRAM=disabled,FlashSize=4M`. The working camera build used `PSRAM=opi,FlashSize=16M`. COM9 reports embedded 8 MB PSRAM and 16 MB flash. A flash backup matched the cached broken minisa_eyes application byte-for-byte, tying those settings to the previously running firmware. Its explicit `psramFound()` failure returns from setup before Wi-Fi/TEG capture.
2. The binary rewrite required EOI at the very end of the assembled buffer. Actual complete camera buffers end with `FF D9 00 <JPEG length, uint32 LE>`. For example, 15,160 assembled bytes ended with `ff d9 00 33 3b 00 00`; 0x3b33 is 15,155, exactly five fewer bytes. Thus its strict EOI check rejected complete camera frames. Original v2 checked SOI only and emitted the trailing metadata in Base64.
3. The rewrite made three unchecked USB writes and then stopped forever. A missed or partial one-shot transmission could not be recovered by Python reconnecting. The local HWCDC implementation can drop queued prefixes while disconnected and can return short writes.

## Changes

Recovered GET_INFO/GET_STREAM byte arrays, Wi-Fi settings, port 10104, dynamic stream port parsing at bytes 34–35, SET_INFO contents/destination, token, MJPEG fields/offset fallback, ordered chunk assembly, and next-frame completion boundary are preserved. Text comparison verified preservation. Failed discovery now retries the same recovered handshake instead of permanently returning to a non-streaming loop.

At the output boundary, remove only the observed five-byte trailer when all checks pass: zero byte, little-endian length equals assembled length minus five, and FF D9 immediately precedes it. Other incomplete/bad endings are rejected. No JPEG marker is fabricated, and no arbitrary scan/truncation is used.

One JPEG remains in PSRAM until reset. Python sends `MNSA:GET\n` on opening/retrying and every two seconds while scanning for the marker. The ESP sends `MNSA` + uint32 LE length + raw JPEG, only after receiving that request. Writes are limited to 256-byte pieces, checked for short returns, and bounded by an eight-second transport deadline. Diagnostics are outside binary frames. A later request resends the retained JPEG, including after a failed transfer. There is no live-video loop.

Python follows the USB device identity across COM changes, bounds reads/reconnect attempts, validates SOI/EOI and actual Pillow decoding, saves `minisa_eye.jpg`, and displays it by default. Non-text remnants from interrupted transfers are suppressed in line diagnostics.

## Validation

- ESP32-S3 firmware compilation/link/image generation succeeded with recovered USB Serial/JTAG, CDC-on-boot, OPI PSRAM and 16 MB flash settings; flash upload hash verified.
- Eleven receiver tests pass: fragmented framing/save/decode, diagnostic resynchronization, malformed lengths/JPEGs, read deadlines, COM identity changes, simulated ClearCommError recovery, serial line settings, repeated host requests, short request writes, and partial-payload reconnect.
- Final real-hardware validation included a failed discovery followed by successful automatic retry:

```text
MINISA EYES v2 / binary USB
PSRAM:OK free=7845420
WIFI:OK
DISCOVERY:FAILED
DISCOVERY:RETRY
WIFI:OK
STREAM_PORT:62510
STREAM:REQUESTED
FRAME_COMPLETE id=44451 chunks=14 bytes=14400
JPEG:VALID_HEADER
JPEG:TRAILER_REMOVED bytes=5 jpeg=14395
USB:FRAME_READY
Frame announced: 14395 bytes
MINISA:EYE_FRAME_OK (640, 480) RGB
```

- Closed the host port after 64 payload bytes, reopened it, and received the identical JPEG. The first resumed transfer was truncated (14,345/14,395 bytes); the bounded timeout/re-request recovered it. A third capture also matched. This tests host close/reopen, not physical cable removal. COM-number changes and ClearCommError are covered by simulation, not a physical unplug test.
- Final captured JPEG SHA256: `1867b9756a74a9958661dab3429dcdb76562e92d2fb4c75cea685b74b199add7`.
- The JPEG was decoded and visually inspected. Logs are in `camera_firmware/build/hardware_validation.log` and `upload.log`.
- A final normal receiver run also captured the identical image. Automatic Windows viewing failed with WinError 1155 because the PNG viewer association used by Pillow is missing; capture/save/decode succeeded, and the saved JPEG can be opened from the workspace link.

## Run

```powershell
& C:/Users/endle/AppData/Local/Programs/Python/Python313/python.exe minisa_eye.py
& C:/Users/endle/AppData/Local/Programs/Python/Python313/python.exe -m unittest test_minisa_eye -v
& C:/Users/endle/AppData/Local/Programs/Python/Python313/python.exe camera_firmware/validate_hardware.py --reset
```

Normal repeated runs return the same retained image. Reset the ESP for a new camera capture; the hardware validation command does that with `--reset`.

## Build and recovery

Open `camera_firmware/minisa_eyes/minisa_eyes.ino` in Arduino IDE. Use ESP32S3 Dev Module, USB Serial/JTAG, USB CDC On Boot enabled, OPI PSRAM, 16 MB flash, QIO, 240 MHz CPU, and the default partition scheme. The exact original FQBN is saved in `camera_firmware/FQBN.txt`.

For this environment:

```powershell
& C:/Users/endle/AppData/Local/Programs/Python/Python313/python.exe camera_firmware/build_camera.py
```

The Arduino CLI could not initialize its protected global package directory. The build script instead invokes the installed ESP32 compiler and linker using the recovered compilation flags, cached matching core/Wi-Fi libraries, and bootloader/partition files. It writes only under `camera_firmware/build`. It depends on that existing Arduino cache; use the IDE with the saved board settings if the cache is cleared. It is not a clean rebuild of the Arduino core.

`camera_firmware/build/minisa_eyes.bin` is the flashed application. The first 1 MiB of the pre-repair flash is preserved as `pre_repair_first_1MiB.bin`, including the previous full application and boot/partition data. This is not a whole-chip backup. Large stub reads failed; smaller reads, with ROM fallback for the affected region, completed the backup before flashing.
