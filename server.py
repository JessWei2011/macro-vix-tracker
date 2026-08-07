"""
Local server for the macro tracker page.

Two jobs:
1. Serve the static files (macro-tracker-offline.html, macro_data.json, ...)
   so the page's fetch() calls work -- opening the html directly via file://
   gets fetch() blocked by the browser's CORS policy.
2. Expose POST /api/analyze/gemini so the page's "AI 總經分析" button can
   trigger a macro analysis without ever putting an API key in browser-side
   JS. The key is read from ai_keys_local.py (gitignored, filled in by hand
   -- never committed, never printed).
"""
import json
import os
import sys
from datetime import datetime
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from urllib.parse import urlparse

import requests

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DIR = Path(__file__).resolve().parent
DATA_FILE = DIR / "macro_data.json"
ANALYSIS_FILE = DIR / "macro_analysis.json"
PORT = 8934

sys.path.insert(0, str(DIR))
try:
    from ai_keys_local import GEMINI_API_KEY
except ImportError:
    GEMINI_API_KEY = ""

FIELD_LABELS = {
    "vixtwn": "VIXTWN(台股期貨波動率指數)",
    "vix": "VIX(美股恐慌指數)",
    "oil": "布蘭特原油",
    "us10y": "10年美債殖利率",
    "spread": "Spread(美國高收益債利差)",
}


def build_prompt():
    entries = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    recent = entries[-10:]
    table = "\n".join(
        f"{e['date']}: " + ", ".join(f"{FIELD_LABELS[k]}={e.get(k)}" for k in FIELD_LABELS)
        for e in recent
    )
    return f"""你是一位總經與跨資產分析師。以下是最近幾天的五項市場指標數據：

{table}

請針對最新一天相對前一天的變化，依序回答：
1.【原因】今天變動最明顯的 1-2 項指標，用你查到的最新新聞/事件解釋「為什麼」會這樣變動（例如 OPEC+ 決策、地緣政治、Fed 談話、經濟數據公布等具體原因，不要只是複述數字）。
2.【傳導】這個變動可能如何牽動其他資產／指數（例如黃金、美元指數、科技股、公債），方向是看漲還是看跌，並說明傳導邏輯。
3.【總經結論】綜合這 5 項數據，對美股與台股各自給出目前總經氛圍的結論，以及可能的投資策略方向（不是報明牌，是總經層級的風險偏好判斷）。

請用繁體中文回答，段落分明，不需要客套話開場。"""


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None, "尚未設定 GEMINI_API_KEY（請在 ai_keys_local.py 填入）"
    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        f"?key={GEMINI_API_KEY}",
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            # -1 = 動態思考預算，讓模型自己決定要想多久，適合這種需要推因果的分析
            # （而不是用固定的低預算，逼它跳過推理直接給答案）。
            "generationConfig": {"thinkingConfig": {"thinkingBudget": -1}},
        },
        timeout=90,
    )
    if not resp.ok:
        return None, f"Gemini API 錯誤 {resp.status_code}: {resp.text[:500]}"
    candidates = resp.json().get("candidates", [])
    if not candidates:
        return None, "Gemini 沒有回傳內容"
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(p["text"] for p in parts if "text" in p)
    return text or None, None if text else "Gemini 沒有回傳文字內容"


def save_analysis(provider, text):
    data = {}
    if ANALYSIS_FILE.exists():
        try:
            data = json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[provider] = {
        "text": text,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    ANALYSIS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/analyze/"):
            self.send_response(404)
            self.end_headers()
            return

        provider = parsed.path.rsplit("/", 1)[-1]
        prompt = build_prompt()
        if provider == "gemini":
            text, error = call_gemini(prompt)
        else:
            text, error = None, f"未知的 provider: {provider}"

        if text:
            save_analysis(provider, text)

        body = json.dumps({"ok": bool(text), "text": text, "error": error}, ensure_ascii=False).encode("utf-8")
        self.send_response(200 if text else 500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[server]", fmt % args)


if __name__ == "__main__":
    os.chdir(DIR)
    with ThreadingTCPServer(("", PORT), Handler) as httpd:
        print(f"Serving {DIR} at http://localhost:{PORT}")
        httpd.serve_forever()
