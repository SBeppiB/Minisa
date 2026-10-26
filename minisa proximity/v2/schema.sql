CREATE TABLE IF NOT EXISTS sensory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    sensor TEXT NOT NULL,
    person TEXT,
    event_type TEXT NOT NULL,
    state TEXT,
    zone TEXT,
    previous_zone TEXT,
    rssi REAL
);
CREATE INDEX IF NOT EXISTS idx_sensory_timestamp ON sensory_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_sensory_type ON sensory_events(event_type);
CREATE INDEX IF NOT EXISTS idx_sensory_identity_time
    ON sensory_events(sensor, person, timestamp);
