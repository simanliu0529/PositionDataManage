"""Check shard integrity and compare the existing HTTP API with direct SQL results."""

import argparse
import json
import sqlite3
import sys
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import urlopen

from db.load import FILENAMES, REPORT, shard_for

PREDICATES = {
    "user_id": "user_id = ?", "start": "checkin_time >= ?", "end": "checkin_time < ?",
    "west": "longitude >= ?", "east": "longitude <= ?",
    "south": "latitude >= ?", "north": "latitude <= ?",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fetch(base, path):
    with urlopen(base.rstrip("/") + path, timeout=30) as response:
        return json.load(response)


def expected_page(nodes, filters, limit, offset):
    indexes = [shard_for(filters["user_id"])] if "user_id" in filters else [0, 1]
    where = " AND ".join(PREDICATES[key] for key in filters) or "1=1"
    rows = []
    for index in indexes:
        found = nodes[index].execute(
            f"SELECT * FROM checkins WHERE {where} ORDER BY checkin_time DESC, id DESC LIMIT ?",
            (*filters.values(), offset + limit + 1),
        )
        rows.extend({**dict(row), "node": index} for row in found)
    rows.sort(key=lambda row: (row["checkin_time"], row["id"], row["node"]), reverse=True)
    return {"items": rows[offset:offset + limit], "queried_nodes": indexes,
            "has_more": len(rows) > offset + limit and offset + limit <= 10000}


def verify(directory, *, api_url=None, full_integrity=False):
    directory = Path(directory).resolve()
    require(not (directory / ".import.lock").exists(), "Import is still running")
    manifest = json.loads((directory / REPORT).read_text(encoding="utf-8"))
    started = perf_counter()
    summaries = []
    api_checks = []
    with ExitStack() as stack:
        nodes = []
        users = []
        for index, name in enumerate(FILENAMES):
            conn = stack.enter_context(closing(sqlite3.connect((directory / name).as_uri() + "?mode=ro", uri=True)))
            conn.row_factory = sqlite3.Row
            nodes.append(conn)
            print(f"Verifying node {index}", file=sys.stderr, flush=True)
            check = "integrity_check" if full_integrity else "quick_check"
            integrity = [row[0] for row in conn.execute(f"PRAGMA {check}")]
            require(integrity == ["ok"], f"Node {index}: {integrity}")
            count, first, last = conn.execute(
                "SELECT COUNT(*), MIN(checkin_time), MAX(checkin_time) FROM checkins"
            ).fetchone()
            require(count == manifest["node_counts"][index], f"Node {index} count differs from import report")
            user_count = 0
            first_user = None
            for row in conn.execute("SELECT DISTINCT user_id FROM checkins ORDER BY user_id"):
                user_count += 1
                first_user = first_user if first_user is not None else row[0]
                require(shard_for(row[0]) == index, f"User {row[0]} is in the wrong shard")
            users.append(first_user)
            indexes = [row[1] for row in conn.execute("PRAGMA index_list(checkins)")]
            require({"idx_checkin_time", "idx_user_time", "idx_latitude"}.issubset(indexes), "Missing index")
            plan = [row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM checkins WHERE user_id = ? "
                "ORDER BY checkin_time DESC, id DESC LIMIT 50", (first_user or "0",))]
            require(any("idx_user_time" in line for line in plan), "User query does not use idx_user_time")
            summaries.append({"node": index, "count": count, "users": user_count,
                              "first_checkin": first, "last_checkin": last,
                              check: "ok", "indexes": indexes, "user_query_plan": plan})
        total = sum(node["count"] for node in summaries)
        require(total == manifest["inserted"], "Inserted count mismatch")
        require(manifest["valid"] == manifest["duplicates"] + total, "Input/output counts do not balance")
        if api_url:
            counts = fetch(api_url, "/api/nodes")
            require(counts == {"nodes": [{"node": n["node"], "count": n["count"]} for n in summaries]},
                    "API node counts differ from SQL")
            api_checks.append({"case": "node_counts", "passed": True})
            cases = [("global_page", {}, 50, 0), ("global_offset", {}, 25, 25)]
            for index, user in enumerate(users):
                if user is not None:
                    cases.append((f"user_node_{index}", {"user_id": user}, 50, 0))
            newest = expected_page(nodes, {}, 1, 0)["items"]
            if newest:
                row = newest[0]
                moment = datetime.fromisoformat(row["checkin_time"])
                cases.append(("utc_time_range", {"start": row["checkin_time"],
                              "end": (moment + timedelta(seconds=1)).isoformat()}, 50, 0))
                cases.append(("bounding_box", {
                    "west": max(-180, row["longitude"] - 0.01),
                    "east": min(180, row["longitude"] + 0.01),
                    "south": max(-90, row["latitude"] - 0.01),
                    "north": min(90, row["latitude"] + 0.01),
                }, 50, 0))
            cases.append(("empty_result", {"start": "9998-01-01T00:00:00+00:00"}, 50, 0))
            for label, filters, limit, offset in cases:
                expected = expected_page(nodes, filters, limit, offset)
                query = urlencode({**filters, "limit": limit, "offset": offset})
                t0 = perf_counter()
                actual = fetch(api_url, "/api/checkins?" + query)
                elapsed = round((perf_counter() - t0) * 1000, 2)
                require(actual == expected, f"API result mismatch: {label}")
                api_checks.append({"case": label, "passed": True, "rows": len(actual["items"]),
                                   "queried_nodes": actual["queried_nodes"], "elapsed_ms": elapsed})
    return {"passed": True, "inserted": total, "users": sum(n["users"] for n in summaries),
            "nodes": summaries, "api_checks": api_checks,
            "elapsed_seconds": round(perf_counter() - started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--api-url")
    parser.add_argument("--full-integrity", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.directory, api_url=args.api_url, full_integrity=args.full_integrity)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        parser.exit(1, f"Verification failed: {error}\n")
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(content, encoding="utf-8")
    print(content, end="")


if __name__ == "__main__":
    main()
