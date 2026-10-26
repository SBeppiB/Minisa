# Camera firmware

Complete camera sketch: `minisa_eyes/minisa_eyes.ino`.

The recovered Base64 v2 and original build settings are in `recovered_v2/`; they came from Arduino's cached `her_eyes` build, not the BLE proximity v2 folder.

See `../CAMERA_REPAIR_STATUS.md` for source provenance, confirmed root causes, protocol details, hardware results, and build/run instructions.

Use `build_camera.py` to rebuild in this environment and `validate_hardware.py --reset` to verify a fresh capture plus interrupted-transfer recovery. The ESP retains one JPEG until reset; the PC requests it using `MNSA:GET\n`.

Required settings: ESP32S3 Dev Module, USB Serial/JTAG, CDC on boot, OPI PSRAM, 16 MB flash. Exact settings are in `FQBN.txt`.
