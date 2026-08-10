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
import re
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
    "dxy": "美元指數(DXY)",
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
3.【投資策略建議】依據這 6 項數據的走勢，針對以下 5 檔標的分別給出具體策略建議：QQQM、0050（台股大盤ETF）、IAU（黃金）、00948B、00953B。
   每一檔請明確給出「緊抱／加碼／減碼／出場」其中一種立場，並說明為什麼（要連回前面①②的因果邏輯）。

請用繁體中文回答，段落分明。"""


def build_prompt():
    table = build_data_table()
    return f"""你是一位總經與跨資產分析師。以下是最近幾天的六項市場指標數據：

{table}

{ANALYSIS_QUESTIONS}"""


COMPRESS_INSTRUCTION = """你的任務是把輸入的總經分析文字，濃縮成精簡的因果箭頭鏈格式，只做濃縮改寫，禁止新增原文沒有的資訊，禁止替換或發明原文沒提到的資產代號。

【格式規則，違反視為失敗】
- 每一個論點寫成一行：事件 --> 中間結果 --> 中間結果 --> 最終結論，硬性規定至少 3 個節點（起點+至少1個中間傳導機制+結論），整行不超過 40 個字。
- 嚴禁退化成只有 2 個節點的寫法，例如「科技股 --> 看跌」這種跳過中間推理、只剩結論的寫法一律視為失敗，必須補回原文中「為什麼」的那個中間節點（例如殖利率上升／原油上漲／通膨預期等具體傳導機制），讓讀者不用回頭看原文也知道為什麼。
- 範例：📈 科技股（QQQM）: 原油上漲 --> 增加企業運營成本 --> 高通膨高利率 --> 不利科技股 --> QQQM看跌 📉
- 節點只能是名詞或極短詞組，不准有「可能」「因此」「顯示」等連接詞或完整句子，不准在箭頭鏈後面加冒號解釋。
- 同一個資產／指數如果在不同行出現方向不同的結論（例如科技股一行看跌、另一行又看漲），兩行的中間節點必須分別點出各自不同的驅動因子，讓讀者看得出兩行在講不同的傳導路徑、不是互相矛盾；不能兩行都只寫「資產 --> 方向」導致看起來像是同一件事却給出相反答案。
- 保留原文的三個段落標題，並在標題前加對應 emoji：🔍【原因】、🔗【傳導】、💰【投資策略建議】。
- 【傳導】每一行結尾是看漲就加 📈，看跌就加 📉。
- 每一行開頭依內容性質加一個情境 emoji（自行判斷語意選用，例如地緣政治用 🌍、Fed/利率用 🏦、油價用 🛢️、恐慌情緒用 😨、黃金用 🥇、科技股用 💻，不要每行都用同一個）。
- 第三段【投資策略建議】只能包含這 5 檔，逐一列出、順序不變、代號不可更改替換：QQQM、0050、IAU、00948B、00953B。每檔一行，格式是「emoji 代號 立場（3~8字關鍵理由）」，立場只能是「緊抱／加碼／減碼／出場」其中一種，並依立場在最前面加 emoji：🟢加碼、🟡緊抱、🔴減碼、⚫出場。
- 【投資策略建議】嚴禁幻覺：括號裡的關鍵理由必須是前面【原因】【傳導】兩段實際出現過的因果節點，不准為了湊格式編造原文沒提過的理由。這條規則沒有例外，括號絕對不能省略。
- 如果原文對某檔標的完全沒有給出具體驅動因子，就照實寫「🟡 IAU 緊抱（原文未討論，僅供參考）」這種格式老實承認沒依據，不准硬掰一個看起來煞有其事、但其實原文完全沒出現過的立場或理由，也不准直接省略括號蒙混過去。
- 正確範例：🟢 0050 加碼（風險偏好回升）／🔴 IAU 減碼（美元走強承壓）／🟡 IAU 緊抱（原文未討論，僅供參考）
- 錯誤範例（絕對不要這樣寫，沒有括號說明理由視為違反格式規則）：🟢 IAU 加碼
- 不要開場白、不要總結、不要客套話，只留三個標題加項目符號。
- 輸出必須用真正的換行分隔每一行，絕對不要輸出字面上的反斜線加n（\\n）這種字元。
- 整份輸出字數上限 450 字（不含 emoji）——比原本上限多留一點空間，因為保留中間推理節點跟投資建議的關鍵理由會比之前的極簡版本多佔一些字數，不要為了硬壓字數又把節點砍回 2 個或把理由拿掉。

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


