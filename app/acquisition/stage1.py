from datetime import datetime, timezone
from typing import Any, Dict, List

from app.config import settings
from app.acquisition.nse import NseRawClient


REQUIRED_FIELDS = {
    "oi_spurts_underlying": ["symbol", "latestOI", "prevOI", "changeInOI", "avgInOI", "underlyingValue"],
    "oi_spurts_contracts": ["changeInOI", "expiryDate", "instrument", "latestOI", "ltp", "optionType", "pChange", "prevClose", "prevOI", "strikePrice", "symbol", "underlyingValue", "volume", "identifier", "instrumentType", "pChangeInOI"],
    "most_active_stock_calls": ["identifier", "instrumentType", "instrument", "underlying", "expiryDate", "optionType", "strikePrice", "lastPrice", "numberOfContractsTraded", "totalTurnover", "premiumTurnover", "openInterest", "underlyingValue", "pChange"],
    "most_active_stock_puts": ["identifier", "instrumentType", "instrument", "underlying", "expiryDate", "optionType", "strikePrice", "lastPrice", "numberOfContractsTraded", "totalTurnover", "premiumTurnover", "openInterest", "underlyingValue", "pChange"],
    "most_active_contracts_by_oi": ["identifier", "instrumentType", "instrument", "underlying", "expiryDate", "optionType", "strikePrice", "lastPrice", "numberOfContractsTraded", "totalTurnover", "premiumTurnover", "openInterest", "underlyingValue", "pChange"],
    "most_active_contracts": ["identifier", "instrumentType", "instrument", "underlying", "expiryDate", "optionType", "strikePrice", "lastPrice", "numberOfContractsTraded", "totalTurnover", "premiumTurnover", "openInterest", "underlyingValue", "pChange"],
    "most_active_underlyings": ["symbol", "futVolume", "optVolume", "totVolume", "futTurnover", "optTurnover", "totTurnover", "preTurnover", "latestOI", "underlying"],
}


def _data_rows(name: str, payload: Dict[str, Any]) -> List[Any]:
    if name in {"most_active_stock_calls", "most_active_stock_puts"}:
        return (payload.get("OPTSTK") or {}).get("data", [])
    if name == "most_active_futures":
        return (payload.get("volume") or payload.get("value") or {}).get("data", [])
    if name in {"most_active_contracts_by_oi", "most_active_contracts"}:
        return _snapshot_rows(name, payload)
    return payload.get("data", [])


def _snapshot_rows(name: str, payload: Dict[str, Any]) -> List[Any]:
    for bucket_name in ("volume", "value"):
        if bucket_name in payload:
            bucket = payload[bucket_name]
            if not isinstance(bucket, dict) or not isinstance(bucket.get("data"), list):
                raise ValueError(f"{name}: {bucket_name}.data must be a list")
            return bucket["data"]
    rows = payload.get("data")
    if isinstance(rows, list):
        return rows
    raise ValueError(f"{name}: volume.data, value.data, or data must be a list")


