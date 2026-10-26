import io
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import minisa_eye as eye
from PIL import Image


class Wire:
    def __init__(self, data=b'', width=3):
        self.data = bytearray(data)
        self.width = width
        self.closed = False
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def read(self, size):
        chunk = bytes(self.data[:min(size, self.width)])
        del self.data[:len(chunk)]
        return chunk

    def close(self):
        self.closed = True


def jpeg():
    out = io.BytesIO()
    Image.new('RGB', (24, 16), (90, 140, 200)).save(out, 'JPEG')
    return out.getvalue()


def frame(payload):
    return eye.MAGIC + struct.pack('<I', len(payload)) + payload


class ReceiverTests(unittest.TestCase):
    def receive(self, wire):
        return eye.FrameReader(wire).receive(time.monotonic() + 1, 1)

    def test_fragmented_frame_with_boot_logs_and_overlapping_prefix(self):
        payload = jpeg()
        self.assertEqual(self.receive(Wire(b'ERROR:DISCOVERY\r\nMMNS' + frame(payload), 1)), payload)

    def test_all_fragment_sizes_and_save_decode(self):
        payload = jpeg()
        for width in (1, 2, 3, 4, 7, 64, 4096):
            self.assertEqual(self.receive(Wire(frame(payload), width)), payload)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'minisa_eye.jpg'
            eye.save_frame(payload, output, False)
            self.assertEqual(output.read_bytes(), payload)
            with Image.open(output) as image:
                image.load()
                self.assertEqual(image.size, (24, 16))

    def test_invalid_length_resynchronizes(self):
        payload = jpeg()
        reader = eye.FrameReader(Wire(eye.MAGIC + b'\xff'*4 + frame(payload)))
        with self.assertRaisesRegex(ValueError, 'length'):
            reader.receive(time.monotonic()+1, 1)
        self.assertEqual(reader.receive(time.monotonic()+1, 1), payload)

    def test_bad_jpeg_markers_and_decode(self):
        for payload in (b'xx\xff\xd9', b'\xff\xd8xx', b'\xff\xd8garbage\xff\xd9'):
            with self.assertRaises(ValueError):
                self.receive(Wire(frame(payload)))

    def test_missing_marker_length_and_payload_have_deadlines(self):
        for data, stage in ((b'', 'marker'), (eye.MAGIC+b'\x10', 'length'),
                            (eye.MAGIC+struct.pack('<I', 10)+b'\xff\xd8', 'payload')):
            with self.assertRaisesRegex(eye.FrameTimeout, stage):
                eye.FrameReader(Wire(data)).receive(time.monotonic()+0.01, 0.01)

    def test_com_number_change_follows_serial_identity(self):
        def port(device, serial_number):
            return SimpleNamespace(device=device, vid=0x303a, pid=0x1001,
                                   serial_number=serial_number, location='1-2')
        tracker = eye.PortTracker('COM9')
        with patch.object(eye.list_ports, 'comports', side_effect=[
                [port('COM9', 'esp')], [port('COM9', 'other')],
                [port('COM9', 'other'), port('COM12', 'esp')]]):
            self.assertEqual(tracker.choose(), 'COM9')
            with self.assertRaises(eye.serial.SerialException):
                tracker.choose()
            self.assertEqual(tracker.choose(), 'COM12')

    def test_clearcommerror_reconnects_and_closes_old_handle(self):
        broken = Wire()
        broken.read = lambda size: (_ for _ in ()).throw(
            eye.serial.SerialException('ClearCommError failed'))
        good = Wire(frame(jpeg()))
        with patch.object(eye.PortTracker, 'choose', return_value='COM9'), \
             patch.object(eye, 'open_esp', side_effect=[broken, good]), \
             patch.object(eye.time, 'sleep'):
            self.assertEqual(eye.capture('COM9', 1, 1), jpeg())
        self.assertTrue(broken.closed)
        self.assertTrue(good.closed)

    def test_serial_settings_are_set_before_open(self):
        fake = SimpleNamespace()
        observed = []
        fake.open = lambda: observed.append((fake.port, fake.dtr, fake.rts))
        fake.close = lambda: None
        with patch.object(eye.serial, 'Serial', return_value=fake):
            self.assertIs(eye.open_esp('COM9'), fake)
        self.assertEqual(observed, [('COM9', True, False)])

    def test_retained_frame_requested_after_reopening(self):
        payload = jpeg()

        class Retained(Wire):
            def write(self, data):
                result = super().write(data)
                if data == eye.REQUEST:
                    self.data.extend(frame(payload))
                return result

        for _ in range(2):
            wire = Retained()
            self.assertEqual(self.receive(wire), payload)
            self.assertEqual(wire.written, eye.REQUEST)

    def test_short_request_write_is_a_reconnectable_error(self):
        wire = Wire()
        wire.write = lambda data: 2
        with self.assertRaisesRegex(eye.serial.SerialException, 'Short USB'):
            self.receive(wire)

    def test_partial_payload_reconnects_and_requests_complete_frame(self):
        payload = jpeg()
        partial = Wire(frame(payload)[:30])
        complete = Wire(frame(payload))
        with patch.object(eye.PortTracker, 'choose', return_value='COM9'), \
             patch.object(eye, 'open_esp', side_effect=[partial, complete]), \
             patch.object(eye.time, 'sleep'):
            self.assertEqual(eye.capture('COM9', 1, 0.02), payload)
        self.assertTrue(partial.closed)
        self.assertTrue(complete.closed)
        self.assertEqual(complete.written, eye.REQUEST)


if __name__ == '__main__':
    unittest.main()
