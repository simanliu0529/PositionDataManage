import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("user_id", "checkin_time", "latitude", "longitude", "location_id")


def normalize(row):
    user, time, lat, lon, location = (value.strip() for value in row)
    if not user or not location:
        raise ValueError("missing_id")
    try:
        timestamp = datetime.fromisoformat(time)
        if timestamp.utcoffset() is None or timestamp.microsecond:
            raise ValueError
        timestamp = timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        raise ValueError("invalid_time") from None
    try:
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except ValueError:
        raise ValueError("invalid_coordinates") from None
    return user, timestamp, lat or 0.0, lon or 0.0, location


def clean(source, target):
    if source.resolve() == target.resolve():
        raise ValueError("Input and output must differ")
    stats = Counter(input=0, valid=0)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with source.open(encoding="utf-8-sig") as src, temporary.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.writer(dst)
        writer.writerow(FIELDS)
        for line in src:
            stats["input"] += 1
            values = line.rstrip("\r\n").split("\t")
            if len(values) != len(FIELDS):
                stats["invalid_field_count"] += 1
                continue
            try:
                writer.writerow(normalize(values))
            except ValueError as error:
                stats[str(error)] += 1
            else:
                stats["valid"] += 1
    temporary.replace(target)
    return dict(stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(clean(args.source, args.target)))
