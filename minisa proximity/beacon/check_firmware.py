"""Compile both sketch translation units with installed ESP32-S3 headers.

Fallback when Arduino CLI cannot initialize its package directory. This checks
real compiler/API compatibility but does NOT link firmware or produce flashable
images. Use Arduino CLI/IDE for full builds and upload (see README.md).
"""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, default=Path.home() /
                        "AppData/Local/Arduino15/packages/esp32")
    parser.add_argument("--library", type=Path, default=Path.home() /
                        "OneDrive/Documents/Arduino/libraries/NimBLE-Arduino")
    args = parser.parse_args()
    sdk = args.packages / "tools/esp32s3-libs/3.3.12"
    core = args.packages / "hardware/esp32/3.3.12"
    compiler = args.packages / "tools/esp-x32/2601/bin/xtensa-esp32s3-elf-g++.exe"
    root = Path(__file__).resolve().parent.parent
    output = root / ".build"
    output.mkdir(exist_ok=True)
    flags = [str(compiler), "-x", "c++", "-c", "@" + str(sdk / "flags/cpp_flags"),
             "@" + str(sdk / "flags/defines"), "-iprefix", (sdk / "include").as_posix() + "/",
             "@" + str(sdk / "flags/includes"), "-I" + str(sdk / "qio_qspi/include"),
             "-I" + str(core / "cores/esp32"), "-I" + str(core / "variants/esp32s3"),
             "-I" + str(args.library / "src"), "-DARDUINO=10607", "-DARDUINO_ARCH_ESP32",
             "-DARDUINO_USB_MODE=1", "-DARDUINO_USB_CDC_ON_BOOT=1", "-DF_CPU=240000000L", "-Os"]
    for name, sketch in (("beacon", "beacon/esp32_test_beacon/esp32_test_beacon.ino"),
                         ("scanner", "v2/esp32_presence/esp32_presence.ino")):
        subprocess.run(flags + [str(root / sketch), "-o", str(output / (name + ".o"))], check=True)
        print(f"PASS: {sketch} compiled to object (not linked)", flush=True)


if __name__ == "__main__":
    main()
