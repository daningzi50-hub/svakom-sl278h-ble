#!/usr/bin/env python3
"""SL278H toy relay server. AI writes commands, phone BLE bridge polls them."""
import json, time, os, secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Lock

SECRET = os.environ.get("BRIDGE_SECRET", "changeme")
PORT = int(os.environ.get("RELAY_PORT", "18099"))

queue = []
queue_lock = Lock()
status = {"relay_online": False, "last_poll": 0}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if self.path == "/toy-next":
            status["relay_online"] = True
            status["last_poll"] = time.time()
            with queue_lock:
                if queue:
                    cmd = queue.pop(0)
                    self._json(200, cmd)
                    return
            self._json(200, {"type": "hello"})
        elif self.path == "/toy-status":
            age = time.time() - status["last_poll"] if status["last_poll"] else 9999
            self._json(200, {"relay_online": age < 5, "last_poll_age": round(age, 1)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/toy-cmd":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            with queue_lock:
                queue.append(body)
            self._json(200, {"ok": True, "queued": len(queue)})
        else:
            self._json(404, {"error": "not found"})

print(f"Relay on :{PORT}")
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