GROQ_RETRY_WAIT_RE = re.compile(r"try again in ([\d.]+)(ms|s)", re.IGNORECASE)
GROQ_MAX_RETRIES = 2


def _parse_groq_retry_wait(error_text):
    """從 429 錯誤訊息裡的「Please try again in 405ms」抓出建議等待時間（秒）。
    這種 TPM（每分鐘 token 數）瞬間超標多半幾百 ms 內就恢復，抓不到就給個保守預設值。
    """
    m = GROQ_RETRY_WAIT_RE.search(error_text)
    if not m:
        return 1.0
    value = float(m.group(1))
    return value / 1000 if m.group(2).lower() == "ms" else value


def call_groq_api(system_instruction, user_text, max_tokens=1024):
    """階段二（純改寫壓縮，不需要搜尋）改用 Groq 跑，分攤掉 Gemini 的 google_search 額度，
    因為真正卡住免費額度的是階段一那個有開搜尋工具的呼叫，階段二只是文字重排，
    換去哪家模型都不影響品質，但可以讓 Gemini 的額度只被階段一消耗，用量直接砍半。
    Groq 的 API 是 OpenAI 相容格式（/chat/completions），不是 Gemini 那種格式。

    429（TPM 瞬間超標）通常幾百毫秒內就恢復，不是真的額度用完，所以遇到 429 會照錯誤訊息
    建議的等待時間自動重試幾次，不用使用者手動再按一次分析按鈕。
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

    resp = None
    for attempt in range(GROQ_MAX_RETRIES + 1):
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        if resp.status_code != 429 or attempt == GROQ_MAX_RETRIES:
            break
        wait = _parse_groq_retry_wait(resp.text) + 0.2  # 加一點緩衝，避免卡在邊界又撞一次
        print(f"[groq debug] 429 TPM 超標，{wait:.2f} 秒後重試 (第 {attempt + 1}/{GROQ_MAX_RETRIES} 次)")
        time.sleep(wait)

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


TAVILY_SEARCH_QUERIES = [
    "VIX恐慌指數 布蘭特原油 美國10年公債殖利率 高收益債利差 今日財經新聞",
    # 單獨開一個查詢專門找 Fed 降息機率／就業數據這類「利率預期」催化劑，因為
    # 這類新聞常常是黃金、美元、公債同時變動的關鍵驅動，但不見得會被上面那組
    # 偏指標名稱的關鍵字搜到（實測就漏掉過一次非農就業→降息機率→黃金上漲的鏈）。
    "Fed 聯準會 降息機率 非農就業 CPI 通膨 最新消息",
]


def call_stage1_via_tavily_and_groq():
    """Gemini 的 google_search 額度用完（階段一失敗）時的完整替代路線：
    自己用 Tavily 查新聞，把查到的內容連同數據表一起交給 Groq 寫階段一分析。
    跟原本 Gemini 階段一的差別只在「查資料」跟「寫分析」分別由誰做，
    輸出格式（三段式問答）維持一致，好讓後面的階段二壓縮邏輯不用跟著改。
    """
    search_contexts = []
    for query in TAVILY_SEARCH_QUERIES:
        context, error = call_tavily_search(query)
        if context:
            search_contexts.append(context)
        else:
            print(f"[tavily debug] 查詢「{query}」失敗（{error}），跳過這條，繼續用其他查詢結果")
    if not search_contexts:
        return None, "Tavily 搜尋全部失敗"
    search_context = "\n\n".join(search_contexts)

    table = build_data_table()
    prompt = f"""你是一位總經與跨資產分析師。以下是最近幾天的六項市場指標數據：

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


STANCE_LINE_RE = re.compile(r"^[🟢🟡🔴⚫]\s*\S+\s+(緊抱|加碼|減碼|出場)\s*$")


