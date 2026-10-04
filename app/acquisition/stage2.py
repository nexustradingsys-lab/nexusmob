import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.acquisition.candles import KiteCandleAcquisition
from app.acquisition.kite import normalize_symbol
from app.acquisition.nse import NseRawClient
from app.config import settings
from app.security import safe_error

BASE = Path(__file__).resolve().parents[1] / "config"
MAPPINGS = json.loads((BASE / "index_mapping.json").read_text(encoding="utf-8"))["mappings"]
INDEX_ALIASES = json.loads((BASE / "index_aliases.json").read_text(encoding="utf-8"))
EXPIRIES = [item.strip() for item in __import__("os").getenv("STAGE2_EXPIRIES", "29-Sep-2026,27-Oct-2026,23-Nov-2026").split(",") if item.strip()]


def exact_nse_equity(instrument: Any, symbol: str) -> bool:
    return isinstance(instrument, dict) and normalize_symbol(instrument.get("tradingsymbol")) == normalize_symbol(symbol) and instrument.get("exchange") == "NSE" and instrument.get("segment") == "NSE" and instrument.get("instrument_type") == "EQ"


def _expiry(today):
    for expiry in EXPIRIES:
        try:
            value = datetime.strptime(expiry, "%d-%b-%Y").date()
            if value >= today:
                return expiry
        except ValueError:
            continue
    return EXPIRIES[0]


def _normalize_expiry(value: str):
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None


def _option_chain(client: NseRawClient, symbol: str, expiry: str):
    path = f"/api/option-chain-v3?type=Equity&symbol={urllib.parse.quote(symbol)}&expiry={urllib.parse.quote(expiry)}"
    client.session.headers["Referer"] = f"{client.BASE_URL}/option-chain"
    raw = client.fetch_endpoint(path)
    records = raw.get("records", {})
    data = records.get("data", []) or raw.get("filtered", {}).get("data", [])
    contracts = []
    for item in data:
        item_expiry = item.get("expiryDate") or (item.get("CE") or item.get("PE") or {}).get("expiryDate", "")
        item_date = _normalize_expiry(item_expiry)
        target_date = _normalize_expiry(expiry)
        if str(item_expiry).strip().upper() != str(expiry).strip().upper() and (
            item_date is None or target_date is None or item_date != target_date
        ):
            continue
        strike = float(item.get("strikePrice") or 0.0)
        for option_type in ("CE", "PE"):
            option = item.get(option_type)
            if not option:
                continue
            contracts.append({
                "strike": strike,
                "option_type": option_type,
                "last_price": float(option.get("lastPrice") or 0.0),
                "open_interest": float(option.get("openInterest") or 0.0),
                "change_in_oi": float(option.get("changeinOpenInterest") or 0.0),
                "volume": float(option.get("totalTradedVolume") or 0.0),
                "ltp_change_percent": float(option.get("pChange") or 0.0),
            })
    return raw, contracts, records.get("timestamp")


def _index_payload(logical: str, provider, candles: KiteCandleAcquisition, now: datetime, acquired_at: str):
    requested = INDEX_ALIASES.get(logical, logical)
    base = {
        "logical_index": logical, "instrument_symbol": requested, "status": "UNAVAILABLE", "instrument": {},
        "market": {"symbol": logical, "source": "Kite Connect", "quote": {}, "ohlc": {}},
        "daily": {"interval": "day", "candles": [], "audit": {}},
        "hourly": {"interval": "60minute", "candles": [], "audit": {}},
        "fifteen_minute": {"interval": "15minute", "candles": [], "audit": {}},
        "timestamps": {"acquired_at": acquired_at, "source": None}, "reason": None,
    }
    try:
        instrument = provider.resolve_instrument(requested) if provider else None
    except Exception as exc:
        base.update(status="ERROR", reason=f"INDEX_INSTRUMENT_RESOLUTION_ERROR: {safe_error(exc)}")
        return base
    if not (isinstance(instrument, dict) and normalize_symbol(instrument.get("tradingsymbol")) == normalize_symbol(requested) and instrument.get("exchange") == "NSE" and instrument.get("segment") == "INDICES" and instrument.get("instrument_type") == "EQ"):
        base["reason"] = "INDEX_INSTRUMENT_NOT_RESOLVED"
        return base
    base["instrument"] = instrument
    errors = []
    for key, method in (("quote", "get_quote"), ("ohlc", "get_ohlc")):
        try:
            base["market"][key] = getattr(provider, method)(requested)
        except Exception as exc:
            errors.append(f"{method}: {safe_error(exc)}")
    base["timestamps"]["source"] = (base["market"]["quote"] or {}).get("timestamp")
    for key, interval, days, label in (("daily", "daily", 100, "day"), ("hourly", "1H", 15, "60minute"), ("fifteen_minute", "15M", 5, "15minute")):
        values, audit = candles.acquire(requested, interval, days, now)
        base[key] = {"interval": label, "candles": values, "audit": audit}
        if audit.get("failure_reason"):
            errors.append(audit["failure_reason"])
        if audit.get("actual_latest_candle_timestamp"):
            base["timestamps"][f"{key}_latest"] = audit["actual_latest_candle_timestamp"]
    base["status"] = "ERROR" if errors else "AVAILABLE"
    base["reason"] = "; ".join(errors) if errors else None
    return base


