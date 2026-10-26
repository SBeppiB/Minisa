"""Flash ONE explicitly selected BLE board using a verified full-build image."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def project_layout(script_dir):
    """Same flasher in the canonical repo or the flat Windows transfer bundle."""
    if (script_dir / "firmware").is_dir():
        images = script_dir / "firmware"
        return images, {role: images / role / f"{role}.ino" for role in ("scanner", "beacon")}
    root = script_dir.parent
    return root / ".build/full", {
        "scanner": root / "v2/esp32_presence/esp32_presence.ino",
        "beacon": root / "beacon/esp32_test_beacon/esp32_test_beacon.ino"}


FIRMWARE_ROOT, SKETCHES = project_layout(Path(__file__).resolve().parent)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def esptool_command(override=None):
    if override is not None:
        return [str(override)]
    # Portable bundle uses the pinned package installed into this interpreter.
    # Keep the existing Arduino executable fallback for the canonical repo.
    installed = Path.home() / "AppData/Local/Arduino15/packages/esp32/tools/esptool_py/5.3.1/esptool.exe"
    if not (Path(__file__).resolve().parent / "firmware").is_dir() and installed.is_file():
        return [str(installed)]
    return [sys.executable, "-m", "esptool"]


def verified_image(role, folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("status") != "built" or manifest.get("role") != role or manifest.get("chip") != "esp32s3":
        raise ValueError("No successful full build for this board role")
    image = folder / f"{role}.merged.bin"
    if manifest.get("image") != image.name or sha256(image) != manifest.get("image_sha256"):
        raise ValueError("Firmware image hash mismatch")
    if manifest.get("sketch_sha256") != sha256(SKETCHES[role]):
        raise ValueError("Sketch changed since full build; rebuild first")
    if image.stat().st_size != 4 * 1024 * 1024:
        raise ValueError("Expected a complete 4 MiB flash image")
    return image


def main():
    from serial.tools import list_ports
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=SKETCHES)
    parser.add_argument("--port", required=True, help="Port positively identified by the operator")
    parser.add_argument("--esptool", type=Path, help="Optional executable override; bundle defaults to python -m esptool")
    args = parser.parse_args()
    image = verified_image(args.role, FIRMWARE_ROOT / args.role)
    matches = [p for p in list_ports.comports() if p.device.upper() == args.port.upper()]
    if len(matches) != 1:
        parser.error("Selected port is not currently enumerated. Run discovery again.")
    device = matches[0]
    print(f"Selected {args.role}: {device.device} | {device.description} | {device.hwid}")
    print("Confirm this is the intended BLE ESP32-S3 with >=4 MiB flash, native USB, and supported QIO flash.")
    print("Uploading replaces this board's firmware/data. Never select the camera board.")
    phrase = f"flash {args.role} {device.device}"
    if input(f"Type '{phrase}' to upload: ").strip() != phrase:
        raise SystemExit("Cancelled; nothing flashed")
    # Explicit chip selection rejects a different chip; never use --force.
    command = [*esptool_command(args.esptool), "--chip", "esp32s3", "--port", device.device,
               "--baud", "460800", "--before", "default-reset", "--after", "hard-reset",
               "write-flash", "0x0", str(image)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    print(result.stdout)
    report = dict(role=args.role, port=device.device, hwid=device.hwid,
                  image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                  returncode=result.returncode, output=result.stdout)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report["timestamp"] = stamp
    (image.parent / f"flash-{stamp}.json").write_text(json.dumps(report, indent=2))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
