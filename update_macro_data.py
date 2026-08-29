"""Fetch macro indicators and merge fresh values into local macro_data.json."""
import json
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DIR = Path(__file__).resolve().parent
DATA_FILE = DIR / "macro_data.json"
UPDATE_STATUS_FILE = DIR / "macro_update_status.json"
FIELDS = ("vixtwn", "vix", "oil", "gold", "us10y", "us30y", "spread", "dxy")

sys.path.insert(0, str(DIR))
try:
    from config_local import FRED_API_KEY
except ImportError:
    FRED_API_KEY = None


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_update_status():
    try:
        data = json.loads(UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_update_status(state, run_id, **details):
    """原子更新本機狀態檔，讓網頁輪詢時不會讀到半份 JSON。"""
    payload = {
        "state": state,
        "runId": run_id,
        "updatedAt": now_iso(),
        **details,
    }
    temp_file = UPDATE_STATUS_FILE.with_suffix(UPDATE_STATUS_FILE.suffix + ".tmp")
    temp_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(UPDATE_STATUS_FILE)


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


def fetch_gold_history():
    """取得 XAUS 的 XAU/USD 每日收盤歷史，最長可回傳五年資料。"""
    try:
        import requests
        response = requests.get("https://xaus.com/api/v1/history", timeout=15)
        response.raise_for_status()
        payload = response.json()
        points = payload.get("points", [])
        history = {
            point["d"]: round(float(point["c"]), 2)
            for point in points
            if isinstance(point, dict) and point.get("d") and point.get("c") is not None
        }
        if not history:
            raise ValueError("API 沒有回傳 XAU/USD 歷史點位")
        return history
    except Exception as e:
        print(f"[警告] 抓取 XAU/USD 現貨黃金歷史失敗: {e}")
        return {}


def fetch_gold(history):
    """從每日歷史序列取得最近一個完成交易日的現貨黃金收盤價。"""
    if not history:
        return None, None
    asof = max(history)
    return history[asof], asof


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

    cutoff_hours = {"oil": 5, "gold": 5, "spread": 22}
    cutoff = cutoff_hours.get(key, 4)
    if h >= cutoff:
        return get_prev_trading_day(today).isoformat()
    else:
        return get_prev_trading_day(get_prev_trading_day(today)).isoformat()


def main():
    previous_status = read_update_status()
    run_id = (
        previous_status.get("runId")
        if previous_status.get("state") == "updating" and previous_status.get("runId")
        else uuid.uuid4().hex
    )
    started_at = previous_status.get("startedAt") or now_iso()
    entries = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    today = date.today().isoformat()
    fetched_at = now_iso()

    write_update_status(
        "updating", run_id, startedAt=started_at, phase="fetching",
        message="正在抓取八項總經資料（含現貨黃金）…", updatedFields=[], failedFields=[],
    )

    gold_history = fetch_gold_history()
    fetched = {
        "vixtwn": fetch_vixtwn(),
        "vix": fetch_vix(),
        "oil": fetch_oil(),
        "gold": fetch_gold(gold_history),
        "us10y": fetch_us10y(),
        "us30y": fetch_us30y(),
        "spread": fetch_spread(),
        "dxy": fetch_dxy(),
    }

    updated_fields = [key for key, (val, _asof) in fetched.items() if val is not None]
    failed_fields = [key for key in FIELDS if key not in updated_fields]
    if not updated_fields:
        write_update_status(
            "failed", run_id, startedAt=started_at, finishedAt=now_iso(), phase="failed",
            message="八項資料皆抓取失敗，未載入舊資料。",
            error="所有資料來源都沒有回傳可用數值。",
            updatedFields=[], failedFields=failed_fields,
        )
        return 1

    entry = next((e for e in entries if e["date"] == today), None)
    if entry is None:
        entry = {
            "date": today, "vixtwn": None, "vix": None, "oil": None,
            "gold": None, "us10y": None, "us30y": None, "spread": None, "dxy": None,
        }
        entries.append(entry)

    # 以同一個正式日線來源回填已有資料日期，首次加入就能畫出完整走線圖。
    for historical_entry in entries:
        historical_date = historical_entry.get("date")
        if historical_date in gold_history:
            historical_entry["gold"] = gold_history[historical_date]

    meta = entry.get("_meta", {})
    for key, (val, asof) in fetched.items():
        if val is not None:
            entry[key] = val
            meta[key] = {"asof": asof, "fetchedAt": fetched_at}
    if meta:
        entry["_meta"] = meta

    # 供 JSON 使用者直接閱讀的現貨黃金報告；前端表格只使用 gold 數列與 _meta 的最新日期。
    gold_value, gold_asof = fetched["gold"]
    if gold_value is not None and gold_asof:
        reports = entry.get("_reports", {})
        reports["gold_spot"] = {
            "name": "國際現貨黃金 XAU/USD",
            "symbol": "XAUUSD=X",
            "unit": "USD per troy ounce",
            "latest": gold_value,
            "latestDate": gold_asof,
            "source": "xaus.com/api/v1/history",
            "updatedAt": fetched_at,
        }
        entry["_reports"] = reports

    entries.sort(key=lambda e: e["date"])
    temp_data_file = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
    temp_data_file.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_data_file.replace(DATA_FILE)

    now = datetime.now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {today} 更新欄位: {updated_fields or '(無新資料)'}")
    for key, info in meta.items():
        expected = get_expected_asof(key, now)
        is_fresh = info["asof"] >= expected
        status = "🟢 目前最新" if is_fresh else f"⚪ 尚未更新（預期應有 {expected}）"
        print(f"  - {key}: 資料日期 {info['asof']} {status}")

    is_complete = len(updated_fields) == len(FIELDS)
    state = "success" if is_complete else "partial"
    if is_complete:
        message = "八項總經資料（含現貨黃金）更新完成，已寫入本機資料檔。"
    else:
        message = f"已更新 {len(updated_fields)}/{len(FIELDS)} 項；其餘來源暫時無可用資料。"

    write_update_status(
        state, run_id, startedAt=started_at, finishedAt=now_iso(), phase="complete",
        message=message, updatedFields=updated_fields, failedFields=failed_fields,
    )
    return 0 if is_complete else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        status = read_update_status()
        run_id = status.get("runId") or uuid.uuid4().hex
        started_at = status.get("startedAt") or now_iso()
        message = f"更新程式發生未預期錯誤: {error}"
        print(f"[錯誤] {message}", file=sys.stderr)
        try:
            write_update_status(
                "failed", run_id, startedAt=started_at, finishedAt=now_iso(), phase="failed",
                message="更新失敗，未載入舊資料。", error=message,
                updatedFields=status.get("updatedFields", []),
                failedFields=status.get("failedFields", list(FIELDS)),
            )
        except OSError:
            pass
        sys.exit(1)
