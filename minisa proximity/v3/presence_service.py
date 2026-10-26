"""BLE transport, qualitative proximity, and transition storage; Python 3.10+."""
import argparse
from collections import deque
from statistics import median
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
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
    window_seconds: float = 1.5
    min_observations: int = 3
    agreement: float = 0.75
    confirm_seconds: float = 1.0
    strong_confirm_seconds: float = 0.6
    strong_change_db: float = 6
    thresholds: tuple = (-45, -60, -75)
    hysteresis_db: float = 0.5
    gone_confirm_seconds: float = 0.8
    link_timeout_seconds: float = 5
    reconnect_seconds: float = 2

    def __post_init__(self):
        if self.target not in ("phone", "beacon"):
            raise ValueError("target must be phone or beacon")
        numbers = [self.window_seconds, self.agreement, self.confirm_seconds,
                   self.strong_confirm_seconds, self.strong_change_db,
                   self.hysteresis_db, self.gone_confirm_seconds,
                   self.link_timeout_seconds, self.reconnect_seconds, *self.thresholds]
        if not all(math.isfinite(n) for n in numbers):
            raise ValueError("Tuning values must be finite")
        if (type(self.min_observations) is not int or self.min_observations < 3
                or not 0.5 < self.agreement <= 1):
            raise ValueError("Require at least three observations and majority agreement")
        if not 0 < self.strong_confirm_seconds <= self.confirm_seconds < self.window_seconds:
            raise ValueError("Require 0 < strong confirmation <= confirmation < window")
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
        self.overlong_frames = 0

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
                    self.overlong_frames += 1
        return frames


class Proximity:
    """Qualitative zones from recent RSSI consensus, never physical distance.

    A median limits isolated spikes. Each crossed boundary additionally needs
    a majority of fresh raw samples spanning a minimum duration. Evidence is
    evaluated separately per boundary so movement can skip multiple zones.
    """
    def __init__(self, config):
        self.c = config
        self.zone = None
        self.filtered_rssi = None
        self.gone_since = None
        self.samples = deque()

    def _confirmed(self, boundary, direction):
        # direction +1 means approaching, -1 retreating.
        support = [(t, value) for t, value in self.samples
                   if direction * (value - boundary) >= self.c.hysteresis_db]
        if (len(support) < self.c.min_observations
                or len(support) / len(self.samples) < self.c.agreement):
            return False
        penetration = direction * (self.filtered_rssi - boundary)
        if penetration < self.c.hysteresis_db:
            return False
        strong = [(t, value) for t, value in support
                  if direction * (value - boundary) >= self.c.strong_change_db]
        # Fast confirmation requires sustained strong evidence, not just a
        # large median on the latest packet of an otherwise marginal crossing.
        if (len(strong) >= self.c.min_observations
                and len(strong) / len(self.samples) >= self.c.agreement
                and strong[-1][0] - strong[0][0] >= self.c.strong_confirm_seconds):
            return True
        return support[-1][0] - support[0][0] >= self.c.confirm_seconds

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
            self.filtered_rssi = None
            self.samples.clear()
        else:
            self.gone_since = None
            # Identical timestamps (e.g. buffered serial frames) cannot supply
            # multiple independent votes. Time confirmation also prevents bursts
            # from masquerading as sustained movement.
            if self.samples and now <= self.samples[-1][0]:
                return None
            self.samples.append((now, float(rssi)))
            while self.samples and now - self.samples[0][0] > self.c.window_seconds:
                self.samples.popleft()
            self.filtered_rssi = float(median(value for _, value in self.samples))
            if previous not in ZONES:
                index = sum(self.filtered_rssi < t for t in self.c.thresholds)
            else:
                index = ZONES.index(previous)
                if index > 0 and self._confirmed(self.c.thresholds[index - 1], 1):
                    while index > 0 and self._confirmed(self.c.thresholds[index - 1], 1):
                        index -= 1
                else:
                    while index < 3 and self._confirmed(self.c.thresholds[index], -1):
                        index += 1
            self.zone = ZONES[index]
        if previous == self.zone:
            return None
        return dict(event_type="presence" if previous not in ZONES or self.zone == "gone"
                    else "proximity", state="gone" if self.zone == "gone" else "present",
                    zone=self.zone, previous_zone=previous, rssi=self.filtered_rssi)


