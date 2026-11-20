"""Stream a cleaned CSV into two offline SQLite shards; never overwrite a database."""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from cleaning.clean import FIELDS, normalize

FILENAMES = ("node-0.sqlite", "node-1.sqlite")
REPORT = "import-report.json"
INSERT = """INSERT INTO checkins (user_id, checkin_time, latitude, longitude, location_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id, checkin_time, latitude, longitude, location_id) DO NOTHING"""
INDEXES = (
    "CREATE INDEX idx_checkin_time ON checkins(checkin_time DESC, id DESC)",
    "CREATE INDEX idx_user_time ON checkins(user_id, checkin_time DESC, id DESC)",
    "CREATE INDEX idx_latitude ON checkins(latitude)",
)


def shard_for(user_id):
    # Also imported by backend.app. Changing this requires rebuilding both shards.
    return hashlib.sha256(user_id.encode("utf-8")).digest()[0] % 2


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(source, directory, *, batch_size=10000, progress=None):
    source, directory = Path(source).resolve(), Path(directory).resolve()
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not source.is_file():
        raise FileNotFoundError(source)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".import.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    published = []
    started = perf_counter()
    try:
        for name in (*FILENAMES, REPORT):
            if (directory / name).exists():
                raise FileExistsError(f"Output already exists: {directory / name}; use a new directory")
        original = source.stat()
        source_hash = fingerprint(source)
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with TemporaryDirectory(dir=directory, prefix=".load-") as staging:
            paths = [Path(staging) / name for name in FILENAMES]
            processed = 0
            counts = [0, 0]
            batches = [[], []]
            with ExitStack() as stack:
                nodes = [stack.enter_context(closing(sqlite3.connect(path))) for path in paths]
                for node in nodes:
                    node.execute("PRAGMA cache_size = -32768")
                    node.executescript(schema)

                def flush():
                    for index, node in enumerate(nodes):
                        before = node.total_changes
                        node.executemany(INSERT, batches[index])
                        counts[index] += node.total_changes - before
                        node.commit()
                        batches[index].clear()

                with source.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.reader(stream, strict=True)
                    if tuple(next(reader, ())) != FIELDS:
                        raise ValueError(f"Unexpected CSV header; expected {','.join(FIELDS)}")
                    for row in reader:
                        if len(row) != len(FIELDS):
                            raise ValueError(f"Malformed CSV row at line {reader.line_num}")
                        try:
                            row = normalize(row)
                        except (ValueError, OverflowError) as error:
                            raise ValueError(f"Invalid CSV data at line {reader.line_num}: {error}") from error
                        batches[shard_for(row[0])].append(row)
                        processed += 1
                        if processed % batch_size == 0:
                            flush()
                            if progress and processed % 100000 == 0:
                                progress(processed)
                    flush()
                current = source.stat()
                if (original.st_size, original.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                    raise ValueError("Source CSV changed during import; no database published")
                summaries = []
                for index, node in enumerate(nodes):
                    if progress:
                        progress(processed, f"building indexes on node {index}")
                    for statement in INDEXES:
                        node.execute(statement)
                    node.execute("ANALYZE")
                    node.commit()
                    integrity = node.execute("PRAGMA quick_check").fetchone()[0]
                    if integrity != "ok":
                        raise RuntimeError(f"Node {index} failed integrity check: {integrity}")
                    count, first, last = node.execute(
                        "SELECT COUNT(*), MIN(checkin_time), MAX(checkin_time) FROM checkins"
                    ).fetchone()
                    if count != counts[index]:
                        raise RuntimeError(f"Node {index} count mismatch")
                    summaries.append({"node": index, "file": FILENAMES[index], "count": count,
                                      "first_checkin": first, "last_checkin": last,
                                      "quick_check": integrity})
            result = {
                "valid": processed, "duplicates": processed - sum(counts), "node_counts": counts,
                "inserted": sum(counts), "source": str(source), "source_bytes": original.st_size,
                "source_sha256": source_hash,
                "imported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "elapsed_seconds": round(perf_counter() - started, 3),
                "sharding": "sha256(user_id UTF-8).digest()[0] % 2",
                "time_format": "YYYY-MM-DDTHH:MM:SS+00:00", "nodes": summaries,
            }
            for summary, path in zip(summaries, paths):
                summary["database_bytes"] = path.stat().st_size
            report = Path(staging) / REPORT
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            # Connections are closed before publication, including on Windows.
            for path in [*paths, report]:
                target = directory / path.name
                path.rename(target)
                published.append(target)
        return result
    except BaseException:
        for path in reversed(published):
            path.unlink()
        raise
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description="Import a clean CSV into two SQLite shards")
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--batch-size", type=int, default=10000)
    args = parser.parse_args()

    def progress(rows, stage="loading"):
        print(f"{stage}: {rows:,} rows", file=sys.stderr, flush=True)

    try:
        result = load(args.source, args.directory, batch_size=args.batch_size, progress=progress)
    except (OSError, ValueError, csv.Error, sqlite3.Error, RuntimeError) as error:
        parser.exit(1, f"Import failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
