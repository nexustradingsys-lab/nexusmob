import json
import hashlib
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.acquisition.kite import ZerodhaMarketDataProvider
from app.acquisition.stage0 import Stage0Acquisition
from app.acquisition.stage1 import REQUIRED_FIELDS, Stage1Acquisition, _data_rows, validate_dataset
from app.acquisition.stage2 import INDEX_ALIASES, MAPPINGS, Stage2Acquisition, exact_nse_equity
from app.acquisition.nse import NseRawClient
from app.auth import zerodha
from app import main as main_module
from app.config import Settings
from app.security import safe_error
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def instrument(symbol, exchange="NSE", segment="NSE", instrument_type="EQ", token=1):
    return {
        "tradingsymbol": symbol,
        "name": symbol,
        "exchange": exchange,
        "segment": segment,
        "instrument_type": instrument_type,
        "instrument_token": token,
    }


def test_exact_equity_resolution_rejects_unrelated_bse_candidates():
    master = [
        instrument("OAL", "BSE", "BSE", token=11),
        instrument("ABB", "BSE", "BSE", token=12),
        instrument("LT", "BSE", "BSE", token=13),
    ]
    requested = ("COALINDIA", "ABBOTINDIA", "BOSCHLTD", "VOLTAS", "CEATLTD")
    master.extend(instrument(symbol, "NSE", "NSE", token=100 + index) for index, symbol in enumerate(requested))
    provider = ZerodhaMarketDataProvider(SimpleNamespace())
    provider.instrument_master = master

    for symbol in requested:
        resolved = provider.resolve_instrument(symbol)
        assert exact_nse_equity(resolved, symbol)
        assert resolved["tradingsymbol"] == symbol
        assert resolved["exchange"] == "NSE"
    assert provider.resolve_instrument("COALINDIA")["tradingsymbol"] != "OAL"
    assert provider.resolve_instrument("ABBOTINDIA")["tradingsymbol"] != "ABB"
    assert provider.resolve_instrument("VOLTAS")["tradingsymbol"] != "LT"


def test_exact_resolver_does_not_accept_bse_only_as_nse_substitute():
    provider = ZerodhaMarketDataProvider(SimpleNamespace())
    provider.instrument_master = [instrument("ONLYBSE", "BSE", "BSE")]
    assert provider.resolve_instrument("ONLYBSE") is None
    assert provider.resolve_instrument("NOTLISTED") is None


