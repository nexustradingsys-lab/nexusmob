from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from kiteconnect import KiteConnect


def normalize_symbol(value: Any) -> str:
    return "".join(str(value or "").lower().replace("_", " ").split())


class ZerodhaMarketDataProvider:
    """Minimal Kite acquisition provider with exact tradingsymbol resolution."""

    def __init__(self, client: KiteConnect):
        self.client = client
        self.instrument_master: List[Dict[str, Any]] = []

    def refresh_instruments(self) -> List[Dict[str, Any]]:
        self.instrument_master = self.client.instruments() or []
        return self.instrument_master

    def resolve_instrument(self, symbol: str) -> Optional[Dict[str, Any]]:
        expected = normalize_symbol(symbol)
        if not expected:
            return None
        matches = [
            instrument
            for instrument in self.instrument_master
            if normalize_symbol(instrument.get("tradingsymbol")) == expected
        ]
        for instrument in matches:
            if (
                instrument.get("exchange") == "NSE"
                and instrument.get("segment") == "NSE"
                and instrument.get("instrument_type") == "EQ"
            ):
                return instrument
        if any(
            instrument.get("instrument_type") == "EQ"
            and instrument.get("segment") in {"NSE", "BSE"}
            for instrument in matches
        ):
            return None
        return matches[0] if matches else None

    def _require_instrument(self, symbol: str) -> Dict[str, Any]:
        instrument = self.resolve_instrument(symbol)
        if not instrument:
            raise LookupError(f"Exact instrument not found: {symbol}")
        return instrument

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        instrument = self._require_instrument(symbol)
        key = f"{instrument.get('exchange') or 'NSE'}:{instrument['tradingsymbol']}"
        payload = self.client.ltp(key).get(key, {})
        previous_close = instrument.get("previous_close")
        if previous_close is None:
            try:
                end = datetime.now(timezone.utc)
                candles = self.client.historical_data(
                    int(instrument["instrument_token"]), end - timedelta(days=7), end, "day"
                )
                previous_close = next((row.get("close") for row in reversed(candles or []) if row.get("close") is not None), None)
            except Exception:
                previous_close = None
        return {
            "symbol": symbol,
            "instrument_symbol": instrument.get("instrument_name") or instrument.get("tradingsymbol") or instrument.get("name"),
            "last_price": payload.get("last_price"),
            "previous_close": previous_close,
            "open": (instrument.get("ohlc") or {}).get("open"),
            "high": (instrument.get("ohlc") or {}).get("high"),
            "low": (instrument.get("ohlc") or {}).get("low"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "Kite Connect",
            "data_freshness_status": "FRESH",
        }

    def get_ohlc(self, symbol: str) -> Dict[str, Any]:
        instrument = self._require_instrument(symbol)
        key = f"{instrument.get('exchange') or 'NSE'}:{instrument['tradingsymbol']}"
        response = self.client.ohlc(key).get(key, {})
        ohlc = response.get("ohlc") or {}
        return {
            "symbol": symbol,
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "close": ohlc.get("close"),
            "previous_close": ohlc.get("close"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "Kite Connect",
            "data_freshness_status": "FRESH",
        }

    def get_historical_candles(self, symbol: str, interval: str, from_dt: datetime, to_dt: datetime) -> List[Dict[str, Any]]:
        instrument = self._require_instrument(symbol)
        payload = self.client.historical_data(
            int(instrument["instrument_token"]), from_dt, to_dt, interval
        )
        return [
            {
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
                "timestamp": item.get("date"),
            }
            for item in payload or []
        ]
