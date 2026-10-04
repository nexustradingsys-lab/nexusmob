import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.auth import zerodha
from app.config import Settings


def settings_for(path: Path, token: str = "") -> Settings:
    return Settings("api-key", "api-secret", token, Path("archive"), "INFO", "127.0.0.1", 8000, 5, 1, path)


class FakeKite:
    def __init__(self, api_key, timeout=None):
        self.api_key = api_key
        self.timeout = timeout
        self.access_token = None

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?api_key=masked&v=3"

    def generate_session(self, request_token, api_secret):
        return {
            "access_token": "session-access-token",
            "user_id": "u-100",
            "login_time": datetime(2026, 10, 4, tzinfo=timezone.utc),
        }

    def set_access_token(self, access_token):
        self.access_token = access_token

    def profile(self):
        if self.access_token == "expired-session-token":
            raise RuntimeError("simulated expired token")
        return {"user_id": "u-100"}

    def instruments(self):
        return []


def test_login_url_generation(monkeypatch, tmp_path):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    auth = zerodha.KiteAuthSession(settings_for(tmp_path / "session.json"))
    assert auth.get_login_url() == "https://kite.zerodha.com/connect/login?api_key=masked&v=3"


def test_callback_exchange_persists_mobile_session_and_safe_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    path = tmp_path / "auth" / "session.json"
    auth = zerodha.KiteAuthSession(settings_for(path))

    result = auth.authenticate("request-token-value")
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert result == {"authenticated": True, "session_valid": True}
    assert stored == {
        "version": 1,
        "access_token": "session-access-token",
        "user_id": "u-100",
        "login_time": "2026-10-04T00:00:00+00:00",
    }
    assert auth.status()["token_length"] == len("session-access-token")
    assert "session-access-token" not in str(result)
    assert "request-token-value" not in path.read_text(encoding="utf-8")


def test_persisted_session_loads_before_bootstrap_token(monkeypatch, tmp_path):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    path = tmp_path / "session.json"
    zerodha.KiteSessionStore(path).replace({"version": 1, "access_token": "session-access-token"})
    auth = zerodha.KiteAuthSession(settings_for(path, token="stale-bootstrap-token"))

    client = auth.initialize()

    assert client is not None
    assert client.access_token == "session-access-token"
    assert auth.status()["session_source"] == "persisted"
    assert auth.status()["authenticated"] is True


def test_invalid_persisted_token_blocks_bootstrap_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    path = tmp_path / "session.json"
    zerodha.KiteSessionStore(path).replace({"version": 1, "access_token": "expired-session-token"})
    auth = zerodha.KiteAuthSession(settings_for(path, token="stale-bootstrap-token"))

    assert auth.initialize() is None
    state = auth.status()
    assert state["authenticated"] is False
    assert state["action_required"] == "LOGIN"
    assert state["session_source"] == "persisted"
    assert state["token_present"] is True


def test_successful_reauthentication_replaces_mobile_session(monkeypatch, tmp_path):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    path = tmp_path / "session.json"
    zerodha.KiteSessionStore(path).replace({"version": 1, "access_token": "old-token"})
    auth = zerodha.KiteAuthSession(settings_for(path))

    auth.authenticate("new-request-token")

    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "session-access-token"
    assert auth.create_kite_client().access_token == "session-access-token"


def test_session_store_permissions_and_logout_tombstone(tmp_path):
    import os

    path = tmp_path / "auth" / "session.json"
    store = zerodha.KiteSessionStore(path)
    store.replace({"version": 1, "access_token": "session-access-token"})

    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    store.clear()
    assert store.has_state()
    assert store.load() == {"version": 1, "logged_out": True}


def test_auth_failures_do_not_log_exception_secrets(monkeypatch, tmp_path, caplog):
    class FailingKite(FakeKite):
        def generate_session(self, request_token, api_secret):
            raise RuntimeError("request-token-value api-secret-value")

    monkeypatch.setattr(zerodha, "KiteConnect", FailingKite)
    auth = zerodha.KiteAuthSession(settings_for(tmp_path / "session.json"))
    with caplog.at_level(logging.DEBUG):
        try:
            auth.authenticate("request-token-value")
        except zerodha.KiteAuthenticationError:
            pass
    assert "request-token-value" not in caplog.text
    assert "api-secret-value" not in caplog.text


def test_mobile_auth_module_has_no_original_nexus_imports():
    import sys

    assert not any(name == "auth" or name.startswith(("stage0", "stage1", "stage2")) for name in sys.modules)


def test_auth_routes_complete_private_login_callback_and_logout(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    config = settings_for(tmp_path / "session.json")
    monkeypatch.setattr(main, "settings", config)

    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        status = client.get("/auth/zerodha/status").json()
        assert status["configured"] is True
        assert status["authenticated"] is False
        assert status["token_present"] is False
        assert client.get("/auth/zerodha/login").json()["login_url"].startswith("https://kite.zerodha.com/")
        with caplog.at_level(logging.INFO):
            callback = client.get(
                "/api/callback",
                params={"request_token": "request-token-value"},
                follow_redirects=False,
            )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/mobile?auth=success"
        status = client.get("/auth/zerodha/status").json()
        assert status["authenticated"] is True
        assert status["token_present"] is True
        assert status["token_length"] == len("session-access-token")
        app_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("nexus_mobile"))
        assert "request-token-value" not in app_logs
        assert "api-secret" not in app_logs
        assert client.post("/auth/zerodha/logout").json()["authenticated"] is False
        assert client.get("/auth/zerodha/status").json()["token_present"] is False


def test_auth_routes_reject_public_source_addresses(monkeypatch):
    monkeypatch.setattr(main, "settings", settings_for(Path("session.json")))
    with TestClient(main.app, client=("8.8.8.8", 50000)) as client:
        assert client.get("/auth/zerodha/status").status_code == 403


def test_app_restart_reloads_persisted_token_and_initializes_kite(monkeypatch, tmp_path):
    monkeypatch.setattr(zerodha, "KiteConnect", FakeKite)
    path = tmp_path / "auth" / "session.json"
    zerodha.KiteSessionStore(path).replace({"version": 1, "access_token": "session-access-token"})
    monkeypatch.setattr(main, "settings", settings_for(path, token="stale-bootstrap-token"))

    for port in (50000, 50001):
        with TestClient(main.app, client=("127.0.0.1", port)):
            provider = main.app.state.kite_provider
            assert provider is not None
            assert provider.client.access_token == "session-access-token"
            assert main.app.state.kite_auth.status()["authenticated"] is True