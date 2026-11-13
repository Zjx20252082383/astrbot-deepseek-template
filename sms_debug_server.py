# -*- coding: utf-8 -*-
"""独立临时抓包服务：打印并转储收到的原始 HTTP 请求，不依赖/不修改任何插件。"""
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DUMP = str(Path(__file__).resolve().parent / "sms_debug_dump.txt")


class H(BaseHTTPRequestHandler):
    def _dump(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        lines = []
        lines.append("=" * 60)
        lines.append(f"[{now}] {self.command} {self.path}")
        lines.append("--- headers ---")
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        lines.append("--- raw body (bytes=%d) ---" % len(raw))
        lines.append(repr(raw))
        try:
            lines.append("--- decoded ---")
            lines.append(raw.decode("utf-8"))
        except Exception as e:
            lines.append(f"(decode failed: {e})")
        out = "\n".join(lines) + "\n"
        print(out, flush=True)
        with open(DUMP, "a", encoding="utf-8") as f:
            f.write(out)
        body = b'{"code":0,"msg":"debug-ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _dump
    do_POST = _dump
    do_PUT = _dump

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", 6188), H)
    print("debug server on 0.0.0.0:6188, dump ->", DUMP, flush=True)
    srv.serve_forever()
