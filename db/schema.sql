CREATE TABLE checkins (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    checkin_time TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    location_id TEXT NOT NULL,
    UNIQUE (user_id, checkin_time, latitude, longitude, location_id)
);
