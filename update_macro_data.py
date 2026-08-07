"""
Fetch VIX / Brent oil / US10Y / HY OAS spread and merge into macro_data.json.
Run on either computer, any time of day. Pulls latest data first, only
overwrites fields it actually got a fresh value for (never blanks a field
with None), then commits and pushes.

VIXTWN is not yet automated (no free official API) -- keep entering it
manually via the web page until that's solved.
"""
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent
DATA_FILE = DIR / "macro_data.json"

sys.path.insert(0, str(DIR))
try:
    from config_local import FRED_API_KEY
except ImportError:
    FRED_API_KEY = None


def run_git(*args):
    result = subprocess.run(["git", *args], cwd=DIR, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def git_pull():
    code, out, err = run_git("pull", "--rebase")
    if code != 0:
        print(f"[警告] git pull 失敗，請手動處理後再重跑:\n{err}")
        sys.exit(1)
    print(f"[git pull] {out or 'up to date'}")


def git_commit_and_push(today):
    code, out, err = run_git("add", "macro_data.json")
    code, out, err = run_git("commit", "-m", f"Auto update macro data for {today}")
    if code != 0:
        if "nothing to commit" in (out + err):
            print("[git] 沒有新的變動，略過 commit/push")
            return
        print(f"[警告] git commit 失敗:\n{err}")
        return
    print(f"[git commit] {out}")
    code, out, err = run_git("push")
    if code != 0:
        print(f"[警告] git push 失敗，請手動執行 push.bat:\n{err}")
    else:
        print("[git push] 完成")


def fetch_yfinance_last_close(ticker):
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"[警告] 抓取 {ticker} 失敗: {e}")
        return None


def fetch_vix():
    val = fetch_yfinance_last_close("^VIX")
    return round(val, 2) if val is not None else None


def fetch_oil():
    val = fetch_yfinance_last_close("BZ=F")
    return round(val, 2) if val is not None else None


def fetch_us10y():
    val = fetch_yfinance_last_close("^TNX")
    return round(val, 3) if val is not None else None


def fetch_spread():
    if not FRED_API_KEY:
        print("[警告] 找不到 FRED_API_KEY (config_local.py)，略過 spread")
        return None
    try:
        import requests
        url = (
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=BAMLH0A0HYM2&api_key={FRED_API_KEY}"
            "&file_type=json&sort_order=desc&limit=1"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        obs = resp.json()["observations"][0]
        if obs["value"] == ".":
            return None
        return float(obs["value"])
    except Exception as e:
        print(f"[警告] 抓取 FRED spread 失敗: {e}")
        return None


def fetch_vixtwn():
    # TODO: 期交所沒有正式免費 API，尚未自動化，先留空由網頁手動輸入
    return None


def main():
    git_pull()

    entries = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    today = date.today().isoformat()

    fetched = {
        "vixtwn": fetch_vixtwn(),
        "vix": fetch_vix(),
        "oil": fetch_oil(),
        "us10y": fetch_us10y(),
        "spread": fetch_spread(),
    }

    entry = next((e for e in entries if e["date"] == today), None)
    if entry is None:
        entry = {"date": today, "vixtwn": None, "vix": None, "oil": None, "us10y": None, "spread": None}
        entries.append(entry)

    updated_fields = []
    for key, val in fetched.items():
        if val is not None:
            entry[key] = val
            updated_fields.append(key)

    entries.sort(key=lambda e: e["date"])
    DATA_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {today} 更新欄位: {updated_fields or '(無新資料)'}")

    git_commit_and_push(today)


if __name__ == "__main__":
    main()
