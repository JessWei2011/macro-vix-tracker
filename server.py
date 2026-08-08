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
import subprocess
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
NOTIFY_CONFIG_FILE = DIR / "notify_config.json"
PORT = 8934

sys.path.insert(0, str(DIR))
try:
    from ai_keys_local import GEMINI_API_KEY
except ImportError:
    GEMINI_API_KEY = ""
try:
    from ai_keys_local import GROQ_API_KEY
except ImportError:
    GROQ_API_KEY = ""
try:
    from ai_keys_local import TAVILY_API_KEY
except ImportError:
    TAVILY_API_KEY = ""

FIELD_LABELS = {
    "vixtwn": "VIXTWN(台股期貨波動率指數)",
    "vix": "VIX(美股恐慌指數)",
    "oil": "布蘭特原油",
    "us10y": "10年美債殖利率",
    "spread": "Spread(美國高收益債利差)",
}


def build_data_table():
    entries = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    recent = entries[-10:]
    return "\n".join(
        f"{e['date']}: " + ", ".join(f"{FIELD_LABELS[k]}={e.get(k)}" for k in FIELD_LABELS)
        for e in recent
    )


ANALYSIS_QUESTIONS = """請針對最新一天相對前一天的變化，依序回答：
1.【原因】今天變動最明顯的 1-2 項指標，用你查到的最新新聞/事件解釋「為什麼」會這樣變動（例如 OPEC+ 決策、地緣政治、Fed 談話、經濟數據公布等具體原因，不要只是複述數字）。
2.【傳導】這個變動可能如何牽動其他資產／指數（例如黃金、美元指數、科技股、公債），方向是看漲還是看跌，並說明傳導邏輯。
3.【投資策略建議】依據這 5 項數據的走勢，針對以下 5 檔標的分別給出具體策略建議：QQQM、0050（台股大盤ETF）、IAU（黃金）、00948B、00953B。
   每一檔請明確給出「緊抱／加碼／減碼／出場」其中一種立場，並說明為什麼（要連回前面①②的因果邏輯）。

請用繁體中文回答，段落分明。"""


def build_prompt():
    table = build_data_table()
    return f"""你是一位總經與跨資產分析師。以下是最近幾天的五項市場指標數據：

{table}

{ANALYSIS_QUESTIONS}"""


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


def call_gemini_api(system_instruction, user_text, use_search, max_tokens=1024):
    body = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "maxOutputTokens": max_tokens},
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
        return None, f"Gemini API 錯誤 {resp.status_code}: {resp.text[:500]}", False
    body_json = resp.json()
    candidates = body_json.get("candidates", [])
    if not candidates:
        print(f"[gemini debug] 無 candidates，完整回應: {json.dumps(body_json, ensure_ascii=False)[:1000]}")
        return None, "Gemini 沒有回傳內容", False
    finish_reason = candidates[0].get("finishReason")
    truncated = finish_reason == "MAX_TOKENS"
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(p["text"] for p in parts if "text" in p)
    # 偶爾模型會吐出字面上的反斜線+n，而不是真正的換行字元；直接修正掉。
    text = text.replace("\\n", "\n")
    if not text:
        print(
            f"[gemini debug] 無文字內容，finishReason={finish_reason}，"
            f"candidate: {json.dumps(candidates[0], ensure_ascii=False)[:1000]}"
        )
    elif truncated:
        print(f"[gemini debug] 回應被 maxOutputTokens 截斷 (finishReason=MAX_TOKENS)，長度={len(text)} 字")
    return (text or None), (None if text else "Gemini 沒有回傳文字內容"), truncated


def call_groq_api(system_instruction, user_text, max_tokens=1024):
    """階段二（純改寫壓縮，不需要搜尋）改用 Groq 跑，分攤掉 Gemini 的 google_search 額度，
    因為真正卡住免費額度的是階段一那個有開搜尋工具的呼叫，階段二只是文字重排，
    換去哪家模型都不影響品質，但可以讓 Gemini 的額度只被階段一消耗，用量直接砍半。
    Groq 的 API 是 OpenAI 相容格式（/chat/completions），不是 Gemini 那種格式。
    """
    if not GROQ_API_KEY:
        return None, "尚未設定 GROQ_API_KEY（請在 ai_keys_local.py 填入）", False

    messages = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": user_text})

    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    if not resp.ok:
        return None, f"Groq API 錯誤 {resp.status_code}: {resp.text[:500]}", False
    body_json = resp.json()
    choices = body_json.get("choices", [])
    if not choices:
        print(f"[groq debug] 無 choices，完整回應: {json.dumps(body_json, ensure_ascii=False)[:1000]}")
        return None, "Groq 沒有回傳內容", False
    finish_reason = choices[0].get("finish_reason")
    truncated = finish_reason == "length"
    text = (choices[0].get("message", {}) or {}).get("content", "") or ""
    if not text:
        print(f"[groq debug] 無文字內容，finish_reason={finish_reason}")
    elif truncated:
        print(f"[groq debug] 回應被 max_tokens 截斷 (finish_reason=length)，長度={len(text)} 字")
    return (text or None), (None if text else "Groq 沒有回傳文字內容"), truncated


