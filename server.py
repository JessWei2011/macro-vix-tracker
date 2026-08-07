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
import threading
import time
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
3.【投資策略建議】依據這 5 項數據的走勢，針對以下 5 檔標的分別給出具體策略建議：QQQM、0050（台股大盤ETF）、IAU（黃金）、00948B、00953B。
   每一檔請明確給出「緊抱／加碼／減碼／出場」其中一種立場，並說明為什麼（要連回前面①②的因果邏輯）。

請用繁體中文回答，段落分明。"""


COMPRESS_INSTRUCTION = """你的任務是把輸入的總經分析文字，濃縮成精簡的因果箭頭鏈格式，只做濃縮改寫，禁止新增原文沒有的資訊，禁止替換或發明原文沒提到的資產代號。

【格式規則，違反視為失敗】
- 每一個論點寫成一行：事件 --> 中間結果 --> 中間結果 --> 最終結論，3~5 個節點，整行不超過 40 個字。
- 範例：📈 科技股（QQQM）: 原油上漲 --> 增加企業運營成本 --> 高通膨高利率 --> 不利科技股 --> QQQM看跌 📉
- 節點只能是名詞或極短詞組，不准有「可能」「因此」「顯示」等連接詞或完整句子，不准在箭頭鏈後面加冒號解釋。
- 保留原文的三個段落標題，並在標題前加對應 emoji：🔍【原因】、🔗【傳導】、💰【投資策略建議】。
- 【傳導】每一行結尾是看漲就加 📈，看跌就加 📉。
- 每一行開頭依內容性質加一個情境 emoji（自行判斷語意選用，例如地緣政治用 🌍、Fed/利率用 🏦、油價用 🛢️、恐慌情緒用 😨、黃金用 🥇、科技股用 💻，不要每行都用同一個）。
- 第三段【投資策略建議】只能包含這 5 檔，逐一列出、順序不變、代號不可更改替換：QQQM、0050、IAU、00948B、00953B。每檔一行，結尾保留「緊抱／加碼／減碼／出場」其中一種立場，並依立場在最前面加 emoji：🟢加碼、🟡緊抱、🔴減碼、⚫出場。
- 不要開場白、不要總結、不要客套話，只留三個標題加項目符號。
- 輸出必須用真正的換行分隔每一行，絕對不要輸出字面上的反斜線加n（\\n）這種字元。
- 整份輸出字數上限 300 字（不含 emoji）。

用繁體中文輸出。"""


def call_gemini_api(system_instruction, user_text, use_search):
    body = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "maxOutputTokens": 1024},
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if use_search:
        body["tools"] = [{"google_search": {}}]

    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        f"?key={GEMINI_API_KEY}",
        json=body,
        timeout=90,
    )
    if not resp.ok:
        return None, f"Gemini API 錯誤 {resp.status_code}: {resp.text[:500]}"
    body_json = resp.json()
    candidates = body_json.get("candidates", [])
    if not candidates:
        print(f"[gemini debug] 無 candidates，完整回應: {json.dumps(body_json, ensure_ascii=False)[:1000]}")
        return None, "Gemini 沒有回傳內容"
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(p["text"] for p in parts if "text" in p)
    # 偶爾模型會吐出字面上的反斜線+n，而不是真正的換行字元；直接修正掉。
    text = text.replace("\\n", "\n")
    if not text:
        finish_reason = candidates[0].get("finishReason")
        print(
            f"[gemini debug] 無文字內容，finishReason={finish_reason}，"
            f"candidate: {json.dumps(candidates[0], ensure_ascii=False)[:1000]}"
        )
    return (text or None), (None if text else "Gemini 沒有回傳文字內容")


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None, "尚未設定 GEMINI_API_KEY（請在 ai_keys_local.py 填入）"

    # 階段一：讓模型正常查資料、正常寫分析（開 google_search，不逼格式，寫得詳細沒關係）。
    # 實測發現：格式規則和 google_search 工具一起給時，模型幾乎每次都無視格式規則，
    # 寫成長篇報告。與其硬逼一次到位，不如讓它先把研究做好。
    raw_text, error = call_gemini_api(None, prompt, use_search=True)
    if not raw_text:
        return None, error

    # 階段二：另外開一次「純改寫」呼叫，不開搜尋工具，只把階段一的文字壓縮成因果箭頭鏈。
    # 拿掉搜尋工具的干擾後，格式指令的遵守度大幅提升。
    compress_input = (
        f"{raw_text}\n\n---\n"
        "提醒：【投資策略建議】只能是這 5 檔，不可替換成其他代號：QQQM、0050、IAU、00948B、00953B。"
    )
    compressed_text, error = call_gemini_api(COMPRESS_INSTRUCTION, compress_input, use_search=False)
    if not compressed_text:
        return raw_text, None  # 濃縮失敗就退回原始版本，總比沒有分析好

    return compressed_text, None


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
    def end_headers(self):
        # This is a local dev server whose files change constantly (data updates, code
        # updates) -- never let the browser cache anything, or it'll show stale pages.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/shutdown":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            # Give the response a moment to actually reach the browser before the process dies.
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0))).start()
            return

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
