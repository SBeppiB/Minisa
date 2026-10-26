"""Generate a portable Windows kit from canonical v3 and verified full images.

No USB probing, no flashing, no algorithm transformation. ZIP uses an explicit
file list, excluding existing databases, credentials, caches and validation runs.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from flash_firmware import SKETCHES, verified_image

ROOT = Path(__file__).resolve().parent.parent
V3_FILES = ("presence_service.py", "validate_hardware.py", "hardware_access.py",
            "schema.sql", "config.json", "config.beacon.json", "test_presence.py",
            "test_hardening.py", "test_target.py", "test_validation.py")
TEMPLATES = ("README.md", "requirements.txt", "setup.ps1", "run.ps1", "verify_package.py")


def build_package(destination):
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}; choose a new --output directory")
    images = {role: verified_image(role, ROOT / ".build/full" / role) for role in SKETCHES}
    wheels = sorted((ROOT / ".build/windows-wheels").glob("*.whl"))
    if not any(p.name.startswith("esptool-5.3.1-") for p in wheels):
        raise FileNotFoundError("Prepare offline dependencies with beacon/prepare_windows_wheels.py first")
    destination.mkdir(parents=True)
    entries = []

    def copy(source, relative):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        entries.append(relative)

    for name in V3_FILES:
        copy(ROOT / "v3" / name, name)
    for name in TEMPLATES:
        copy(ROOT / "windows_validation" / name, name)
    for wheel in wheels:
        copy(wheel, "wheels/" + wheel.name)
    copy(ROOT / "beacon/flash_firmware.py", "flash_firmware.py")
    for role, image in images.items():
        for source, relative in ((image, f"firmware/{role}/{image.name}"),
                                 (image.parent / "manifest.json", f"firmware/{role}/manifest.json"),
                                 (SKETCHES[role], f"firmware/{role}/{role}.ino")):
            copy(source, relative)
    copy(ROOT / "v3/PHYSICAL_VALIDATION.md", "PHYSICAL_VALIDATION.md")
    provenance = {
        "canonical_project": "Minisa BLE proximity (remote server)",
        "usb_owner": "Windows PC; no USB forwarding or server serial access required",
        "build": "Arduino ESP32 3.3.12 / NimBLE-Arduino 2.5.1 / esptool 5.3.1; direct full SDK build",
        "physical_validation": "NOT VALIDATED: full linking/image checks are not physical boot/RF evidence",
        "source_files": {name: f"v3/{name}" for name in V3_FILES},
        "upstream": ["https://github.com/espressif/arduino-esp32", "https://github.com/h2zero/NimBLE-Arduino",
                     "https://github.com/espressif/esp-idf", "https://github.com/espressif/esptool"],
        "rebuild": "On canonical server: python beacon/build_firmware.py; python beacon/package_windows.py --output NEW_DIRECTORY",
    }
    (destination / "firmware/provenance.json").write_text(json.dumps(provenance, indent=2))
    entries.append("firmware/provenance.json")
    manifest = dict(format=1, created_utc=datetime.now(timezone.utc).isoformat(), files={})
    for name in sorted(entries):
        data = (destination / name).read_bytes()
        manifest["files"][name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    (destination / "package-manifest.json").write_text(json.dumps(manifest, indent=2))
    entries.append("package-manifest.json")
    archive = destination.with_suffix(".zip")
    if archive.exists():
        raise FileExistsError(f"Refusing to overwrite archive {archive}")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in sorted(entries):
            bundle.write(destination / name, f"{destination.name}/{name}")
    archive.with_suffix(".zip.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/minisa-windows-validation")
    args = parser.parse_args()
    archive = build_package(args.output)
    print(f"Directory: {args.output.resolve()}\nZIP: {archive}\nBytes: {archive.stat().st_size}")


if __name__ == "__main__":
    main()
