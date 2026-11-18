import argparse
import csv
import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory

from cleaning.clean import FIELDS


def shard_for(user_id):
    return hashlib.sha256(user_id.encode("utf-8")).digest()[0] % 2


def load(source, directory):
    directory.mkdir(parents=True, exist_ok=True)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    filenames = ("node-0.sqlite", "node-1.sqlite")
    with TemporaryDirectory(dir=directory) as staging, source.open(encoding="utf-8", newline="") as stream:
        paths = [Path(staging) / name for name in filenames]
        reader = csv.reader(stream, strict=True)
        if tuple(next(reader, ())) != FIELDS:
            raise ValueError("Unexpected CSV header")
        processed = 0
        with ExitStack() as stack:
            nodes = [stack.enter_context(closing(sqlite3.connect(path))) for path in paths]
            for node in nodes:
                node.executescript(schema)
            for row in reader:
                if len(row) != len(FIELDS):
                    raise ValueError(f"Malformed CSV row {reader.line_num}")
                nodes[shard_for(row[0])].execute(
                    """INSERT INTO checkins (user_id, checkin_time, latitude, longitude, location_id)
                       VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""", row
                )
                processed += 1
            counts = []
            for node in nodes:
                node.execute("CREATE INDEX idx_checkin_time ON checkins(checkin_time DESC, id DESC)")
                node.execute("CREATE INDEX idx_latitude ON checkins(latitude)")
                node.commit()
                counts.append(node.execute("SELECT COUNT(*) FROM checkins").fetchone()[0])
        for path in paths:
            path.replace(directory / path.name)
    return {"valid": processed, "duplicates": processed - sum(counts), "node_counts": counts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(load(args.source, args.directory)))
