from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from app.config import settings


class KiteCandleAcquisition:
    def __init__(self, provider):
        self.provider = provider

    def acquire(self, symbol: str, interval: str, lookback_days: int, now: datetime) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        interval_map = {"daily": "day", "1H": "60minute", "15M": "15minute"}
        api_interval = interval_map[interval]
        end = now.astimezone(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
        start = end - timedelta(days=lookback_days)
        base = {
            "source": "Kite Connect",
            "requested_interval": interval,
            "requested_lookback": f"{lookback_days}d",
            "fallback_used": False,
        }
        if not self.provider:
            return [], {**base, "raw_count": 0, "normalized_count": 0, "actual_latest_candle_timestamp": None, "failure_reason": "Kite provider unavailable"}
        try:
            candles = self.provider.get_historical_candles(symbol, api_interval, start, end)
            filtered = []
            current_ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
            for candle in candles:
                stamp = candle.get("timestamp")
                if not stamp:
                    continue
                if isinstance(stamp, str):
                    stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                candle_ist = stamp.astimezone(timezone(timedelta(hours=5, minutes=30)))
                if interval == "daily":
                    complete = current_ist.date() > candle_ist.date() or (
                        current_ist.date() == candle_ist.date()
                        and current_ist.time() >= datetime.min.time().replace(hour=15, minute=30)
                    )
                elif interval == "1H":
                    complete = current_ist >= candle_ist + timedelta(hours=1)
                else:
                    complete = current_ist >= candle_ist + timedelta(minutes=15)
                if complete:
                    filtered.append(candle)
            normalized = [
                {**candle, "timestamp": candle["timestamp"].isoformat() if hasattr(candle["timestamp"], "isoformat") else str(candle["timestamp"])}
                for candle in filtered
            ]
            return normalized, {
                **base,
                "raw_count": len(candles),
                "normalized_count": len(normalized),
                "actual_latest_candle_timestamp": normalized[-1]["timestamp"] if normalized else None,
                "whether_current_candle_excluded": len(filtered) < len(candles),
                "failure_reason": None,
            }
        except Exception as exc:
            return [], {
                **base,
                "raw_count": 0,
                "normalized_count": 0,
                "actual_latest_candle_timestamp": None,
                "failure_reason": f"Kite candle acquisition failed: {exc}",
            }