def call_tavily_search(query, max_results=5):
    """Gemini 的 google_search 額度用完時，用這個當替代的搜尋來源。
    Tavily 回傳的是已經整理過的網頁摘要（title/content/url），可以直接塞進
    LLM prompt 當作「查到的新聞」使用，不用自己再寫爬蟲/解析 HTML。
    """
    if not TAVILY_API_KEY:
        return None, "尚未設定 TAVILY_API_KEY（請在 ai_keys_local.py 填入）"
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": False,
            },
            timeout=30,
        )
    except requests.RequestException as e:
        return None, f"Tavily 連線失敗: {e}"
    if not resp.ok:
        return None, f"Tavily API 錯誤 {resp.status_code}: {resp.text[:500]}"
    results = resp.json().get("results", [])
    if not results:
        return None, "Tavily 沒有搜尋到結果"
    formatted = "\n\n".join(
        f"【{r.get('title', '')}】\n{r.get('content', '')[:400]}\n來源: {r.get('url', '')}"
        for r in results
    )
    return formatted, None


TAVILY_SEARCH_QUERY = "VIX恐慌指數 布蘭特原油 美國10年公債殖利率 高收益債利差 今日財經新聞"


def call_stage1_via_tavily_and_groq():
    """Gemini 的 google_search 額度用完（階段一失敗）時的完整替代路線：
    自己用 Tavily 查新聞，把查到的內容連同數據表一起交給 Groq 寫階段一分析。
    跟原本 Gemini 階段一的差別只在「查資料」跟「寫分析」分別由誰做，
    輸出格式（三段式問答）維持一致，好讓後面的階段二壓縮邏輯不用跟著改。
    """
    search_context, error = call_tavily_search(TAVILY_SEARCH_QUERY)
    if not search_context:
        return None, f"Tavily 搜尋失敗: {error}"

    table = build_data_table()
    prompt = f"""你是一位總經與跨資產分析師。以下是最近幾天的五項市場指標數據：

{table}

以下是搜尋到的相關財經新聞片段，可用來解釋「為什麼」指標會這樣變動：

{search_context}

{ANALYSIS_QUESTIONS}"""

    text, error, truncated = call_groq_api(None, prompt, max_tokens=4096)
    if not text:
        return None, f"Groq 撰寫階段一分析失敗: {error}"
    if truncated:
        text += "\n\n⚠️（內容可能因長度限制被截斷）"
    return text, None


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None, "尚未設定 GEMINI_API_KEY（請在 ai_keys_local.py 填入）"

    # 階段一：讓模型正常查資料、正常寫分析（開 google_search，不逼格式，寫得詳細沒關係）。
    # 實測發現：格式規則和 google_search 工具一起給時，模型幾乎每次都無視格式規則，
    # 寫成長篇報告。與其硬逼一次到位，不如讓它先把研究做好。
    # max_tokens 給大一點空間 (4096)：這階段是詳細中文長文 + google_search 的 grounding
    # 內容都算在輸出裡，1024 太容易被硬切斷（曾發生截到一半、句子沒寫完就沒了）。
    raw_text, error, raw_truncated = call_gemini_api(None, prompt, use_search=True, max_tokens=4096)
    if not raw_text:
        # Gemini 的 google_search 額度通常比模型本身的請求額度小很多，最容易先在
        # 這裡被打回票。整條線改用 Tavily 查資料 + Groq 寫分析頂上，避免直接失敗。
        print(f"[gemini debug] 階段一失敗（{error}），改用 Tavily+Groq 頂上")
        raw_text, fallback_error = call_stage1_via_tavily_and_groq()
        if not raw_text:
            return None, f"Gemini 失敗（{error}），Tavily+Groq 備援也失敗（{fallback_error}）"
        raw_truncated = False

    # 階段二：另外開一次「純改寫」呼叫，不開搜尋工具，只把階段一的文字壓縮成因果箭頭鏈。
    # 拿掉搜尋工具的干擾後，格式指令的遵守度大幅提升。COMPRESS_INSTRUCTION 本身就要求輸出
    # 上限 300 字，1536 給足夠餘裕但不會浪費太多。
    # 這段不需要搜尋，改丟給 Groq（免費、快）跑，真正卡免費額度的是階段一的
    # google_search 呼叫，讓階段二完全不碰 Gemini，等於把 Gemini 用量砍半。
    # Groq 沒設定 key 或臨時出錯時，退回原本用 Gemini 跑這段，不會整條線斷掉。
    compress_input = (
        f"{raw_text}\n\n---\n"
        "提醒：【投資策略建議】只能是這 5 檔，不可替換成其他代號：QQQM、0050、IAU、00948B、00953B。"
    )
    compressed_text, error, compress_truncated = call_groq_api(
        COMPRESS_INSTRUCTION, compress_input, max_tokens=1536
    )
    if not compressed_text:
        print(f"[groq debug] 階段二改用 Groq 失敗（{error}），退回用 Gemini 跑這段")
        compressed_text, error, compress_truncated = call_gemini_api(
            COMPRESS_INSTRUCTION, compress_input, use_search=False, max_tokens=1536
        )
    if not compressed_text:
        # 濃縮失敗就退回原始版本，總比沒有分析好；但如果原始版本本身也被截斷過，
        # 要清楚標記出來，不要讓半句話看起來像是正常存檔的完整分析。
        note = "\n\n⚠️（此版本內容因回應長度限制被截斷，不完整，建議稍後重新分析）" if raw_truncated else ""
        return raw_text + note, None

    if compress_truncated:
        compressed_text += "\n\n⚠️（內容可能因長度限制被截斷）"
    return compressed_text, None