def flag_ungrounded_investment_lines(text):
    """COMPRESS_INSTRUCTION 已經要求【投資策略建議】每一行都要附括號理由，
    但實測發現 Groq/Gemini 都不是每次乖乖照做——尤其原文本身就沒給某檔標的
    理由時，模型常常直接照抄成一行光禿禿的立場（例如「🟢 IAU 加碼」），
    看起來像是有憑有據，其實原文根本沒討論過。與其靠 prompt 硬凹到 100%
    遵守，這裡直接用程式碼掃過一遍：沒有括號理由的立場行，強制補上警語，
    確保絕對不會有「看起來像有根據、其實沒有」的立場漏網。
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if STANCE_LINE_RE.match(line.strip()):
            lines[i] = line.rstrip() + "（原文未充分討論，僅供參考）"
    return "\n".join(lines)


def compress_to_arrow_chain(raw_text, raw_truncated=False):
    """階段二：把階段一寫好的長文壓縮成因果箭頭鏈格式，Gemini/Tavily+Groq 兩條路線
    到這裡都共用同一段邏輯。優先用 Groq（免搜尋、免占用 Gemini 額度），失敗才退回 Gemini。
    """
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
    compressed_text = flag_ungrounded_investment_lines(compressed_text)
    return compressed_text, None


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None, None, "尚未設定 GEMINI_API_KEY（請在 ai_keys_local.py 填入）"

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
            return None, None, f"Gemini 失敗（{error}），Tavily+Groq 備援也失敗（{fallback_error}）"
        raw_truncated = False

    compressed_text, error = compress_to_arrow_chain(raw_text, raw_truncated)
    return compressed_text, raw_text, error


def run_fallback_analysis():
    """給「Tavily+Groq」按鈕用，跳過 Gemini，直接跑 Tavily 搜尋 + Groq 分析這條線。
    現在跟 Gemini 那條線是平行的兩組獨立結果（給使用者互相比對/仲裁用），不再只是
    「Gemini 失敗才用的備援」。
    """
    raw_text, error = call_stage1_via_tavily_and_groq()
    if not raw_text:
        return None, None, error
    compressed_text, error = compress_to_arrow_chain(raw_text)
    return compressed_text, raw_text, error


ARBITRATION_INSTRUCTION = """你是一位具備即時網路搜尋能力的總經與跨資產分析師。以下有兩組各自獨立產生、針對同一份市場數據的分析（分析組 A 與分析組 B），兩者的結論可能互相矛盾。你的任務是「仲裁」，判斷誰的論點比較站得住腳，而不是各打五十大板、各取一半。

【仲裁規則 — 請務必遵守】
1. 請優先動用你的搜尋能力，逐一查證兩組分析裡各自引用的關鍵新聞／數據事件是否真實存在、發生時間是否吻合最新一天的市場數據，而不是單純比較兩段文字何者邏輯比較通順。
2. 如果其中一組引用了對方完全沒提到的關鍵事件（例如某項經濟數據公布、地緣政治事件），請特別查證這個事件是否真的發生、是否足以解釋走勢——這往往是判斷誰比較可信的關鍵，比純邏輯比較更重要。
3. 針對【投資策略建議】裡的這 5 檔標的（QQQM、0050、IAU、00948B、00953B），逐一輸出：
   - 標的代號
   - 可信度較高的是哪一組（A／B／兩者皆可信／兩者皆不可信）
   - 一句話原因（依查證結果，不是憑空猜測）
   - 一句話最終推論建議（緊抱／加碼／減碼／出場其中一種；證據不足時才寫「證據不足，維持觀望」）
4. 回答務必精簡：每檔標的只用 3-4 行說完，不要長篇大論、不要覆述兩組原文全文、不要客套話開場白或總結。

請直接依序輸出這 5 檔標的的仲裁結果，用繁體中文回答。"""


def build_arbitration_prompt():
    """把「數據表 + G+G 與 T+G 兩條線各自的第一階段分析原文 + 仲裁指令」組成一段文字，
    給使用者複製到網頁版 AI（建議用 Perplexity 或 Gemini 網頁版，兩者都有即時搜尋能力）
    手動仲裁用。刻意不做成自動 API 呼叫：仲裁只偶爾需要、且網頁版通常額度比 API 免費層
    寬鬆很多，用網頁版人工貼上完全不會多消耗任何 API 額度。
    """
    data = {}
    if ANALYSIS_FILE.exists():
        try:
            data = json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    gemini_raw = (data.get("gemini") or {}).get("rawText")
    fallback_raw = (data.get("fallback") or {}).get("rawText")
    missing = []
    if not gemini_raw:
        gemini_raw = "（尚無分析原文，請先按左側「Gemini+Groq」跑一次）"
        missing.append("Gemini+Groq")
    if not fallback_raw:
        fallback_raw = "（尚無分析原文，請先按右側「Tavily+Groq」跑一次）"
        missing.append("Tavily+Groq")

    table = build_data_table()
    prompt = f"""{ARBITRATION_INSTRUCTION}

