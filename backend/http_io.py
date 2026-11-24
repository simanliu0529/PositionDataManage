import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

from cleaning.clean import normalize


def parse_query(query):
    values = parse_qs(query, keep_blank_values=True)
    allowed = {"start", "end", "west", "east", "south", "north", "user_id", "limit", "offset"}
    if any(key not in allowed or len(parts) != 1 for key, parts in values.items()):
        raise ValueError("查询参数未知或重复")
    filters = {key: parts[0] for key, parts in values.items() if parts[0] != ""}
    try:
        limit = int(filters.pop("limit", 50))
        offset = int(filters.pop("offset", 0))
        for key in ("start", "end"):
            if key in filters:
                filters[key] = normalize(("0", filters[key], "0", "0", "0"))[1]
        for key, bound in (("west", 180), ("east", 180), ("south", 90), ("north", 90)):
            if key in filters:
                filters[key] = float(filters[key])
                if not -bound <= filters[key] <= bound:
                    raise ValueError
        for lower, upper in (("west", "east"), ("south", "north"), ("start", "end")):
            if lower in filters and upper in filters and filters[lower] >= filters[upper]:
                raise ValueError
        if not (1 <= limit <= 200 and 0 <= offset <= 10000):
            raise ValueError
    except ValueError:
        raise ValueError("请检查经纬度、带时区的时间范围和分页参数") from None
    return filters, limit, offset


class Handler(BaseHTTPRequestHandler):
    def json(self, status, data):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode())

    def send_bytes(self, status, content_type, body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A timed out / cancelled client cannot receive a second error reply.
            self.close_connection = True

    def do_GET(self):
        try:
            self.get()
        except ValueError as error:
            self.json(400, {"error": str(error)})
        except (OSError, TimeoutError, RuntimeError):
            self.json(503, {"error": "数据节点暂不可用，请检查节点状态"})
