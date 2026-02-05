#!/usr/bin/env python3
import os, sys, json, time, socket, platform
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

try:
    import psutil
except Exception:
    print("ERROR: psutil not installed. Install: pip install psutil", file=sys.stderr)
    raise

def _now_ms(): return int(time.time() * 1000)
def _host(): return socket.gethostname()
def _bytes_to_mb(x): return x / 1024 / 1024

def _pick_bot_process():
    me = os.getpid()
    best = None
    want = (os.getenv("BOT_MATCH") or "").strip().lower()
    for p in psutil.process_iter(attrs=["pid","name","cmdline","create_time"]):
        try:
            if p.info["pid"] == me:
                continue
            cmdl = p.info.get("cmdline") or []
            cmd = " ".join(cmdl)
            low = cmd.lower()
            name = (p.info.get("name") or "").lower()
            if "python" not in name and "python" not in low:
                continue
            if want:
                if want not in low:
                    continue
            else:
                if ("main.py" not in low) and ("nixe" not in low) and ("discord" not in low):
                    continue
            if best is None or (p.info.get("create_time") or 0) > (best.info.get("create_time") or 0):
                best = p
        except Exception:
            continue
    return best

def _target_cap_mb():
    env = os.getenv("TARGET_RAM_CAP_MB")
    if env and env.isdigit():
        return int(env)
    if platform.system().lower() == "windows":
        return 4096
    return None

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json({"ok": True, "ts_ms": _now_ms()}, 200)

        if path == "/metrics.json":
            vm = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=None)
            proc = _pick_bot_process()

            rss_mb = None
            pid = None
            cmd = None
            if proc:
                try:
                    rss_mb = _bytes_to_mb(proc.memory_info().rss)
                    pid = proc.pid
                    cmd = " ".join(proc.cmdline())
                except Exception:
                    pass

            payload = {
                "ts_ms": _now_ms(),
                "host": _host(),
                "env": {
                    "platform": platform.system(),
                    "platform_release": platform.release(),
                },
                "ram": {
                    "used_mb": _bytes_to_mb(vm.total - vm.available),
                    "avail_mb": _bytes_to_mb(vm.available),
                    "total_mb": _bytes_to_mb(vm.total),
                    "percent": vm.percent,
                    "target_cap_mb": _target_cap_mb()
                },
                "cpu": {
                    "percent": cpu,
                    "cores_logical": psutil.cpu_count(logical=True),
                    "cores_physical": psutil.cpu_count(logical=False),
                },
                "bot_process": {
                    "pid": pid,
                    "rss_mb": rss_mb,
                    "cmd": cmd,
                },
                "uptime_s": int(time.time() - psutil.boot_time()),
            }
            return self._json(payload, 200)

        return self._json({"ok": False, "error": "not_found", "path": path}, 404)

    def log_message(self, *_):
        return

def main():
    port = int(os.getenv("TELEMETRY_PORT", "9800"))
    bind = os.getenv("TELEMETRY_BIND", "127.0.0.1")
    httpd = HTTPServer((bind, port), Handler)
    print(f"[telemetry] http://{bind}:{port}  /healthz  /metrics.json  cap={_target_cap_mb()}")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
