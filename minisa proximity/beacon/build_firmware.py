"""Full ESP32-S3 build using installed Arduino 3.3.12 recipes and SDK.

Fallback for Arduino CLI's directory-discovery access failure. Does not alter
the installation. Fixed settings: S3, hardware CDC, no PSRAM, QIO 80 MHz, 4 MiB.
Compiles Arduino core and NimBLE sources, links SDK archives, generates images.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SKETCHES = {"scanner": ROOT / "v2/esp32_presence/esp32_presence.ino",
            "beacon": ROOT / "beacon/esp32_test_beacon/esp32_test_beacon.ino"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, default=Path.home() / "AppData/Local/Arduino15/packages/esp32")
    parser.add_argument("--library", type=Path, default=Path.home() / "OneDrive/Documents/Arduino/libraries/NimBLE-Arduino")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 16:
        parser.error("jobs must be between 1 and 16")
    core = args.packages / "hardware/esp32/3.3.12"
    sdk = args.packages / "tools/esp32s3-libs/3.3.12"
    compiler = args.packages / "tools/esp-x32/2601/bin"
    esptool = args.packages / "tools/esptool_py/5.3.1/esptool.exe"
    if "version=2.5.1" not in (args.library / "library.properties").read_text():
        parser.error("This fallback requires NimBLE-Arduino 2.5.1")
    out = ROOT / ".build/full"
    objdir = out / "objects"
    objdir.mkdir(parents=True, exist_ok=True)
    log = out / "build.log"
    log.write_text("Direct full build from installed Arduino 3.3.12 platform recipes\n")
    for role in SKETCHES:
        (out / role).mkdir(exist_ok=True)
        (out / role / "manifest.json").write_text(json.dumps({"status": "building"}))

    def execute(command):
        result = subprocess.run([str(x) for x in command], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        # Called from compiler workers; append a complete command/result block.
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps([str(x) for x in command]) + "\n" + result.stdout + "\n")
        if result.returncode:
            raise RuntimeError(f"Command failed ({result.returncode}): {command[0]}\n{result.stdout}")
        return result.stdout

    common = ["@" + str(sdk / "flags/defines"), "-iprefix", (sdk / "include").as_posix() + "/",
              "@" + str(sdk / "flags/includes"), "-I" + str(sdk / "qio_qspi/include"),
              "-I" + str(core / "cores/esp32"), "-I" + str(core / "variants/esp32s3"),
              "-I" + str(args.library / "src"), "-DARDUINO=10607", "-DARDUINO_ARCH_ESP32",
              "-DARDUINO_ESP32S3_DEV", '-DARDUINO_BOARD="ESP32S3_DEV"',
              '-DARDUINO_VARIANT="esp32s3"', "-DARDUINO_PARTITION_default",
              "-DARDUINO_USB_MODE=1", "-DARDUINO_USB_CDC_ON_BOOT=1",
              "-DARDUINO_USB_MSC_ON_BOOT=0", "-DARDUINO_USB_DFU_ON_BOOT=0",
              "-DF_CPU=240000000L", "-DCORE_DEBUG_LEVEL=0", "-Os"]

    def compile_source(source):
        language = "cpp" if source.suffix in (".cpp", ".ino") else "S" if source.suffix == ".S" else "c"
        tool = "g++" if language == "cpp" else "gcc"
        destination = objdir / (source.stem[:30] + "_" + hashlib.sha256(str(source).encode()).hexdigest()[:12] + ".o")
        command = [compiler / f"xtensa-esp32s3-elf-{tool}.exe", "-c",
                   "@" + str(sdk / f"flags/{language}_flags"), *common]
        if source.is_relative_to(core / "cores"):
            command.append("-DARDUINO_CORE_BUILD")
        if source.suffix == ".ino":
            command += ["-x", "c++"]  # These sketches need no generated prototypes.
        execute(command + [source, "-o", destination])
        return destination

    sources = sorted(p for folder in (core / "cores/esp32", args.library / "src")
                     for p in folder.rglob("*") if p.suffix in (".c", ".cpp", ".S"))
    print(f"Compiling {len(sources)} core/NimBLE translation units", flush=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        objects = list(pool.map(compile_source, sources))
    archive = out / "core-nimble.a"
    archive.unlink(missing_ok=True)  # Generated archive only; avoid stale members.
    # ar receives a response file to stay below Windows command-line limits.
    response = out / "objects.rsp"
    response.write_text("\n".join('"' + p.as_posix() + '"' for p in objects))
    execute([compiler / "xtensa-esp32s3-elf-ar.exe", "rcs", archive, "@" + str(response)])
    for role, sketch in SKETCHES.items():
        print(f"Linking and generating {role}", flush=True)
        folder = out / role
        folder.mkdir(exist_ok=True)
        manifest_path = folder / "manifest.json"
        # Invalidate any previous successful result before rebuilding this role.
        manifest_path.write_text(json.dumps({"status": "building"}))
        sketch_obj = compile_source(sketch)
        elf = folder / f"{role}.elf"
        execute([compiler / "xtensa-esp32s3-elf-g++.exe",
                 "-Wl,--Map=" + str(folder / f"{role}.map"),
                 *["-L" + str(sdk / sub) for sub in ("lib", "ld", "qio_qspi")],
                 "-Wl,--wrap=esp_panic_handler", "-Wl,--wrap=esp_bt_mem_release",
                 "-Wl,--wrap=esp_bt_controller_mem_release",
                 "@" + str(sdk / "flags/ld_flags"), "@" + str(sdk / "flags/ld_scripts"),
                 "-Wl,--start-group", sketch_obj, archive, "@" + str(sdk / "flags/ld_libs"),
                 "-Wl,--end-group", "-Wl,-EL", "-o", elf])
        boot = folder / "bootloader.bin"
        app = folder / f"{role}.bin"
        for source, dest in ((sdk / "bin/bootloader_qio_80m.elf", boot), (elf, app)):
            execute([esptool, "--chip", "esp32s3", "elf2image", "--flash-mode", "dio",
                     "--flash-freq", "80m", "--flash-size", "4MB", "-o", dest, source])
        partition = folder / "partitions.bin"
        execute([sys.executable, core / "tools/gen_esp32part.py",
                 core / "tools/partitions/default.csv", partition])
        # Default app0 is 0x140000 bytes. Reject oversized application images.
        if app.stat().st_size > 0x140000:
            raise RuntimeError("Application exceeds the default partition")
        merged = folder / f"{role}.merged.bin"
        execute([esptool, "--chip", "esp32s3", "merge-bin", "-o", merged,
                 "--pad-to-size", "4MB", "--flash-mode", "keep", "--flash-freq", "keep",
                 "--flash-size", "keep", "0x0", boot, "0x8000", partition,
                 "0xe000", core / "tools/partitions/boot_app0.bin", "0x10000", app])
        size = execute([compiler / "xtensa-esp32s3-elf-size.exe", elf])
        print(size, flush=True)
        manifest_path.write_text(json.dumps(dict(status="built", role=role, chip="esp32s3",
            arduino="3.3.12", nimble="2.5.1", recipe="direct platform/SDK fallback",
            sketch_sha256=sha256(sketch), image=merged.name, image_sha256=sha256(merged),
            image_bytes=merged.stat().st_size, app_bytes=app.stat().st_size,
            settings="hardware CDC, PSRAM disabled, QIO 80MHz bootloader, DIO image header, 4MB"), indent=2))
        print(f"PASS full build: {merged}", flush=True)


if __name__ == "__main__":
    main()
