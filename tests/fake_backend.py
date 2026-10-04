"""Minimal OpenAI-compatible server for tests. Records requests, replies with queued content."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeBackend:
    def __init__(self):
        self.requests, self.replies, self.by_model = [], [], {}
        self.delay, self.inflight, self.max_inflight, self._lock = 0.0, 0, 0, threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.inflight += 1
                    outer.max_inflight = max(outer.max_inflight, outer.inflight)
                time.sleep(outer.delay)
                with outer._lock:
                    outer.inflight -= 1
                outer.requests.append((self.path, body))
                q = outer.by_model.get(body.get("model"))
                content = q.pop(0) if q else (outer.replies.pop(0) if outer.replies else "{}")
                if isinstance(content, dict):
                    status, payload = 200, {"choices": [{"message": {"content": content["content"]},
                                                         "finish_reason": content["finish_reason"]}]}
                else:
                    status, payload = 200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
                if isinstance(content, tuple):
                    status, payload = content
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, model):
        return [b for _, b in self.requests if b.get("model") == model]