【原始市場數據】
{table}

【分析組 A：Gemini+Groq】
{gemini_raw}

【分析組 B：Tavily+Groq】
{fallback_raw}"""
    return prompt, missing


GIT_LOCK = threading.Lock()
GIT_PULL_MAX_RETRIES = 2
GIT_PULL_RETRY_WAIT = 1.5  # 秒


def _git_pull_rebase_with_retry(cwd):
    """git pull --rebase 偶爾會撞上「同一時間有另一個 process 也在動這個 repo」的暫時性衝突
    （例如 update_macro_data.py 的自動同步、或另一台電腦剛好也在 push），常見症狀就是
    FETCH_HEAD 被讀到一半的狀態，跳出 "Cannot rebase onto multiple branches" 這類跟遠端
    真實內容衝突無關的錯誤。這種情況本身沒有需要解決的衝突，等一下重試通常就會自己好。
    GIT_LOCK 只能防住同一個 server.py process 裡的請求互撞，防不住外部的
    update_macro_data.py，所以這裡额外加重試而不是只靠鎖。
    """
    result = None
    for attempt in range(GIT_PULL_MAX_RETRIES + 1):
        result = subprocess.run(["git", "pull", "--rebase"], cwd=cwd, capture_output=True, text=True)
        if result.returncode == 0 or attempt == GIT_PULL_MAX_RETRIES:
            break
        print(
            f"[git debug] pull --rebase 失敗，{GIT_PULL_RETRY_WAIT} 秒後重試 "
            f"(第 {attempt + 1}/{GIT_PULL_MAX_RETRIES} 次): {result.stderr.strip()}"
        )
        time.sleep(GIT_PULL_RETRY_WAIT)
    return result


def save_notify_config(cfg):
    with GIT_LOCK:
        return _save_notify_config_locked(cfg)


def _save_notify_config_locked(cfg):
    result = _git_pull_rebase_with_retry(DIR)
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


def save_analysis(provider, text, raw_text=None):
    """寫入分析結果並同步到 git，讓兩台電腦都看得到同一份最新結果。

    除了濃縮後的 text，也把 stage1 原文（raw_text）一併存下來——這是仲裁 prompt
    要用的素材，濃縮後的箭頭鏈格式資訊密度太高、細節被砍光了，仲裁時需要看回
    未濃縮的完整推理過程才能查證雙方引用的事件是否屬實。

    做法跟 save_notify_config() 一樣：先 pull 再寫檔再 commit/push，
    先寫本機檔案，寫完才碰 git（commit -> pull --rebase -> push），這樣不管後面
    git 發生什麼事，這次分析結果都已經真的存進 macro_analysis.json，不會憑空消失。
    commit 放在 pull 前面是刻意的：本機這次的變更先進一個 commit，「pull --rebase」
    才有東西可以疊在遠端新 commit 上面重放，不會再卡「working tree 有未提交變更」
    這個之前踩過的坑。
    回傳 None 代表全部成功（本機+同步都完成）；否則回傳一則可以直接顯示給使用者看
    的錯誤說明，但無論如何本機檔案這時已經寫好了。
    """
    with GIT_LOCK:
        return _save_analysis_locked(provider, text, raw_text)


def _save_analysis_locked(provider, text, raw_text):
    data = {}
    if ANALYSIS_FILE.exists():
        try:
            data = json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[provider] = {
        "text": text,
        "rawText": raw_text,
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

    pull = _git_pull_rebase_with_retry(DIR)
    if pull.returncode != 0:
        return f"已存到本機並 commit，但 git pull --rebase 失敗（可能跟遠端衝突，請手動處理）: {pull.stderr.strip()}"

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

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/arbitration-prompt":
            prompt, missing = build_arbitration_prompt()
            body = json.dumps({"ok": True, "text": prompt, "missing": missing}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

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
            text, raw_text, error = call_gemini(prompt)
        elif provider == "fallback":
            text, raw_text, error = run_fallback_analysis()
        else:
            text, raw_text, error = None, None, f"未知的 provider: {provider}"

        sync_error = save_analysis(provider, text, raw_text) if text else None

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
