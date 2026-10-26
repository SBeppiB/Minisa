"""Receive one MNSA + little-endian length + JPEG frame over native USB."""
import argparse
import io
from pathlib import Path
import struct
import sys
import time

import serial
from serial.tools import list_ports
from PIL import Image

MAGIC = b"MNSA"
MAX_JPEG_SIZE = 512 * 1024
REQUEST = b"MNSA:GET\n"


def status(message):
    print(message, flush=True)


class FrameTimeout(TimeoutError):
    pass


def read_exact(ser, size, deadline, stage):
    data = bytearray()
    report_at = time.monotonic() + 5
    while len(data) < size:
        if time.monotonic() >= deadline:
            raise FrameTimeout(f"{stage}: received {len(data)}/{size} bytes")
        data.extend(ser.read(min(size - len(data), 4096)))
        if time.monotonic() >= report_at:
            status(f"{stage}: {len(data)}/{size} bytes")
            report_at = time.monotonic() + 5
    return bytes(data)


class FrameReader:
    def __init__(self, ser):
        self.ser = ser
        self.pending = bytearray()
        self.log = bytearray()
        self.binary_log = False
        self.discarded = 0

    def diagnostic(self, byte):
        self.discarded += 1
        if byte == 10 or len(self.log) >= 160:
            if self.log and not self.binary_log:
                status(f"ESP: {bytes(self.log)!r}")
            self.log.clear()
            self.binary_log = False
        if byte not in (10, 13):
            if not 32 <= byte <= 126 and byte != 9:
                self.binary_log = True
            self.log.append(byte)

    def find_magic(self, deadline):
        report_at = time.monotonic() + 5
        request_at = 0
        while time.monotonic() < deadline:
            if time.monotonic() >= request_at:
                if self.ser.write(REQUEST) != len(REQUEST):
                    raise serial.SerialException("Short USB capture request")
                request_at = time.monotonic() + 2
            while self.pending and not MAGIC.startswith(self.pending):
                self.diagnostic(self.pending.pop(0))
            if self.pending == MAGIC:
                self.pending.clear()
                return
            self.pending.extend(self.ser.read(1))
            if time.monotonic() >= report_at:
                status(f"Waiting for USB frame; {self.discarded} non-frame bytes; "
                       f"recent={bytes(self.log)!r}, marker prefix={bytes(self.pending)!r}")
                report_at = time.monotonic() + 5
        raise FrameTimeout(f"No complete USB marker; discarded={self.discarded}, "
                           f"recent={bytes(self.log)!r}, prefix={bytes(self.pending)!r}")

    def receive(self, overall_deadline, frame_timeout):
        self.find_magic(min(overall_deadline, time.monotonic() + frame_timeout))
        deadline = min(overall_deadline, time.monotonic() + frame_timeout)
        raw_size = read_exact(self.ser, 4, deadline, "JPEG length")
        size = struct.unpack("<I", raw_size)[0]
        if not 4 <= size <= MAX_JPEG_SIZE:
            self.pending.extend(raw_size)
            raise ValueError(f"Invalid JPEG length {size}; expected 4..{MAX_JPEG_SIZE}")
        status(f"Frame announced: {size} bytes")
        jpeg = read_exact(self.ser, size, deadline, "JPEG payload")
        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            raise ValueError(f"JPEG markers invalid: first={jpeg[:8].hex()} last={jpeg[-8:].hex()}")
        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                if image.format != "JPEG":
                    raise ValueError("Payload is not JPEG")
                image.load()
        except (OSError, SyntaxError) as exc:
            raise ValueError(f"JPEG decoding failed: {exc}") from exc
        return jpeg


class PortTracker:
    """Follow the selected USB device, never an arbitrary available COM port."""
    def __init__(self, preferred):
        self.preferred = preferred
        self.identity = None

    def choose(self):
        ports = list(list_ports.comports())
        if self.identity:
            vid, pid, serial_number, location = self.identity
            matches = [p for p in ports if (p.vid, p.pid) == (vid, pid) and
                       ((serial_number and p.serial_number == serial_number) or
                        (not serial_number and location and p.location == location))]
            if len(matches) == 1:
                self.preferred = matches[0].device
                return self.preferred
            if serial_number or location:
                raise serial.SerialException("Waiting for the previously selected USB device")
        selected = next((p for p in ports if p.device.upper() == self.preferred.upper()), None)
        if selected:
            self.identity = (selected.vid, selected.pid, selected.serial_number, selected.location)
        return self.preferred


def open_esp(port):
    # Configure before opening; no reset sequence or input-buffer purge.
    ser = serial.Serial(port=None, baudrate=115200, timeout=0.25,
                        write_timeout=1, dsrdtr=False, rtscts=False)
    ser.dtr = True  # Native USB host-ready signal.
    ser.rts = False
    ser.port = port
    try:
        ser.open()
    except BaseException:
        ser.close()
        raise
    return ser


def capture(port, timeout, frame_timeout):
    tracker = PortTracker(port)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ser = None
        try:
            selected = tracker.choose()
            status(f"Opening {selected}...")
            ser = open_esp(selected)
            status("Connected. Waiting for Minisa's eye...")
            reader = FrameReader(ser)
            while time.monotonic() < deadline:
                try:
                    return reader.receive(deadline, frame_timeout)
                except ValueError as exc:
                    status(f"Bad frame: {exc}; resynchronizing on the same connection")
        except FrameTimeout as exc:
            status(f"Timeout: {exc}. Reopening USB and requesting the retained JPEG again.")
        except (serial.SerialException, OSError) as exc:
            status(f"USB unavailable/disconnected: {type(exc).__name__}: {exc}; reconnecting")
        finally:
            if ser is not None:
                try:
                    ser.close()
                except OSError:
                    pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise FrameTimeout(f"No decodable JPEG within {timeout:g}s. "
                       "Close other serial readers and inspect the ESP diagnostics above.")


def save_frame(jpeg, output, display=True):
    output = Path(output)
    output.write_bytes(jpeg)
    with Image.open(io.BytesIO(jpeg)) as image:
        image.load()
        status(f"MINISA:EYE_FRAME_OK {image.size} {image.mode}; saved {output.resolve()}")
        if display:
            try:
                image.show()
            except OSError as exc:
                status(f"JPEG saved and decoded, but image viewer failed: {exc}")


def positive_seconds(value):
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM9")
    parser.add_argument("--timeout", type=positive_seconds, default=120,
                        help="total capture/reconnect budget in seconds (default 120)")
    parser.add_argument("--frame-timeout", type=positive_seconds, default=20)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("minisa_eye.jpg"))
    parser.add_argument("--no-display", action="store_true")
    args = parser.parse_args()
    try:
        jpeg = capture(args.port, args.timeout, args.frame_timeout)
        save_frame(jpeg, args.output, not args.no_display)
    except KeyboardInterrupt:
        status("Stopped.")
        return 130
    except (FrameTimeout, OSError) as exc:
        status(f"Capture failed: {exc}")
        return 1
    status("Capture complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
