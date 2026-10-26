"""BLE transport, qualitative proximity, and transition storage; Python 3.10+."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import time

LOG = logging.getLogger("minisa.presence")
ZONES = ("very_close", "nearby", "far", "very_far")


@dataclass
class Config:
    target: str = "phone"
    port: str = "COM10"
    baud: int = 115200
    database: str = "minisa_senses.db"
    sensor: str = "esp32-s3-ble"
    person: str = "SBeppi"
    alpha: float = 0.4
    fast_alpha: float = 0.85
    strong_change_db: float = 12
    thresholds: tuple = (-45, -60, -75)
    hysteresis_db: float = 3
    gone_confirm_seconds: float = 0.8
    link_timeout_seconds: float = 5
    reconnect_seconds: float = 2

    def __post_init__(self):
        if self.target not in ("phone", "beacon"):
            raise ValueError("target must be phone or beacon")
        numbers = [self.alpha, self.fast_alpha, self.strong_change_db,
                   self.hysteresis_db, self.gone_confirm_seconds,
                   self.link_timeout_seconds, self.reconnect_seconds, *self.thresholds]
        if not all(math.isfinite(n) for n in numbers):
            raise ValueError("Tuning values must be finite")
        if not 0 < self.alpha <= self.fast_alpha <= 1:
            raise ValueError("Require 0 < alpha <= fast_alpha <= 1")
        if len(self.thresholds) != 3 or not all(
            -127 <= n <= 20 for n in self.thresholds
        ) or not all(a > b for a, b in zip(self.thresholds, self.thresholds[1:])):
            raise ValueError("Require three descending RSSI thresholds")
        if self.hysteresis_db < 0 or any(n <= 0 for n in (
            self.strong_change_db, self.gone_confirm_seconds,
            self.link_timeout_seconds, self.reconnect_seconds, self.baud
        )):
            raise ValueError("Invalid timing, baud, or hysteresis")
        if self.link_timeout_seconds <= 1:
            raise ValueError("Link timeout must exceed the one-second heartbeat")


def parse_line(line):
    if line == b"SCAN:OK":
        return ("heartbeat", None)
    if line == b"PRESENCE:GONE":
        return ("gone", None)
    match = re.fullmatch(rb"PRESENCE:PRESENT:(-?\d{1,3})", line)
    if match:
        value = int(match[1])
        if -127 <= value <= 20:
            return ("present", value)
    return None


class LineBuffer:
    """Keep partial serial reads; discard overlong frames through newline."""
    def __init__(self):
        self.data = bytearray()
        self.dropping = False

    def feed(self, chunk):
        frames = []
        for byte in chunk:
            if byte == 10:
                if not self.dropping:
                    frames.append(bytes(self.data).rstrip(b"\r"))
                self.data.clear()
                self.dropping = False
            elif not self.dropping:
                self.data.append(byte)
                if len(self.data) > 80:
                    self.data.clear()
                    self.dropping = True
        return frames


class Proximity:
    def __init__(self, config):
        self.c = config
        self.zone = None  # Unknown until fresh evidence, including after reconnect.
        self.ema = None
        self.gone_since = None

    def observe(self, kind, rssi, now):
        previous = self.zone
        if kind == "heartbeat":
            return None
        if kind == "gone":
            if self.gone_since is None:
                self.gone_since = now
                return None
            if now - self.gone_since < self.c.gone_confirm_seconds:
                return None
            self.zone = "gone"
            self.ema = None
        else:
            self.gone_since = None
            if self.ema is None:
                self.ema = float(rssi)
            else:
                alpha = (self.c.fast_alpha if abs(rssi - self.ema) >=
                         self.c.strong_change_db else self.c.alpha)
                self.ema += alpha * (rssi - self.ema)
            if previous not in ZONES:
                index = sum(self.ema < t for t in self.c.thresholds)
            else:
                index = ZONES.index(previous)
                while index > 0 and self.ema >= self.c.thresholds[index - 1] + self.c.hysteresis_db:
                    index -= 1
                while index < 3 and self.ema < self.c.thresholds[index] - self.c.hysteresis_db:
                    index += 1
            self.zone = ZONES[index]
        if previous == self.zone:
            return None
        return dict(event_type="presence" if previous not in ZONES or self.zone == "gone"
                    else "proximity", state="gone" if self.zone == "gone" else "present",
                    zone=self.zone, previous_zone=previous, rssi=self.ema)


class EventStore:
    def __init__(self, path, config):
        self.c = config
        self.sensor_state = None
        self.db = sqlite3.connect(path, timeout=5)
        try:
            self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def save(self, event):
        if event["event_type"] == "sensor" and event.get("state") == self.sensor_state:
            return
        with self.db:
            self.db.execute(
                "INSERT INTO sensory_events "
                "(timestamp,sensor,person,event_type,state,zone,previous_zone,rssi) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), self.c.sensor, self.c.person,
                 event["event_type"], event.get("state"), event.get("zone"),
                 event.get("previous_zone"), event.get("rssi")))
        if event["event_type"] == "sensor":
            self.sensor_state = event.get("state")
        LOG.info("%s", event)

    def close(self):
        self.db.close()


def configure_target(esp, config, serial_error, clock=time.monotonic):
    """Discard all presence data until this scanner acknowledges target selection."""
    command = f"TARGET:{config.target.upper()}\n".encode("ascii")
    expected = b"TARGET:OK:" + config.target.upper().encode("ascii")
    buffer = LineBuffer()
    started = clock()
    retry_at = started
    while clock() - started < config.link_timeout_seconds:
        now = clock()
        if now >= retry_at:
            if esp.write(command) != len(command):
                raise serial_error("Short target configuration write")
            retry_at = now + 1
        for line in buffer.feed(esp.read(min(esp.in_waiting or 1, 256))):
            if line == expected:
                LOG.info("Scanner target confirmed: %s", config.target)
                return
            if line.startswith(b"TARGET:ERROR"):
                raise serial_error("Scanner rejected target configuration")
    raise serial_error("Target acknowledgement timed out; flash updated scanner firmware")


def read_session(esp, config, store, serial_error, clock=time.monotonic):
    model = Proximity(config)
    buffer = LineBuffer()
    last_message = clock()
    ready = False
    while True:
        chunk = esp.read(min(esp.in_waiting or 1, 256))
        for line in buffer.feed(chunk):
            parsed = parse_line(line)
            if parsed is None:
                continue
            last_message = clock()
            if not ready:
                store.save(dict(event_type="sensor", state="online"))
                ready = True
            event = model.observe(*parsed, last_message)
            if event:
                store.save(event)
        if clock() - last_message >= config.link_timeout_seconds:
            raise serial_error("No valid sensor frames (USB or scanner stalled)")


def run(config, db_path):
    import serial  # Pure algorithm/database tests do not require hardware or pyserial.
    store = EventStore(db_path, config)
    offline = False
    try:
        while True:
            try:
                with serial.Serial(config.port, config.baud, timeout=0.1, write_timeout=1) as esp:
                    esp.reset_input_buffer()
                    configure_target(esp, config, serial.SerialException)
                    offline = False
                    read_session(esp, config, store, serial.SerialException)
            except (serial.SerialException, OSError) as error:
                LOG.warning("Sensor unavailable: %s", error)
                if not offline:
                    store.save(dict(event_type="sensor", state="offline"))
                    offline = True
                time.sleep(config.reconnect_seconds)
    except KeyboardInterrupt:
        LOG.info("Stopping sensory service")
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    config = Config(**json.loads(args.config.read_text(encoding="utf-8")))
    db_path = Path(config.database)
    if not db_path.is_absolute():
        db_path = args.config.resolve().parent / db_path
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run(config, db_path)


if __name__ == "__main__":
    main()
