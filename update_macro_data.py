"""
Fetch VIXTWN / VIX / Brent oil / US10Y / US30Y / HY OAS spread / DXY and merge into
macro_data.json. Run on either computer, any time of day. Pulls latest data
first, only overwrites fields it actually got a fresh value for (never
blanks a field with None), then commits and pushes.
"""
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

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


GIT_PULL_MAX_RETRIES = 2
GIT_PULL_RETRY_WAIT = 1.5  # 秒


def git_pull():
    """pull --rebase 偶爾會撞上「同一時間 server.py 也在存分析結果」的暫時性衝突
    （像是 FETCH_HEAD 被讀到一半），常見症狀是 "Cannot rebase onto multiple branches"
    這種跟遠端真實內容衝突無關的錯誤，等一下重試通常就會自己好。
    """
    for attempt in range(GIT_PULL_MAX_RETRIES + 1):
        code, out, err = run_git("pull", "--rebase")
        if code == 0:
            print(f"[git pull] {out or 'up to date'}")
            return
        if attempt == GIT_PULL_MAX_RETRIES:
            print(f"[警告] git pull 失敗，請手動處理後再重跑:\n{err}")
            sys.exit(1)
        print(f"[git pull] 失敗，{GIT_PULL_RETRY_WAIT} 秒後重試 (第 {attempt + 1}/{GIT_PULL_MAX_RETRIES} 次): {err}")
        time.sleep(GIT_PULL_RETRY_WAIT)


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
    """Returns (value, asof_date_iso) for the most recent completed daily bar."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="5d")
        if hist.empty:
            return None, None
        asof = hist.index[-1].strftime("%Y-%m-%d")
        return float(hist["Close"].iloc[-1]), asof
    except Exception as e:
        print(f"[警告] 抓取 {ticker} 失敗: {e}")
        return None, None


def fetch_vix():
    val, asof = fetch_yfinance_last_close("^VIX")
    return (round(val, 2), asof) if val is not None else (None, None)


def fetch_oil():
    val, asof = fetch_yfinance_last_close("BZ=F")
    return (round(val, 2), asof) if val is not None else (None, None)


def fetch_us10y():
    val, asof = fetch_yfinance_last_close("^TNX")
    return (round(val, 3), asof) if val is not None else (None, None)


def fetch_us30y():
    val, asof = fetch_yfinance_last_close("^TYX")
    return (round(val, 3), asof) if val is not None else (None, None)


def fetch_dxy():
    val, asof = fetch_yfinance_last_close("DX-Y.NYB")
    return (round(val, 2), asof) if val is not None else (None, None)


def fetch_spread():
    if not FRED_API_KEY:
        print("[警告] 找不到 FRED_API_KEY (config_local.py)，略過 spread")
        return None, None
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
            return None, None
        return float(obs["value"]), obs["date"]
    except Exception as e:
        print(f"[警告] 抓取 FRED spread 失敗: {e}")
        return None, None


def fetch_vixtwn():
    today = date.today()
    yyyymm = today.strftime("%Y%m")
    url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{yyyymm}new.txt"
    try:
        import requests
        resp = requests.get(url, timeout=10)
        lines = []
        if resp.status_code == 200:
            text = resp.content.decode("big5", errors="ignore")
            lines = [
                line for line in text.splitlines()
                if line.strip() and not line.startswith("-") and "交易日期" not in line
            ]
        # 若月初新月份文字檔尚未產生，嘗試讀取上一個月文字檔備用
        if not lines:
            prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
            prev_url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{prev_month}new.txt"
            resp_prev = requests.get(prev_url, timeout=10)
            if resp_prev.status_code == 200:
                text_prev = resp_prev.content.decode("big5", errors="ignore")
                lines = [
                    line for line in text_prev.splitlines()
                    if line.strip() and not line.startswith("-") and "交易日期" not in line
                ]
        if not lines:
            return None, None
        row_date, _time, value, _pre_close_avg = lines[-1].split()
        asof = f"{row_date[:4]}-{row_date[4:6]}-{row_date[6:]}" if len(row_date) == 8 else row_date
        return float(value), asof
    except Exception as e:
        print(f"[警告] 抓取 VIXTWN 失敗: {e}")
        return None, None


def get_latest_trading_day(d):
    """若 d 為週六 (5) 退回週五 (d-1)；若為週日 (6) 退回週五 (d-2)；否則回傳 d。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d - timedelta(days=2)
    return d


def get_prev_trading_day(d):
    """取得 d 之前的上一個交易日（自動跳過週末）。"""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def get_expected_asof(key, now):
    """跟網頁 getExpectedAsof() 邏輯一致：
    考慮週末（六、日無台美股收盤），計算當前時間點預期應拿到的最新交易日日期。"""
    today = now.date()
    h = now.hour

    if today.weekday() >= 5:
        return get_latest_trading_day(today).isoformat()

    if key == "vixtwn":
        return (today if h >= 14 else get_prev_trading_day(today)).isoformat()

    cutoff_hours = {"oil": 5, "spread": 22}
    cutoff = cutoff_hours.get(key, 4)
    if h >= cutoff:
        return get_prev_trading_day(today).isoformat()
    else:
        return get_prev_trading_day(get_prev_trading_day(today)).isoformat()


def main():
    git_pull()

    entries = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    today = date.today().isoformat()
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    fetched = {
        "vixtwn": fetch_vixtwn(),
        "vix": fetch_vix(),
        "oil": fetch_oil(),
        "us10y": fetch_us10y(),
        "us30y": fetch_us30y(),
        "spread": fetch_spread(),
        "dxy": fetch_dxy(),
    }

    entry = next((e for e in entries if e["date"] == today), None)
    if entry is None:
        entry = {
            "date": today, "vixtwn": None, "vix": None, "oil": None,
            "us10y": None, "us30y": None, "spread": None, "dxy": None,
        }
        entries.append(entry)

    meta = entry.get("_meta", {})
    updated_fields = []
    for key, (val, asof) in fetched.items():
        if val is not None:
            entry[key] = val
            meta[key] = {"asof": asof, "fetchedAt": now_iso}
            updated_fields.append(key)
    if meta:
        entry["_meta"] = meta

    entries.sort(key=lambda e: e["date"])
    DATA_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

    now = datetime.now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {today} 更新欄位: {updated_fields or '(無新資料)'}")
    for key, info in meta.items():
        expected = get_expected_asof(key, now)
        is_fresh = info["asof"] >= expected
        status = "🟢 目前最新" if is_fresh else f"⚪ 尚未更新（預期應有 {expected}）"
        print(f"  - {key}: 資料日期 {info['asof']} {status}")

    git_commit_and_push(today)


if __name__ == "__main__":
    main()
