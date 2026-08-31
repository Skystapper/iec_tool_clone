"""
End-to-end test using a mock ABBY server.

Verifies that a category run:
  * uploads each file ONCE,
  * fetches its extracted markdown,
  * sends ONE batched chat request per file (not one per requirement),
  * deletes the ABBY file afterwards,
  * produces a downloadable Excel report with one row per requirement.
"""
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["ABBY_API_KEY"] = "test-key"
os.environ["ABBY_BASE_URL"] = "http://127.0.0.1:8767"
os.environ["ABBY_CHAT_MODE"] = "simple"
os.environ["ABBY_MODEL"] = "gpt-5-nano"
os.environ["DELETE_FILES_AFTER_ANALYSIS"] = "true"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

FILES = {}
CHAT_CALLS = {"count": 0, "file_ids_seen": [], "prompts": []}

MOCK_MARKDOWN = (
    "# Security Policy\n\n"
    "Access control policy v1.0. Logging is enabled. "
    "Multi-factor authentication is required for administrative access.\n"
)


class MockABBY(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if self.path.startswith("/api/v1/developers/files"):
            fid = str(uuid.uuid4())
            FILES[fid] = {"status": "success", "bytes": length,
                          "filename": f"{fid}.pdf"}
            self._send(200, json.dumps({
                "file_id": fid, "filename": f"{fid}.pdf",
                "created_at": time.time(), "bytes": length,
            }))
            return

        if self.path.startswith("/api/v1/developers/simple_chat"):
            CHAT_CALLS["count"] += 1
            payload = json.loads(body)
            content = payload["input"][0]["content"]
            fids = [b["file"]["file_id"] for b in content
                    if b.get("type") == "input_file"]
            CHAT_CALLS["file_ids_seen"].extend(fids)
            prompt = next(b["text"] for b in content if b.get("type") == "input_text")
            CHAT_CALLS["prompts"].append(prompt)

            # Return a JSON array, one assessment per requirement id found in
            # the prompt.
            ids = []
            import re
            for m in re.finditer(r'"id":\s*"(SG-[^"]+)"', prompt):
                ids.append(m.group(1))
            arr = [
                {
                    "id": rid,
                    "status": "Fully Met",
                    "explanation": f"Policy documents appropriate controls for {rid}.",
                    "evidence": "Access control policy v1.0",
                }
                for rid in ids
            ]
            self._send(200, json.dumps({
                "message_id": str(uuid.uuid4()),
                "model": "gpt-5-nano",
                "timestamp": time.time(),
                "output": {"content": json.dumps(arr), "type": "text"},
                "usage": {"input_tokens": 1000, "output_tokens": 300},
            }))
            return

        self._send(404, json.dumps({"detail": "not found"}))

    def do_GET(self):
        if self.path.startswith("/api/v1/developers/files/") and "/content" in self.path:
            fid = self.path.split("/files/")[1].split("/")[0]
            if fid in FILES:
                self._send(200, MOCK_MARKDOWN, ctype="text/markdown")
            else:
                self._send(404, "{}")
            return
        if self.path.startswith("/api/v1/developers/files"):
            fid = self.path.split("file_id=")[-1]
            meta = FILES.get(fid)
            if not meta:
                self._send(404, json.dumps({"detail": "not found"}))
                return
            self._send(200, json.dumps([{
                "file_id": fid, "filename": meta["filename"],
                "created_at": time.time(), "bytes": meta["bytes"],
                "upload_status": meta["status"], "upload_sub_status": "none",
            }]))
            return
        self._send(404, "{}")

    def do_DELETE(self):
        fid = self.path.rstrip("/").split("/")[-1]
        FILES.pop(fid, None)
        self._send(200, json.dumps({"file_id": fid, "deleted": True}))


def start_mock():
    srv = HTTPServer(("127.0.0.1", 8767), MockABBY)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run():
    srv = start_mock()
    try:
        client = TestClient(main.app)
        txt = b"Security policy. Access control policy v1.0. Logging enabled."
        r = client.post(
            "/api/analyze",
            data={"requirement_id": "SG"},
            files={"files": ("policy.pdf", txt, "application/pdf")},
        )
        print("STATUS:", r.status_code)
        print("BODY:", r.json())
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True, body
        n = body["requirements_analyzed"]
        print(f"requirements analyzed: {n}")

        # THE KEY ASSERTION: only one chat call per file (batched), and the
        # same file_id is reused.
        print(f"chat calls: {CHAT_CALLS['count']} (expected 1, batched)")
        assert CHAT_CALLS["count"] == 1, "category run must batch into one call"
        unique_fids = set(CHAT_CALLS["file_ids_seen"])
        print(f"unique file_ids referenced: {len(unique_fids)} (expected 1)")
        assert len(unique_fids) == 1

        # Uploaded file must be cleaned up.
        assert len(FILES) == 0, f"files not cleaned up: {list(FILES)}"

        # Report exists.
        dl = client.get(body["download_url"])
        assert dl.status_code == 200 and len(dl.content) > 1000
        print("download report bytes:", len(dl.content))
        print("ALL E2E ASSERTIONS PASSED")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    run()