def observation_debug(model, previous, rssi, now, accepted):
    """Read-only receipt snapshot; never feeds back into transition decisions."""
    required = max(model.c.min_observations,
                   math.ceil(len(model.samples) * model.c.agreement))
    candidates = []
    if previous in ZONES:
        index = ZONES.index(previous)
        # Include every boundary reachable from the pre-observation zone, since
        # a single observation can confirm a multi-zone move.
        for i, boundary in enumerate(model.c.thresholds):
            direction = 1 if i < index else -1
            target = ZONES[i] if direction == 1 else ZONES[i + 1]
            support = [(t, v) for t, v in model.samples
                       if direction * (v - boundary) >= model.c.hysteresis_db]
            strong = [(t, v) for t, v in support
                      if direction * (v - boundary) >= model.c.strong_change_db]
            candidates.append(dict(
                candidate_zone=target, boundary=boundary,
                supporting_samples=len(support), required_samples=required,
                pending_seconds=now - support[0][0] if support else 0.0,
                support_span_seconds=support[-1][0] - support[0][0] if support else 0.0,
                confirm_seconds=model.c.confirm_seconds,
                sample_counted_as_evidence=bool(accepted and
                    direction * (rssi - boundary) >= model.c.hysteresis_db),
                median_gate_passed=direction * (model.filtered_rssi - boundary)
                    >= model.c.hysteresis_db,
                strong_supporting_samples=len(strong),
                strong_span_seconds=strong[-1][0] - strong[0][0] if strong else 0.0,
                strong_confirm_seconds=model.c.strong_confirm_seconds,
                strong_sample_counted_as_evidence=bool(accepted and
                    direction * (rssi - boundary) >= model.c.strong_change_db),
                confirmed=model._confirmed(boundary, direction)))
    return dict(timestamp=datetime.now(timezone.utc).isoformat(),
                monotonic_seconds=now, frame="PRESENCE:PRESENT", raw_rssi=rssi,
                rolling_median=model.filtered_rssi, previous_zone=previous,
                current_zone=model.zone, window_samples=len(model.samples),
                sample_accepted=accepted,
                sample_status=("baseline" if previous not in ZONES else "accepted")
                    if accepted else "non_increasing_timestamp",
                candidates=candidates)


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
        # Stop precisely at the acknowledgement newline. A bulk read here can
        # consume (and lose) the first observation or its partial prefix.
        for line in buffer.feed(esp.read(1)):
            if line == expected:
                LOG.info("Scanner target confirmed: %s", config.target)
                return
            if line.startswith(b"TARGET:ERROR"):
                raise serial_error("Scanner rejected target configuration")
    raise serial_error("Target acknowledgement timed out; flash updated scanner firmware")


def read_session(esp, config, store, serial_error, clock=time.monotonic, debug=False,
                 stop_event=None):
    model = Proximity(config)
    buffer = LineBuffer()
    last_message = clock()
    ready = False
    while stop_event is None or not stop_event.is_set():
        chunk = esp.read(min(esp.in_waiting or 1, 256))
        # All frames already buffered in this read have one receipt time.
        # Database/logging latency must not manufacture sustained RF evidence.
        received_at = clock() if chunk else None
        dropped_before = buffer.overlong_frames
        lines = buffer.feed(chunk)
        if debug and buffer.overlong_frames != dropped_before:
            LOG.debug("%s", json.dumps(dict(
                timestamp=datetime.now(timezone.utc).isoformat(),
                monotonic_seconds=received_at, frame="OVERLONG",
                dropped_frames=buffer.overlong_frames - dropped_before)))
        for line in lines:
            parsed = parse_line(line)
            if parsed is None:
                if debug:
                    LOG.debug("%s", json.dumps(dict(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        monotonic_seconds=received_at, frame="INVALID",
                        raw_hex=line.hex())))
                continue
            last_message = received_at
            if not ready:
                store.save(dict(event_type="sensor", state="online"))
                ready = True
            if debug:
                previous = model.zone
                accepted = not model.samples or last_message > model.samples[-1][0]
            event = model.observe(*parsed, last_message)
            if debug:
                if parsed[0] == "present":
                    LOG.debug("%s", json.dumps(observation_debug(
                        model, previous, parsed[1], last_message, accepted)))
                elif parsed[0] == "heartbeat":
                    LOG.debug("%s", json.dumps(dict(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        monotonic_seconds=last_message, frame="SCAN:OK")))
                elif parsed[0] == "gone":
                    LOG.debug("%s", json.dumps(dict(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        monotonic_seconds=last_message, frame="PRESENCE:GONE",
                        previous_zone=previous, current_zone=model.zone,
                        gone_pending_seconds=last_message - model.gone_since,
                        gone_confirm_seconds=config.gone_confirm_seconds)))
            if event:
                store.save(event)
        if clock() - last_message >= config.link_timeout_seconds:
            raise serial_error("No valid sensor frames (USB or scanner stalled)")


def run(config, db_path, debug=False, stop_event=None):
    import serial  # Pure algorithm/database tests do not require hardware or pyserial.
    store = EventStore(db_path, config)
    offline = False
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                with serial.Serial(config.port, config.baud, timeout=0.1, write_timeout=1) as esp:
                    esp.reset_input_buffer()
                    configure_target(esp, config, serial.SerialException)
                    offline = False
                    read_session(esp, config, store, serial.SerialException, debug=debug,
                                 stop_event=stop_event)
            except (serial.SerialException, OSError) as error:
                LOG.warning("Sensor unavailable: %s", error)
                if not offline:
                    store.save(dict(event_type="sensor", state="offline"))
                    offline = True
                if stop_event is None:
                    time.sleep(config.reconnect_seconds)
                else:
                    stop_event.wait(config.reconnect_seconds)
    except KeyboardInterrupt:
        LOG.info("Stopping sensory service")
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--debug", action="store_true",
                        help="Trace observations, heartbeat, absence and invalid frames")
    parser.add_argument("--port", help="Override the configured scanner COM port")
    parser.add_argument("--log-file", type=Path,
                        help="Also save logs (10 MiB per file, five backups)")
    args = parser.parse_args()
    config = Config(**json.loads(args.config.read_text(encoding="utf-8")))
    if args.port:
        config.port = args.port
    db_path = Path(config.database)
    if not db_path.is_absolute():
        db_path = args.config.resolve().parent / db_path
    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=10 * 1024 * 1024,
                                            backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(message)s", handlers=handlers)
    LOG.info("Starting target=%s port=%s database=%s config=%s",
             config.target, config.port, db_path, json.dumps(vars(config)))
    run(config, db_path, debug=args.debug)


if __name__ == "__main__":
    main()