def save_notify_config(cfg):
    result = subprocess.run(["git", "pull", "--rebase"], cwd=DIR, capture_output=True, text=True)
    if result.returncode != 0:
        return f"git pull 失敗，未儲存（請手動處理後重新整理頁面重試）: {result.stderr.strip()}"
    NOTIFY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "notify_config.json"], cwd=DIR, capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "commit", "-m", "Update notify thresholds"], cwd=DIR, capture_output=True, text=True
    )
    commit_output = commit.stdout + commit.stderr
    nothing_changed = "nothing to commit" in commit_output or "nothing added to commit" in commit_output
    if commit.returncode != 0 and not nothing_changed:
        return f"已存到本機，但 git commit 失敗: {commit.stderr.strip()}"
    push = subprocess.run(["git", "push"], cwd=DIR, capture_output=True, text=True)
    if push.returncode != 0:
        return f"已存到本機並 commit，但 git push 失敗（請手動執行 push.bat）: {push.stderr.strip()}"
    return None


def save_analysis(provider, text):
    """寫入分析結果並同步到 git，讓兩台電腦都看得到同一份最新結果。

    做法跟 save_notify_config() 一樣：先 pull 再寫檔再 commit/push，
    避免像之前那樣把檔案改成「已追蹤但沒 commit」的懸空狀態，
    導致下次 update.bat 的 git pull --rebase 卡住。
    回傳 None 代表全部成功；否則回傳一則可以直接顯示給使用者看的錯誤說明
    （分析結果本身已經寫進本機檔案，不會因為同步失敗而遺失）。
    """
    pull = subprocess.run(["git", "pull", "--rebase"], cwd=DIR, capture_output=True, text=True)
    if pull.returncode != 0:
        return f"git pull 失敗，本次分析結果只存在本機、尚未同步: {pull.stderr.strip()}"

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

    subprocess.run(["git", "add", "macro_analysis.json"], cwd=DIR, capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "commit", "-m", f"Update {provider} macro analysis"], cwd=DIR, capture_output=True, text=True
    )
    commit_output = commit.stdout + commit.stderr
    nothing_changed = "nothing to commit" in commit_output or "nothing added to commit" in commit_output
    if commit.returncode != 0 and not nothing_changed:
        return f"已存到本機，但 git commit 失敗: {commit.stderr.strip()}"
    push = subprocess.run(["git", "push"], cwd=DIR, capture_output=True, text=True)
    if push.returncode != 0:
        return f"已存到本機並 commit，但 git push 失敗（請手動執行 push.bat）: {push.stderr.strip()}"
    return None


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

        if parsed.path == "/api/save-notify-config":
            length = int(self.headers.get("Content-Length", 0))
            try:
                cfg = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                cfg = None
            if not isinstance(cfg, dict):
                body = json.dumps({"ok": False, "error": "無效的設定內容"}).encode("utf-8")
                self.send_response(400)
            else:
                error = save_notify_config(cfg)
                body = json.dumps({"ok": error is None, "error": error}, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if error is None else 500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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

        sync_error = save_analysis(provider, text) if text else None

        body = json.dumps(
            {"ok": bool(text), "text": text, "error": error, "syncError": sync_error}, ensure_ascii=False
        ).encode("utf-8")
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
