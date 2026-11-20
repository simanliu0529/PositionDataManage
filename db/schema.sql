CREATE TABLE checkins (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
    checkin_time TEXT NOT NULL CHECK (
        length(checkin_time) = 25 AND substr(checkin_time, 20) = '+00:00'
    ),
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    location_id TEXT NOT NULL CHECK (length(trim(location_id)) > 0),
    UNIQUE (user_id, checkin_time, latitude, longitude, location_id)
);
