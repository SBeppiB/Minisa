"""Exercise real USB retransmission; optionally reset for a fresh camera capture."""
import argparse
import hashlib
from pathlib import Path
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import minisa_eye as eye
from build_camera import ESPTOOL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM9')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()
    if args.reset:
        subprocess.run([str(ESPTOOL), '--chip', 'esp32s3', '--port', args.port,
                        'chip-id'], check=True)
    first = eye.capture(args.port, 45, 20)
    eye.save_frame(first, Path(eye.__file__).with_name('minisa_eye.jpg'), False)
    # Request again, abandon after 64 payload bytes, then reopen the port.
    with eye.open_esp(args.port) as ser:
        reader = eye.FrameReader(ser)
        deadline = time.monotonic() + 10
        reader.find_magic(deadline)
        length = struct.unpack('<I', eye.read_exact(ser, 4, deadline, 'length'))[0]
        assert length == len(first), (length, len(first))
        assert eye.read_exact(ser, 64, deadline, 'abandoned payload') == first[:64]
    print('TEST: Closed host port after 64 JPEG bytes', flush=True)
    second = eye.capture(args.port, 30, 20)
    assert first == second, 'Retained frame changed after reopening'
    third = eye.capture(args.port, 30, 20)
    assert first == third, 'Retained frame changed on repeat capture'
    print(f'PASS: capture, interrupted read/reopen, repeat capture; '
          f'bytes={len(first)} SHA256={hashlib.sha256(first).hexdigest()}', flush=True)


if __name__ == '__main__':
    main()
