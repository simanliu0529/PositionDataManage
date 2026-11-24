"""Test-only adapter for the upstream legacy API regression suite. No executable entry point."""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from backend.http_io import Handler, parse_query
from db.load import shard_for

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def request_node(base, path):
    with urlopen(base + path, timeout=8) as response:
        return json.load(response)


class AppHandler(Handler):
    def get(self):
        url = urlsplit(self.path)
        if url.path in STATIC:
            file, content_type = STATIC[url.path]
            return self.send_bytes(200, content_type, (FRONTEND / file).read_bytes())
        nodes = self.server.nodes
        if url.path == "/api/nodes":
            counts = self.fetch(nodes, "/count")
            return self.json(200, {"nodes": [
                {"node": index, "count": result["count"]} for index, result in enumerate(counts)
            ]})
        if url.path != "/api/checkins":
            return self.json(404, {"error": "路径不存在"})
        filters, limit, offset = parse_query(url.query)
        indexes = [shard_for(filters["user_id"])] if "user_id" in filters else list(range(len(nodes)))
        data = self.fetch([nodes[index] for index in indexes], "/checkins?" + url.query)
        rows = [
            {**row, "node": index}
            for index, result in zip(indexes, data)
            for row in result["items"]
        ]
        rows.sort(key=lambda row: (row["checkin_time"], row["id"], row["node"]), reverse=True)
        end = offset + limit
        self.json(200, {
            "items": rows[offset:end], "queried_nodes": indexes,
            "has_more": len(rows) > end and end <= 10000,
        })

    def fetch(self, nodes, path):
        with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
            return list(pool.map(lambda node: request_node(node, path), nodes))