def validate_dataset(name: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{name}: payload must be an object")
    if name == "oi_spurts_contracts":
        data = payload.get("data")
        if not isinstance(data, list):
            raise ValueError("oi_spurts_contracts: data must be a list")
        buckets = ("Rise-in-OI-Rise", "Rise-in-OI-Slide", "Slide-in-OI-Slide", "Slide-in-OI-Rise")
        for bucket_name in buckets:
            bucket = next((item[bucket_name] for item in data if isinstance(item, dict) and bucket_name in item), None)
            if bucket is not None:
                _validate_rows("oi_spurts_contracts", bucket, REQUIRED_FIELDS[name])
        return
    if name == "most_active_futures":
        for bucket_name in ("volume", "value"):
            bucket = payload.get(bucket_name)
            if not isinstance(bucket, dict) or not isinstance(bucket.get("data"), list):
                raise ValueError(f"most_active_futures: {bucket_name}.data must be a list")
            if any(not isinstance(row, dict) for row in bucket["data"]):
                raise ValueError(f"most_active_futures: {bucket_name} contains a non-object row")
        return
    if name in {"most_active_stock_calls", "most_active_stock_puts"}:
        rows = _data_rows(name, payload)
        if not isinstance(rows, list):
            raise ValueError(f"{name}: OPTSTK.data must be a list")
    elif name in {"most_active_contracts_by_oi", "most_active_contracts"}:
        rows = _snapshot_rows(name, payload)
    elif not isinstance(payload.get("data"), list):
        raise ValueError(f"{name}: data must be a list")
    else:
        rows = _data_rows(name, payload)
    _validate_rows(name, rows, REQUIRED_FIELDS.get(name, []))


def _validate_rows(name: str, rows: Any, required_fields: List[str]) -> None:
    if not isinstance(rows, list):
        raise ValueError(f"{name}: data rows must be a list")
    if not rows:
        return
    if not isinstance(rows[0], dict):
        raise ValueError(f"{name}: first data row must be an object")
    missing = [field for field in required_fields if field not in rows[0]]
    if missing:
        raise ValueError(f"{name}: first data row missing required fields {missing}")


def _source_timestamp(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("timestamp", "source_timestamp", "currTradingDate", "lastUpdateTime"):
            if payload.get(key) is not None:
                return payload[key]
        for child in payload.values():
            found = _source_timestamp(child)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _source_timestamp(item)
            if found is not None:
                return found
    return None


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _base_record(source: str, dataset: str, acquired_at: str, row: Dict[str, Any]) -> Dict[str, Any]:
    return {"source": source, "dataset": dataset, "retrieved_at": acquired_at, "raw_payload": row}


def _normalized_rows(payloads: Dict[str, Dict[str, Any]], acquired_at: str) -> Dict[str, List[Dict[str, Any]]]:
    normalized: Dict[str, List[Dict[str, Any]]] = {}
    underlying_source = "/api/live-analysis-oi-spurts-underlyings"
    contract_source = "/api/live-analysis-oi-spurts-contracts"
    active_sources = {
        "most_active_stock_calls": ("/api/snapshot-derivatives-equity?index=calls-stocks-vol", "MOST_ACTIVE_STOCK_CALLS"),
        "most_active_stock_puts": ("/api/snapshot-derivatives-equity?index=puts-stocks-vol", "MOST_ACTIVE_STOCK_PUTS"),
        "most_active_contracts_by_oi": ("/api/snapshot-derivatives-equity?index=oi", "MOST_ACTIVE_CONTRACTS_BY_OI"),
        "most_active_contracts": ("/api/snapshot-derivatives-equity?index=contracts&limit=50", "MOST_ACTIVE_CONTRACTS"),
    }

    normalized["oi_spurts_underlying"] = []
    for row in _data_rows("oi_spurts_underlying", payloads["oi_spurts_underlying"]):
        if isinstance(row, dict):
            normalized["oi_spurts_underlying"].append({
                **_base_record(underlying_source, "OI_SPURTS_UNDERLYING", acquired_at, row),
                "symbol": row.get("symbol", ""), "latest_oi": _number(row.get("latestOI")),
                "prev_oi": _number(row.get("prevOI")), "change_oi": _number(row.get("changeInOI")),
                "pct_change_oi": _number(row.get("avgInOI")), "underlying_value": _number(row.get("underlyingValue")),
            })

    bucket_specs = (
        ("Rise-in-OI-Rise", "long_build_up"),
        ("Rise-in-OI-Slide", "short_build_up"),
        ("Slide-in-OI-Slide", "long_unwinding"),
        ("Slide-in-OI-Rise", "short_covering"),
    )
    bucket_data = payloads["oi_spurts_contracts"].get("data", [])
    for bucket, filename in bucket_specs:
        rows = next((item[bucket] for item in bucket_data if isinstance(item, dict) and isinstance(item.get(bucket), list)), [])
        normalized[filename] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized[filename].append({
                **_base_record(contract_source, bucket, acquired_at, row),
                "symbol": row.get("symbol", ""), "underlying": row.get("symbol", ""),
                "instrumentType": row.get("instrumentType", row.get("instrument", "")),
                "expiryDate": row.get("expiryDate", ""), "optionType": row.get("optionType", ""),
                "strikePrice": _number(row.get("strikePrice")), "price": _number(row.get("ltp")),
                "price_change_pct": _number(row.get("pChange")), "oi": _number(row.get("latestOI")),
                "oi_change": _number(row.get("changeInOI")), "oi_change_pct": _number(row.get("pChangeInOI")),
                "volume": _number(row.get("volume")), "turnover": 0.0, "classification": bucket,
            })

    for name, (source, dataset) in active_sources.items():
        normalized[name] = []
        for row in _data_rows(name, payloads[name]):
            if not isinstance(row, dict):
                continue
            normalized[name].append({
                **_base_record(source, dataset, acquired_at, row),
                "symbol": row.get("identifier", row.get("instrument", "")),
                "underlying": row.get("underlying", ""), "instrumentType": row.get("instrumentType", ""),
                "optionType": row.get("optionType", ""), "strike": _number(row.get("strikePrice")),
                "expiry": row.get("expiryDate", ""), "price": _number(row.get("lastPrice")),
                "price_change_pct": _number(row.get("pChange")), "oi": _number(row.get("openInterest")),
                "oi_change_pct": _number(row.get("pChangeInOI") or row.get("pChangeOi")),
                "volume": _number(row.get("numberOfContractsTraded")),
                "turnover": _number(row.get("totalTurnover")), "classification": row.get("instrumentType", ""),
            })

    normalized["most_active_underlyings"] = []
    for row in _data_rows("most_active_underlyings", payloads["most_active_underlyings"]):
        if isinstance(row, dict):
            normalized["most_active_underlyings"].append({
                **_base_record("/api/live-analysis-most-active-underlying", "MOST_ACTIVE_UNDERLYINGS", acquired_at, row),
                "symbol": row.get("symbol", ""), "volume": _number(row.get("totVolume")),
                "turnover": _number(row.get("totTurnover")), "latest_oi": _number(row.get("latestOI")),
                "underlying": _number(row.get("underlying")),
                "call_volume": int(row.get("callVolume", 0) or row.get("calls", 0) or 0),
                "put_volume": int(row.get("putVolume", 0) or row.get("puts", 0) or 0),
                "put_call_ratio": _number(row.get("pcr") or row.get("putCallRatio")),
                "classification": row.get("classification", ""),
            })
    return normalized


def _write_json(path, value) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


class Stage1Acquisition:
    DATASET_NAMES = (
        "oi_spurts_underlying", "oi_spurts_contracts", "most_active_stock_calls",
        "most_active_stock_puts", "most_active_contracts_by_oi", "most_active_contracts",
        "most_active_futures", "most_active_underlyings",
    )

    def __init__(self, client: NseRawClient | None = None):
        self.client = client or NseRawClient(settings.request_timeout_seconds, settings.nse_retries)

    def acquire(self) -> Dict[str, Any]:
        acquired_at = datetime.now(timezone.utc).isoformat()
        raw = self.client.fetch_datasets()
        if set(raw) != set(self.DATASET_NAMES):
            raise RuntimeError("NSE acquisition returned an incomplete dataset set")
        for name in self.DATASET_NAMES:
            validate_dataset(name, raw[name])

        timestamp = datetime.now(timezone.utc)
        date_key = timestamp.strftime("%Y-%m-%d")
        time_key = timestamp.strftime("%H-%M-%S-%f")
        run_dir = settings.archive_path / date_key / time_key
        raw_dir, normalized_dir = run_dir / "raw", run_dir / "normalized"
        for name, payload in raw.items():
            _write_json(raw_dir / f"{name}.json", payload)
        normalized = _normalized_rows(raw, acquired_at)
        for name, rows in normalized.items():
            safe_name = name.replace(":", "_")
            _write_json(normalized_dir / f"{safe_name}.json", rows)
        manifest = {
            "run_id": f"{date_key}_{time_key}", "timestamp": acquired_at, "timezone": "UTC",
            "environment": "PROGRAMMATIC", "endpoints_queried": len(self.DATASET_NAMES), "status": "PASS",
            "oi_spurts_breakdown": {
                "LONG_BUILD_UP": len(normalized["long_build_up"]),
                "SHORT_BUILD_UP": len(normalized["short_build_up"]),
                "LONG_UNWINDING": len(normalized["long_unwinding"]),
                "SHORT_COVERING": len(normalized["short_covering"]),
            },
        }
        summary = {"logical_datasets": {
            "OI_SPURTS_UNDERLYING": len(normalized["oi_spurts_underlying"]),
            "LONG_BUILD_UP": len(normalized["long_build_up"]),
            "SHORT_BUILD_UP": len(normalized["short_build_up"]),
            "LONG_UNWINDING": len(normalized["long_unwinding"]),
            "SHORT_COVERING": len(normalized["short_covering"]),
            "MOST_ACTIVE_STOCK_CALLS": len(normalized["most_active_stock_calls"]),
            "MOST_ACTIVE_STOCK_PUTS": len(normalized["most_active_stock_puts"]),
            "MOST_ACTIVE_CONTRACTS_BY_OI": len(normalized["most_active_contracts_by_oi"]),
            "MOST_ACTIVE_CONTRACTS": len(normalized["most_active_contracts"]),
            "MOST_ACTIVE_UNDERLYINGS": len(normalized["most_active_underlyings"]),
        }}
        _write_json(run_dir / "manifest.json", manifest)
        _write_json(run_dir / "run_summary.json", summary)

        datasets = {}
        for name, payload in raw.items():
            datasets[name] = {
                "status": "AVAILABLE", "acquired_at": acquired_at, "source": "NSE",
                "source_timestamp": _source_timestamp(payload), "data": payload,
            }
        return {
            "schema_version": "mobile_raw_v2",
            "execution": {"execution_id": f"mobile_stage1_{timestamp.strftime('%Y%m%dT%H%M%SZ')}", "request_id": f"mobile_stage1_request_{timestamp.strftime('%Y%m%dT%H%M%SZ')}", "timestamp": acquired_at, "timezone": "Asia/Kolkata"},
            "source": {"kite": False, "nse": True}, "stage": 1, "status": "SUCCESS", "datasets": datasets,
            "audit": {
                "acquired_at": acquired_at,
                "source": "NSE website programmatic API",
                "notes": "Raw Stage 1 NSE datasets are preserved. Ranking, eligibility, confidence and Stage 1 calculations remain outside the mobile layer. No derived Stage 1 result is serialized.",
                "archive_id": manifest["run_id"],
            },
        }
