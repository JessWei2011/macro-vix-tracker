"""Serve the local macro dashboard and its local-only persistence APIs."""
import json
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from urllib.parse import urlparse


if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DIR = Path(__file__).resolve().parent
DATA_FILE = DIR / "macro_data.json"
NOTIFY_CONFIG_FILE = DIR / "notify_config.json"
UPDATE_STATUS_FILE = DIR / "macro_update_status.json"
PORT = 8934
FILE_WRITE_LOCK = threading.Lock()


def _write_json_atomic(path, data, trailing_newline=False):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    temp_file = path.with_suffix(path.suffix + ".tmp")
    temp_file.write_text(text, encoding="utf-8")
    temp_file.replace(path)


def save_macro_data(entries):
    """將表格內聯編輯寫回本機資料檔；Git 同步由使用者手動執行。"""
    try:
        with FILE_WRITE_LOCK:
            _write_json_atomic(DATA_FILE, sorted(entries, key=lambda entry: entry["date"]))
        return None
    except OSError as error:
        return f"寫入本機 macro_data.json 失敗: {error}"


def save_notify_config(config):
    """將通知門檻寫回本機設定檔；Git 同步由使用者手動執行。"""
    try:
        with FILE_WRITE_LOCK:
            _write_json_atomic(NOTIFY_CONFIG_FILE, config, trailing_newline=True)
        return None
    except OSError as error:
        return f"寫入本機 notify_config.json 失敗: {error}"


def json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/update-status":
            try:
                status = json.loads(UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    raise ValueError("狀態內容不是物件")
            except FileNotFoundError:
                status = {
                    "state": "idle",
                    "message": "尚未執行總經資料更新。",
                    "updatedFields": [],
                    "failedFields": [],
                }
            except (OSError, ValueError) as error:
                status = {
                    "state": "failed",
                    "message": "無法讀取更新狀態，未載入舊資料。",
                    "error": str(error),
                    "updatedFields": [],
                    "failedFields": [],
                }
            json_response(self, 200, status)
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/shutdown":
            json_response(self, 200, {"ok": True})
            # 先讓回應送達瀏覽器，再結束本機 server process。
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return

        if parsed.path == "/api/save-notify-config":
            length = int(self.headers.get("Content-Length", 0))
            try:
                config = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                config = None
            if not isinstance(config, dict):
                json_response(self, 400, {"ok": False, "error": "無效的設定內容"})
                return
            error = save_notify_config(config)
            json_response(self, 200 if error is None else 500, {"ok": error is None, "error": error})
            return

        if parsed.path == "/api/save-macro-data":
            length = int(self.headers.get("Content-Length", 0))
            try:
                entries = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                entries = None
            valid_entries = (
                isinstance(entries, list)
                and all(isinstance(entry, dict) and "date" in entry for entry in entries)
            )
            if not valid_entries:
                json_response(self, 400, {"ok": False, "error": "無效的資料內容"})
                return
            error = save_macro_data(entries)
            json_response(self, 200 if error is None else 500, {"ok": error is None, "error": error})
            return

        json_response(self, 404, {"ok": False, "error": "找不到 API"})

    def log_message(self, fmt, *args):
        print("[server]", fmt % args)


if __name__ == "__main__":
    os.chdir(DIR)
    with ThreadingTCPServer(("", PORT), Handler) as httpd:
        print(f"Serving {DIR} at http://localhost:{PORT}")
        httpd.serve_forever()