class Stage2Acquisition:
    def __init__(self, provider, nse_client: Optional[NseRawClient] = None):
        self.provider = provider
        self.candles = KiteCandleAcquisition(provider)
        self.nse = nse_client or NseRawClient(settings.request_timeout_seconds, settings.nse_retries)

    def acquire(self, symbols: List[str]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        acquired_at = now.isoformat()
        expiry = _expiry(now.astimezone(timezone(timedelta(hours=5, minutes=30))).date())
        result = []
        for symbol in symbols:
            segment = {"mapping_status": "MAPPED" if symbol in MAPPINGS else "UNMAPPED", "logical_index": MAPPINGS.get(symbol), "context_ref": MAPPINGS.get(symbol)}
            try:
                instrument = self.provider.resolve_instrument(symbol) if self.provider else None
            except Exception as exc:
                instrument = None
                resolution_error = safe_error(exc)
            else:
                resolution_error = None
            if not exact_nse_equity(instrument, symbol):
                failure = resolution_error or "EXACT_NSE_EQUITY_INSTRUMENT_NOT_RESOLVED"
                result.append({"symbol": symbol, "status": "FAILED", "failure_category": "KITE_INSTRUMENT_RESOLUTION", "reason": failure, "acquisition_status": "FAILED", "timestamp": acquired_at, "instrument": instrument or {}, "market": {"symbol": symbol, "source": "Kite Connect", "quote": {}, "ohlc": {}}, "daily": {"interval": "day", "candles": [], "audit": {"failure_reason": failure}}, "hourly": {"interval": "60minute", "candles": [], "audit": {"failure_reason": failure}}, "fifteen_minute": {"interval": "15minute", "candles": [], "audit": {"failure_reason": failure}}, "option_chain": {"expiry": expiry, "available_expiries": EXPIRIES, "selected_expiry": expiry, "data": [], "source_timestamp": None, "audit": {"failure_reason": failure}}, "segment_context": segment, "audit": {"acquired_at": acquired_at, "source_timestamps": []}})
                continue
            market = {"symbol": symbol, "source": "Kite Connect", "quote": {}, "ohlc": {}}
            errors = []
            for key, method in (("quote", "get_quote"), ("ohlc", "get_ohlc")):
                try:
                    market[key] = getattr(self.provider, method)(symbol)
                except Exception as exc:
                    errors.append(f"{method}: {safe_error(exc)}")
            candles_data = {}
            for key, interval, lookback, label in (("daily", "daily", 100, "day"), ("hourly", "1H", 15, "60minute"), ("fifteen_minute", "15M", 5, "15minute")):
                values, audit = self.candles.acquire(symbol, interval, lookback, now)
                candles_data[key] = {"interval": label, "candles": values, "audit": audit}
                if audit.get("failure_reason"):
                    errors.append(audit["failure_reason"])
            try:
                _, option_data, option_timestamp = _option_chain(self.nse, symbol, expiry)
                option_audit = {"source": "NSE India", "raw_count": len(option_data), "normalized_count": len(option_data), "actual_latest_timestamp": option_timestamp, "expiry_used": expiry, "failure_reason": None}
            except Exception as exc:
                option_data, option_timestamp = [], None
                option_audit = {"source": "NSE India", "raw_count": 0, "normalized_count": 0, "actual_latest_timestamp": None, "expiry_used": expiry, "failure_reason": f"NSE option chain acquisition failed: {safe_error(exc)}"}
                errors.append(option_audit["failure_reason"])
            source_timestamps = [candles_data[key]["audit"].get("actual_latest_candle_timestamp") for key in ("daily", "hourly", "fifteen_minute")] + [option_timestamp]
            row = {"symbol": symbol, "status": "FAILED" if errors else "SUCCESS", "instrument": instrument, "market": market, **candles_data, "option_chain": {"expiry": expiry, "available_expiries": EXPIRIES, "selected_expiry": expiry, "data": option_data, "source_timestamp": option_timestamp, "audit": option_audit}, "segment_context": segment, "audit": {"acquired_at": acquired_at, "source_timestamps": source_timestamps}}
            if errors:
                row.update(failure_category="ACQUISITION", reason="; ".join(errors), acquisition_status="FAILED", timestamp=acquired_at)
            result.append(row)

        nifty = _index_payload("NIFTY 50", self.provider, self.candles, now, acquired_at)
        required = sorted({MAPPINGS[symbol] for symbol in symbols if symbol in MAPPINGS and MAPPINGS[symbol] != "NIFTY 50"})
        segment_indices = {logical: _index_payload(logical, self.provider, self.candles, now, acquired_at) for logical in required}
        return {"schema_version": "mobile_raw_v2", "execution": {"execution_id": f"mobile_stage2_{now.strftime('%Y%m%dT%H%M%SZ')}", "request_id": f"mobile_stage2_request_{now.strftime('%Y%m%dT%H%M%SZ')}", "timestamp": acquired_at, "timezone": "Asia/Kolkata"}, "source": {"kite": bool(self.provider), "nse": True}, "stage": 2, "symbols_requested": symbols, "symbols": result, "market_context": {"nifty": nifty, "segment_indices": segment_indices}}
