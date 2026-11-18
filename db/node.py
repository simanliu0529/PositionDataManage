import argparse
import sqlite3
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from backend.http_io import Handler, parse_query

COLUMNS = {
    "start": "checkin_time >= ?",
    "end": "checkin_time < ?",
    "west": "longitude >= ?",
    "east": "longitude <= ?",
    "south": "latitude >= ?",
    "north": "latitude <= ?",
    "user_id": "user_id = ?",
}


class NodeHandler(Handler):
    def get(self):
        url = urlsplit(self.path)
        with closing(sqlite3.connect(self.server.database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if url.path == "/count":
                return self.json(200, {"count": conn.execute("SELECT COUNT(*) FROM checkins").fetchone()[0]})
            if url.path != "/checkins":
                return self.json(404, {"error": "路径不存在"})
            filters, limit, offset = parse_query(url.query)
            where = " AND ".join(COLUMNS[key] for key in filters) or "1=1"
            rows = conn.execute(
                f"SELECT * FROM checkins WHERE {where} ORDER BY checkin_time DESC, id DESC LIMIT ?",
                (*filters.values(), limit + offset + 1),
            )
            self.json(200, {"items": [dict(row) for row in rows]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("port", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("请先执行 db.load")
    server = ThreadingHTTPServer((args.host, args.port), NodeHandler)
    server.database = args.database.resolve()
    server.serve_forever()
