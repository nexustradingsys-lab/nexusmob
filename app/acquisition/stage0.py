from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.acquisition.kite import normalize_symbol
from app.acquisition.nse import NseRawClient
from app.config import settings
from app.security import safe_error

SECTOR_SYMBOLS = (
    "NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY IT", "NIFTY AUTO",
    "NIFTY PHARMA", "NIFTY METAL", "NIFTY FMCG", "NIFTY ENERGY", "NIFTY REALTY",
)
NSE_INDEX_NAMES = {"NIFTY FIN SERVICE": "NIFTY FINANCIAL SERVICES"}


def _find_index(records: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    target = str(name).upper().strip()
    for row in records:
        if str(row.get("index", "")).upper().strip() == target or str(row.get("indexSymbol", "")).upper().strip() == target:
            return row
    return None


def _source(data: Any, acquired_at: str, source_timestamp: Any = None) -> Dict[str, Any]:
    timestamp = source_timestamp
    if timestamp is None and isinstance(data, dict):
        timestamp = data.get("timestamp") or data.get("source_timestamp")
    status = "AVAILABLE" if _has_source_value(data) else "UNAVAILABLE"
    return {"status": status, "acquired_at": acquired_at, "source_timestamp": timestamp, "source_timestamp_status": "AVAILABLE" if timestamp is not None else "UNAVAILABLE", "data": data}


def _has_source_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_has_source_value(item) for item in value.values())
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


class Stage0Acquisition:
    def __init__(self, provider, nse_client: Optional[NseRawClient] = None):
        self.provider = provider
        self.nse = nse_client or NseRawClient(settings.request_timeout_seconds, settings.nse_retries)

    def acquire(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        acquired_at = now.isoformat()
        raw: Dict[str, Any] = {}
        source_errors = {}
        quotes = {}
        for key, symbol in (("nifty", "NIFTY 50"), ("banknifty", "NIFTY BANK"), ("finnifty", "NIFTY FIN SERVICE")):
            try:
                quotes[key] = self.provider.get_quote(symbol) if self.provider else None
            except Exception as exc:
                quotes[key] = None
                source_errors[f"{key}.quote"] = safe_error(exc)
        candles = {}
        for key, symbol in (("nifty", "NIFTY 50"), ("banknifty", "NIFTY BANK")):
            candles[key] = {}
            end = now.astimezone(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
            for field, interval, days in (("daily_candles", "day", 30), ("fifteen_minute_candles", "15minute", 7)):
                try:
                    candles[key][field] = self.provider.get_historical_candles(symbol, interval, end - timedelta(days=days), end) if self.provider else None
                except Exception as exc:
                    candles[key][field] = None
                    source_errors[f"{key}.{field}"] = safe_error(exc)

        try:
            nse_payload = self.nse.get_all_indices()
            rows = nse_payload.get("data", []) if isinstance(nse_payload, dict) else []
            nse_timestamp = nse_payload.get("timestamp") or nse_payload.get("lastUpdateTime")
        except Exception as exc:
            rows, nse_timestamp = [], None
            source_errors["nse.indices"] = safe_error(exc)

        def index_raw(label, lookup=None):
            row = _find_index(rows, lookup or label)
            return row

        for key, label in (("nifty", "NIFTY 50"), ("banknifty", "NIFTY BANK"), ("finnifty", "NIFTY FINANCIAL SERVICES")):
            raw[key] = index_raw(label)
            if raw[key] is None:
                source_errors[f"{key}.index_data"] = "NSE index record unavailable"
        vix = index_raw("INDIA VIX")
        vix_data = None if vix is None else {
            "current": vix.get("last"), "previous_close": vix.get("previousClose"), "open": vix.get("open"),
            "high": vix.get("high"), "low": vix.get("low"), "change": vix.get("variation"),
            "change_percent": vix.get("percentChange"), "timestamp": vix.get("timestamp") or nse_timestamp,
        }
        sector_rows = []
        for symbol in SECTOR_SYMBOLS:
            actual_name = NSE_INDEX_NAMES.get(symbol, symbol)
            item = index_raw(symbol, actual_name)
            if item is not None:
                sector_rows.append({
                    "symbol": symbol,
                    "ltp": item.get("last"),
                    "previous_close": item.get("previousClose"),
                    "change_percent": item.get("percentChange"),
                    "timestamp": item.get("timestamp") or nse_timestamp,
                    "source": "NSE_ALL_INDICES",
                })
        nifty_raw = _index_market_data(raw["nifty"], candles.get("nifty", {}))
        bank_raw = _index_market_data(raw["banknifty"], candles.get("banknifty", {}))
        finnifty_quote = quotes.get("finnifty") or {}
        finnifty_raw = {
            "ltp": finnifty_quote.get("last_price"),
            "previous_close": finnifty_quote.get("previous_close"),
            "open": finnifty_quote.get("open"),
            "high": finnifty_quote.get("high"),
            "low": finnifty_quote.get("low"),
            "timestamp": finnifty_quote.get("timestamp"),
        }
        vix_raw = vix_data
        breadth_raw = {
            "nifty": _select_breadth(raw["nifty"]),
            "banknifty": _select_breadth(raw["banknifty"]),
        }
        raw_inputs = {
            "nifty": _source(nifty_raw, acquired_at, _last_candle_time(nifty_raw.get("daily_candles"))),
            "banknifty": _source(bank_raw, acquired_at, _last_candle_time(bank_raw.get("daily_candles"))),
            "finnifty": _source(finnifty_raw, acquired_at),
            "india_vix": _source(vix_raw, acquired_at),
            "breadth": _source(breadth_raw, acquired_at, nse_timestamp),
            "sectors": _source({"observations": sector_rows}, acquired_at, nse_timestamp),
        }
        status = "SUCCESS" if all(source["status"] == "AVAILABLE" for source in raw_inputs.values()) else "ERROR"
        return {"schema_version": "mobile_raw_v2", "execution": {"execution_id": f"mobile_stage0_{now.strftime('%Y%m%dT%H%M%SZ')}", "request_id": f"mobile_stage0_request_{now.strftime('%Y%m%dT%H%M%SZ')}", "timestamp": acquired_at, "timezone": "Asia/Kolkata"}, "stage": 0, "status": status, "raw_inputs": raw_inputs, "audit": {"acquired_at": acquired_at, "source_errors": source_errors}}


def _last_candle_time(candles):
    if not isinstance(candles, list) or not candles:
        return None
    value = candles[-1].get("timestamp") if isinstance(candles[-1], dict) else None
    return value.isoformat() if hasattr(value, "isoformat") else value


def _select_breadth(row):
    if row is None:
        return None
    return {key: row.get(key) for key in ("advances", "declines", "unchanged", "timestamp")}


def _index_market_data(record, candles):
    return {
        "ltp": record.get("last") if record else None,
        "previous_close": record.get("previousClose") if record else None,
        "open": record.get("open") if record else None,
        "high": record.get("high") if record else None,
        "low": record.get("low") if record else None,
        "daily_candles": candles.get("daily_candles"),
        "fifteen_minute_candles": candles.get("fifteen_minute_candles"),
    }
