#!/usr/bin/env python3
"""Tiny round-robin reverse proxy (stdlib only) for multi-instance testing.

Usage: python scripts/lb_proxy.py --listen 8003 --backends 8001,8002
"""
import argparse
import http.server
import threading

BACKENDS = []
_rr_lock = threading.Lock()
_rr_count = 0


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _pick(self):
        global _rr_count
        with _rr_lock:
            backend = BACKENDS[_rr_count % len(BACKENDS)]
            _rr_count += 1
            return backend

    def _proxy(self):
        import http.client
        host, port = self._pick()
        try:
            conn = http.client.HTTPConnection(host, port, timeout=300)
            body = None
            if self.headers.get("Content-Length"):
                body = self.rfile.read(int(self.headers["Content-Length"]))
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "connection")}
            conn.request(self.command, self.path,
                         body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as e:  # noqa: BLE001
            self.send_response(502)
            msg = f"lb upstream error: {e}".encode()
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy

    def log_message(self, *a):
        pass


def main():
    global BACKENDS
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=8003)
    ap.add_argument("--backends", default="8001,8002")
    args = ap.parse_args()
    BACKENDS[:] = [("127.0.0.1", int(p)) for p in args.backends.split(",")]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.listen), Handler)
    print(f"LB :{args.listen} -> {[b[1] for b in BACKENDS]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
