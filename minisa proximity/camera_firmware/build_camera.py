"""Build using the recovered Arduino build's exact compiler settings and core.

Uses installed tools read-only; all generated files stay under camera_firmware.
The Arduino CLI cannot initialize its global package directory in this sandbox.
"""
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
CACHE = Path.home() / 'AppData/Local/arduino/sketches/A6E81AD9D9E1533AB427F000969631D1'
PACKAGES = Path.home() / 'AppData/Local/Arduino15/packages/esp32'
SDK = PACKAGES / 'tools/esp32s3-libs/3.3.12'
ESPTOOL = PACKAGES / 'tools/esptool_py/5.3.1/esptool.exe'
BUILD = ROOT / 'build'


def run(args):
    subprocess.run([str(a) for a in args], cwd=BUILD, check=True)


def main():
    BUILD.mkdir(exist_ok=True)
    commands = json.loads((CACHE / 'compile_commands.json').read_text())
    args = commands[0]['arguments'].copy()
    compiler = Path(args[0]).with_suffix('.exe')
    args[0] = str(compiler)
    source = BUILD / 'minisa_eyes.cpp'
    source.write_text((ROOT / 'minisa_eyes/minisa_eyes.ino').read_text())
    obj = BUILD / 'minisa_eyes.cpp.o'
    args[args.index(commands[0]['file'])] = str(source)
    args[args.index('-o') + 1] = str(obj)
    run(args)
    elf = BUILD / 'minisa_eyes.elf'
    link = [compiler, f'-Wl,--Map={BUILD / "minisa_eyes.map"}']
    link += [f'-L{SDK / p}' for p in ('lib', 'ld', 'qio_opi')]
    link += ['-Wl,--wrap=esp_panic_handler', '-Wl,--wrap=esp_bt_mem_release',
             '-Wl,--wrap=esp_bt_controller_mem_release',
             f'@{SDK / "flags/ld_flags"}', f'@{SDK / "flags/ld_scripts"}',
             '-Wl,--start-group', obj]
    link += sorted((CACHE / 'libraries').rglob('*.o'))
    link += [CACHE / 'core/core.a', f'@{SDK / "flags/ld_libs"}',
             '-Wl,--end-group', '-Wl,-EL', '-o', elf]
    run(link)
    run([ESPTOOL, '--chip', 'esp32s3', 'elf2image', '--flash-mode', 'dio',
         '--flash-freq', '80m', '--flash-size', '16MB',
         '--elf-sha256-offset', '0xb0', '-o', BUILD / 'minisa_eyes.bin', elf])
    for old, new in [('her_eyes.ino.bootloader.bin', 'bootloader.bin'),
                     ('her_eyes.ino.partitions.bin', 'partitions.bin'),
                     ('boot_app0.bin', 'boot_app0.bin')]:
        shutil.copy2(CACHE / old, BUILD / new)
    print(f'Built {BUILD / "minisa_eyes.bin"}')


if __name__ == '__main__':
    main()