def test_mapping_has_expected_counts_and_critical_entries():
    assert len(MAPPINGS) == 181
    assert len(set(MAPPINGS.values())) == 19
    digest = hashlib.sha256(json.dumps(MAPPINGS, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == "4d4c14f7209f7faea7484a4e66325eef0951bdeccb2a4ff985bdff74a0b6385c"
    assert MAPPINGS["COALINDIA"] == "NIFTY METAL"
    assert MAPPINGS["HDFCBANK"] == "BANKNIFTY"
    assert MAPPINGS["INFY"] == "NIFTY IT"
    assert MAPPINGS["MARUTI"] == "NIFTY AUTO"
    assert MAPPINGS["RELIANCE"] == "NIFTY ENERGY"
    assert MAPPINGS["TCS"] == "NIFTY IT"
    assert MAPPINGS["WIPRO"] == "NIFTY IT"
    assert "BSE" not in MAPPINGS


def test_index_aliases_are_explicit_and_exact():
    assert INDEX_ALIASES == {
        "BANKNIFTY": "NIFTY BANK",
        "FINNIFTY": "NIFTY FIN SERVICE",
        "NIFTY CONSUMER DURABLES": "NIFTY CONSUMPTION",
        "NIFTY INFRASTRUCTURE": "NIFTY INFRA",
        "NIFTY MIDCAP": "NIFTY MIDCAP 50",
        "NIFTY SERVICES": "NIFTY SERV SECTOR",
    }


class FakeNseClient:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.session = SimpleNamespace(headers={})

    def fetch_endpoint(self, path):
        return self.payload

    def get_all_indices(self):
        return self.payload

    def fetch_datasets(self):
        return self.payload


class FakeProvider:
    def __init__(self, instruments):
        self.instrument_master = instruments
        self.quote_symbols = []
        self.ohlc_symbols = []
        self.history_symbols = []

    def resolve_instrument(self, symbol):
        expected = symbol.replace(" ", "").lower()
        for item in self.instrument_master:
            if item["tradingsymbol"].replace(" ", "").lower() == expected:
                if item.get("instrument_type") == "EQ" and item.get("segment") in {"NSE", "BSE"}:
                    return item if item.get("exchange") == "NSE" and item.get("segment") == "NSE" else None
                return item
        return None

    def get_quote(self, symbol):
        self.quote_symbols.append(symbol)
        return {"symbol": symbol, "instrument_symbol": symbol, "last_price": 0, "previous_close": 0, "timestamp": "2026-10-03T10:00:00+00:00", "source": "Kite Connect"}

    def get_ohlc(self, symbol):
        self.ohlc_symbols.append(symbol)
        return {"symbol": symbol, "open": 0, "high": 0, "low": 0, "close": 0, "timestamp": "2026-10-03T10:00:00+00:00"}

    def get_historical_candles(self, symbol, interval, from_dt, to_dt):
        self.history_symbols.append((symbol, interval))
        return []


class FakeOptionClient(FakeNseClient):
    BASE_URL = "https://www.nseindia.com"

    def fetch_endpoint(self, path):
        return {"records": {"timestamp": "2026-10-03T10:00:00Z", "data": []}, "filtered": {"data": []}}


class FakeRawDatasets(FakeNseClient):
    def __init__(self):
        empty_contracts = {"data": []}
        self.payload = {
            "oi_spurts_underlying": {"data": []},
            "oi_spurts_contracts": {"data": []},
            "most_active_stock_calls": {"OPTSTK": {"data": []}, "currTradingDate": "03-Oct-2026"},
            "most_active_stock_puts": {"OPTSTK": {"data": []}, "currTradingDate": "03-Oct-2026"},
            "most_active_contracts_by_oi": empty_contracts,
            "most_active_contracts": empty_contracts,
            "most_active_futures": {"volume": {"data": []}, "value": {"data": []}},
            "most_active_underlyings": {"data": []},
        }
        self.session = SimpleNamespace(headers={})


def test_stage0_raw_is_raw_only_and_preserves_zero_values():
    records = []
    for name in ("NIFTY 50", "NIFTY BANK", "NIFTY FINANCIAL SERVICES", "INDIA VIX", "NIFTY IT", "NIFTY AUTO", "NIFTY PHARMA", "NIFTY METAL", "NIFTY FMCG", "NIFTY ENERGY", "NIFTY REALTY"):
        records.append({"index": name, "indexSymbol": name, "last": 0, "previousClose": 0, "open": 0, "high": 0, "low": 0, "advances": 0, "declines": 0, "unchanged": 0, "percentChange": 0, "timestamp": "2026-10-03T10:00:00Z"})
    provider = FakeProvider([instrument("NIFTY 50", segment="INDICES"), instrument("NIFTY BANK", segment="INDICES"), instrument("NIFTY FIN SERVICE", segment="INDICES")])
    result = Stage0Acquisition(provider, FakeNseClient({"data": records, "timestamp": "2026-10-03T10:00:00Z"})).acquire()

    assert result["schema_version"] == "mobile_raw_v2"
    assert set(result["raw_inputs"]) == {"nifty", "banknifty", "finnifty", "india_vix", "breadth", "sectors"}
    assert result["raw_inputs"]["nifty"]["data"]["ltp"] == 0
    assert result["raw_inputs"]["nifty"]["data"]["daily_candles"] == []
    assert result["raw_inputs"]["breadth"]["data"]["nifty"]["advances"] == 0
    for forbidden in ("stage0_result", "derived_result", "marketScore", "buyer_score", "seller_score", "marketType", "decision", "analysis"):
        assert forbidden not in json.dumps(result)


def test_stage1_valid_empty_data_is_available_and_archived(tmp_path, monkeypatch):
    import app.acquisition.stage1 as stage1_module
    monkeypatch.setattr(stage1_module, "settings", SimpleNamespace(archive_path=tmp_path))
    result = Stage1Acquisition(FakeRawDatasets()).acquire()
    assert result["schema_version"] == "mobile_raw_v2"
    assert result["stage"] == 1
    assert result["status"] == "SUCCESS"
    assert result["source"] == {"kite": False, "nse": True}
    assert set(result["datasets"]) == set(Stage1Acquisition.DATASET_NAMES)
    assert all(dataset["status"] == "AVAILABLE" for dataset in result["datasets"].values())
    assert all(dataset["data"] is not None for dataset in result["datasets"].values())
    assert all({"status", "acquired_at", "source", "source_timestamp", "data"} == set(dataset) for dataset in result["datasets"].values())
    assert isinstance(result["audit"], dict)
    assert result["audit"]["notes"] == (
        "Raw Stage 1 NSE datasets are preserved. Ranking, eligibility, confidence "
        "and Stage 1 calculations remain outside the mobile layer. No derived "
        "Stage 1 result is serialized."
    )
    assert isinstance(result["audit"]["notes"], str)
    assert result["audit"]["archive_id"]
    assert set(result["execution"]) == {"execution_id", "request_id", "timestamp", "timezone"}
    run_dirs = list(tmp_path.glob("*/*"))
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "manifest.json").exists()
    assert (run_dirs[0] / "run_summary.json").exists()
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["endpoints_queried"] == 8
    assert set(manifest["oi_spurts_breakdown"]) == {
        "LONG_BUILD_UP", "SHORT_BUILD_UP", "LONG_UNWINDING", "SHORT_COVERING"
    }
    assert len(list((run_dirs[0] / "raw").glob("*.json"))) == 8
    assert list((run_dirs[0] / "normalized").glob("*.json"))
    raw_contract = json.loads(json.dumps(result))
    raw_contract["audit"].pop("notes")
    for forbidden in ("Top5", "ranking_score", "confidence", "eligibility", "direction", "derived_result", "analysis"):
        assert forbidden not in json.dumps(raw_contract)


def test_stage1_validation_failure_is_not_reported_as_success():
    client = FakeRawDatasets()
    client.payload["oi_spurts_underlying"] = {"unexpected": []}
    acquisition = Stage1Acquisition(client)
    try:
        acquisition.acquire()
    except ValueError as exc:
        assert "oi_spurts_underlying" in str(exc)
    else:
        raise AssertionError("invalid NSE payload should fail acquisition")


def test_stage1_validates_original_nested_snapshot_payloads_strictly():
    rows = [{field: None for field in REQUIRED_FIELDS["most_active_contracts_by_oi"]}]
    payload = {"volume": {"data": rows}, "value": {"data": rows}}

    validate_dataset("most_active_contracts_by_oi", payload)
    assert _data_rows("most_active_contracts_by_oi", payload) is rows

    for malformed in ({}, {"volume": {}}, {"volume": {"data": {}}}):
        try:
            validate_dataset("most_active_contracts_by_oi", malformed)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed nested snapshot data must fail validation")


def test_stage1_transport_failure_is_not_reported_as_success():
    class FailedClient:
        def fetch_datasets(self):
            raise TimeoutError("NSE request timed out")

    try:
        Stage1Acquisition(FailedClient()).acquire()
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("NSE transport failure should propagate")


def test_stage2_mobile_raw_identity_mapping_nifty_and_unmapped():
    stocks = ["COALINDIA", "HDFCBANK", "INFY", "BSE", "ABBOTINDIA", "BOSCHLTD", "VOLTAS", "CEATLTD"]
    index_symbols = {"NIFTY 50", "NIFTY METAL", "NIFTY BANK", "NIFTY IT"}
    provider = FakeProvider(
        [instrument(symbol, token=index + 1) for index, symbol in enumerate(stocks)]
        + [instrument(symbol, segment="INDICES", token=500 + index) for index, symbol in enumerate(index_symbols)]
    )
    result = Stage2Acquisition(provider, FakeOptionClient()).acquire(["COALINDIA", "HDFCBANK", "INFY", "BSE"])
    rows = {row["symbol"]: row for row in result["symbols"]}
    assert result["schema_version"] == "mobile_raw_v2"
    assert result["market_context"]["nifty"]["status"] == "AVAILABLE"
    assert rows["COALINDIA"]["instrument"]["tradingsymbol"] == "COALINDIA"
    assert rows["COALINDIA"]["instrument"]["exchange"] == "NSE"
    assert rows["COALINDIA"]["market"]["quote"]["instrument_symbol"] != "OAL"
    assert rows["COALINDIA"]["segment_context"]["logical_index"] == "NIFTY METAL"
    assert rows["HDFCBANK"]["segment_context"]["logical_index"] == "BANKNIFTY"
    assert rows["INFY"]["segment_context"]["logical_index"] == "NIFTY IT"
    assert rows["BSE"]["segment_context"]["mapping_status"] == "UNMAPPED"
    assert result["market_context"]["segment_indices"]["BANKNIFTY"]["status"] == "AVAILABLE"
    assert provider.quote_symbols.count("NIFTY BANK") == 1
    assert "OAL" not in provider.quote_symbols


def test_stage2_refuses_bse_instead_of_substituting():
    provider = FakeProvider([instrument("ONLYBSE", "BSE", "BSE")])
    result = Stage2Acquisition(provider, FakeOptionClient()).acquire(["ONLYBSE"])
    assert result["symbols"][0]["status"] == "FAILED"
    assert result["symbols"][0]["failure_category"] == "KITE_INSTRUMENT_RESOLUTION"
    assert provider.quote_symbols == []
    assert provider.history_symbols == []


def test_stage2_keeps_stock_when_mapped_index_is_unavailable():
    provider = FakeProvider([instrument("HDFCBANK")])
    result = Stage2Acquisition(provider, FakeOptionClient()).acquire(["HDFCBANK"])
    candidate = result["symbols"][0]
    assert candidate["status"] == "SUCCESS", candidate.get("reason")
    assert candidate["segment_context"]["logical_index"] == "BANKNIFTY"
    assert result["market_context"]["segment_indices"]["BANKNIFTY"]["status"] == "UNAVAILABLE"
    assert result["market_context"]["nifty"]["status"] == "UNAVAILABLE"
    forbidden = {"ema", "sma", "vwap", "support", "resistance", "ranking", "confidence", "recommendation", "decision", "derived_result", "technical_score", "stage2_result"}

    def collect_keys(value):
        if isinstance(value, dict):
            return {str(key).lower() for key in value} | set().union(*(collect_keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(collect_keys(item) for item in value)) if value else set()
        return set()

    assert not (forbidden & collect_keys(result))


def test_mobile_route_inventory_and_contracts(monkeypatch, tmp_path):
    from app.routes import mobile
    class NoAuthSession:
        def __init__(self, config):
            pass
        def initialize(self):
            return None
        def status(self):
            return {"action_required": "LOGIN"}

    monkeypatch.setattr(main_module, "KiteAuthSession", NoAuthSession)
    monkeypatch.setattr(mobile, "Stage0Acquisition", lambda provider: SimpleNamespace(acquire=lambda: {"schema_version": "mobile_raw_v2", "stage": 0, "raw_inputs": {}}))
    monkeypatch.setattr(mobile, "Stage1Acquisition", lambda: SimpleNamespace(acquire=lambda: {"schema_version": "mobile_raw_v2", "stage": 1, "datasets": {}}))
    monkeypatch.setattr(mobile, "Stage2Acquisition", lambda provider: SimpleNamespace(acquire=lambda symbols: {"schema_version": "mobile_raw_v2", "stage": 2, "symbols_requested": symbols, "symbols": [], "market_context": {"nifty": {}, "segment_indices": {}}}))
    with TestClient(app) as client:
        paths = {route.path for route in app.routes if hasattr(route, "methods")}
        assert paths == {"/mobile", "/mobile/stage0/raw", "/mobile/stage1/raw", "/mobile/stage2/raw", "/auth/zerodha/login", "/api/callback", "/auth/zerodha/status", "/auth/zerodha/logout"}
        assert client.get("/mobile").status_code == 200
        assert client.post("/mobile/stage0/raw").json()["schema_version"] == "mobile_raw_v2"
        assert client.post("/mobile/stage1/raw").json()["schema_version"] == "mobile_raw_v2"
        response = client.post("/mobile/stage2/raw", json={"symbols": "COALINDIA,HDFCBANK"})
        assert response.status_code == 200
        assert response.json()["symbols_requested"] == ["COALINDIA", "HDFCBANK"]
        assert response.headers["X-Acquisition-Status"] == "ERROR"


def test_auth_adapter_injects_configured_token(monkeypatch, tmp_path):
    captured = {}

    class FakeKite:
        def __init__(self, api_key, timeout=None):
            captured["api_key"] = api_key
            captured["timeout"] = timeout
        def set_access_token(self, token):
            captured["token"] = token

    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    api_key, api_secret, token = object(), object(), object()
    config = Settings(api_key, api_secret, token, Path("archive"), "INFO", "127.0.0.1", 8000, 5, 1, tmp_path / "session.json")
    client = zerodha.create_kite_client(config)
    assert isinstance(client, FakeKite)
    assert captured["api_key"] is api_key
    assert captured["token"] is token
    assert captured["timeout"] == 5
    assert api_secret not in client.__dict__.values()


def test_safe_error_redacts_injected_secrets(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "settings", SimpleNamespace(kite_api_key="key-value", kite_api_secret="secret-value", kite_access_token="token-value"))
    message = safe_error(RuntimeError("failed key-value secret-value token-value"))
    assert message == "failed [REDACTED] [REDACTED] [REDACTED]"


def test_settings_read_required_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("KITE_API_KEY", "test-key")
    monkeypatch.setenv("KITE_API_SECRET", "test-secret")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("STAGE1_ARCHIVE_PATH", str(tmp_path))
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "17")
    config = Settings.from_env()
    assert config.kite_configured
    assert config.archive_path == tmp_path.resolve()
    assert config.request_timeout_seconds == 17


def test_settings_read_secret_from_docker_secrets(monkeypatch, tmp_path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "kite_api_key").write_text("file-api-key\n", encoding="utf-8")
    (secrets_dir / "kite_api_secret").write_text("file-api-secret\n", encoding="utf-8")

    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.setenv("SECRETS_DIR", str(secrets_dir))

    config = Settings.from_env()
    assert config.kite_api_key == "file-api-key"
    assert config.kite_api_secret == "file-api-secret"
    assert config.kite_credentials_configured is True


def test_nse_client_retries_and_returns_raw_payload():
    class Response:
        def __init__(self, status, payload=None):
            self.status_code = status
            self.payload = payload or {"data": []}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.headers = {}
            self.cookies = SimpleNamespace(clear=lambda: None)
            self.responses = [Response(403), Response(200), Response(200, {"data": [{"value": 0}]})]
        def get(self, url, timeout):
            return self.responses.pop(0)

    session = Session()
    client = NseRawClient(timeout_seconds=1, retries=2, session=session)
    client.cookies_initialized = True
    with patch("app.acquisition.nse.time.sleep"):
        result = client.fetch_endpoint("/test")
    assert result == {"data": [{"value": 0}]}
    assert not session.responses


def test_nse_client_matches_original_request_headers_and_stage0_retries(monkeypatch):
    client = NseRawClient(timeout_seconds=10, retries=2, session=FakeNseClient().session)
    assert client.session.headers["User-Agent"] == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    assert client.session.headers["Accept"] == (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8"
    )
    assert client.session.headers["Accept-Encoding"] == "gzip, deflate"
    assert client.session.headers["Connection"] == "keep-alive"

    calls = []
    monkeypatch.setattr(client, "fetch_endpoint", lambda path, retries=None: calls.append((path, retries)) or {})
    client.get_all_indices()
    assert calls == [("/api/allIndices", 3)]


def test_import_isolation_runs_without_nexus_on_pythonpath():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    code = (
        "import app.main, pathlib, sys; "
        "root=pathlib.Path.cwd().resolve(); "
        "paths=[pathlib.Path(m.__file__).resolve() for n,m in sys.modules.items() if n.startswith('app.') and getattr(m,'__file__',None)]; "
        "assert paths and all(root in p.parents for p in paths); "
        "assert not any(n.startswith(('stage0','stage1','stage2','auth')) for n in sys.modules); "
        "print('isolated')"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "isolated"


def test_auth_login_url_and_callback_exchange_match_original_kite_flow(monkeypatch, tmp_path):
    captured = {}

    class FakeKite:
        def __init__(self, api_key, timeout=None):
            captured["api_key"] = api_key
            captured["timeout"] = timeout
        def login_url(self):
            return "https://kite.zerodha.com/connect/login?api_key=test-key&v=3"
        def generate_session(self, request_token, api_secret):
            captured["request_token"] = request_token
            captured["api_secret"] = api_secret
            return {"access_token": "new-token", "user_id": "u-100", "user_name": "demo", "login_time": datetime.now(timezone.utc)}
        def set_access_token(self, token):
            captured["token"] = token
        def profile(self):
            return {"user_id": "u-100"}

    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    config = Settings("test-key", "test-secret", "", Path("archive"), "INFO", "127.0.0.1", 8000, 5, 1, tmp_path / "session.json")

    login_url = zerodha.get_kite_login_url(config)
    assert login_url == "https://kite.zerodha.com/connect/login?api_key=test-key&v=3"

    result = zerodha.authenticate_request_token("req-123", config)
    assert result["authenticated"] is True
    assert captured["request_token"] == "req-123"
    assert captured["api_secret"] == "test-secret"
    assert zerodha.get_access_token(config) == "new-token"
    assert captured["token"] == "new-token"
