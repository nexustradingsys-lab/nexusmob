import csv
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional, Tuple

from kiteconnect import KiteConnect

from app.config import Settings, settings

class KiteAuthenticationError(RuntimeError):
    pass


class KiteSessionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = RLock()

    def has_state(self) -> bool:
        return self.path.exists()

    def load(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _protect_directory(self) -> None:
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
            return
        try:
            identity = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            sid = next(csv.reader(identity.splitlines()))[1]
            subprocess.run(
                ["icacls", str(self.path.parent), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError, IndexError, StopIteration):
            raise OSError("Unable to secure Mobile Kite session directory permissions.") from None

    def replace(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._protect_directory()
        fd, temporary_name = tempfile.mkstemp(prefix=".kite-session-", dir=self.path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as session_file:
                json.dump(payload, session_file, separators=(",", ":"))
                session_file.flush()
                os.fsync(session_file.fileno())
            os.replace(temporary_name, self.path)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def clear(self) -> None:
        self.replace({"version": 1, "logged_out": True})


class KiteAuthSession:
    def __init__(self, config: Settings = settings):
        self.config = config
        self.store = KiteSessionStore(config.auth_session_path)
        self._lock = RLock()
        self._client: Optional[KiteConnect] = None
        self._authenticated = False
        self._action_required = "LOGIN"

    def _effective_config(self) -> Settings:
        if self.config is not settings:
            return self.config
        api_key = os.getenv("KITE_API_KEY", "").strip() or self.config.kite_api_key
        api_secret = os.getenv("KITE_API_SECRET", "").strip() or self.config.kite_api_secret
        bootstrap_token = os.getenv("KITE_ACCESS_TOKEN", "").strip() or self.config.kite_access_token
        if (api_key, api_secret, bootstrap_token) == (
            self.config.kite_api_key,
            self.config.kite_api_secret,
            self.config.kite_access_token,
        ):
            return self.config
        return Settings(
            api_key,
            api_secret,
            bootstrap_token,
            self.config.archive_path,
            self.config.log_level,
            self.config.host,
            self.config.port,
            self.config.request_timeout_seconds,
            self.config.nse_retries,
            self.config.auth_session_path,
        )

    def _token_and_source(self) -> Tuple[Optional[str], str]:
        if self.store.has_state():
            token = self.store.load().get("access_token")
            return (token.strip() if isinstance(token, str) and token.strip() else None), "persisted"
        token = self._effective_config().kite_access_token
        if isinstance(token, str):
            token = token.strip() or None
        return token, "bootstrap" if token else "none"

    def get_access_token(self) -> Optional[str]:
        return self._token_and_source()[0]

    def get_login_url(self) -> str:
        config = self._effective_config()
        if not config.kite_api_key:
            raise KiteAuthenticationError("KITE_API_KEY is required to build the Kite login URL.")
        return KiteConnect(api_key=config.kite_api_key, timeout=config.request_timeout_seconds).login_url()

    def authenticate(self, request_token: str) -> Dict[str, Any]:
        if not request_token:
            raise KiteAuthenticationError("request_token is required.")
        config = self._effective_config()
        if not config.kite_credentials_configured:
            raise KiteAuthenticationError("KITE_API_KEY and KITE_API_SECRET are required for token exchange.")
        client = KiteConnect(api_key=config.kite_api_key, timeout=config.request_timeout_seconds)
        try:
            data = client.generate_session(request_token, api_secret=config.kite_api_secret) or {}
            access_token = data.get("access_token") or ""
            if not isinstance(access_token, str) or not access_token.strip():
                raise KiteAuthenticationError("Kite did not return an access token.")
            client.set_access_token(access_token)
            client.profile()
        except KiteAuthenticationError:
            raise
        except Exception:
            raise KiteAuthenticationError("Kite authentication failed; start a new login.") from None
        session = {
            "version": 1,
            "access_token": access_token,
            "user_id": data.get("user_id"),
            "login_time": data.get("login_time").isoformat()
            if hasattr(data.get("login_time"), "isoformat")
            else data.get("login_time"),
        }
        with self._lock:
            self.store.replace(session)
            self._client = client
            self._authenticated = True
            self._action_required = "READY"
        return {"authenticated": True, "session_valid": True}

    def create_kite_client(self, *, verify_profile: bool = False) -> KiteConnect:
        config = self._effective_config()
        token, _ = self._token_and_source()
        if not config.kite_credentials_configured or not token:
            with self._lock:
                self._authenticated = False
                self._action_required = "CONFIGURE" if not config.kite_credentials_configured else "LOGIN"
            raise KiteAuthenticationError("Kite credentials and an authenticated session are required.")
        client = KiteConnect(api_key=config.kite_api_key, timeout=config.request_timeout_seconds)
        client.set_access_token(token)
        if verify_profile:
            try:
                client.profile()
            except Exception:
                with self._lock:
                    self._authenticated = False
                    self._action_required = "LOGIN"
                raise KiteAuthenticationError("Kite access token is invalid or expired; login is required.") from None
            with self._lock:
                self._client = client
                self._authenticated = True
                self._action_required = "READY"
        return client

    def initialize(self) -> Optional[KiteConnect]:
        try:
            return self.create_kite_client(verify_profile=True)
        except KiteAuthenticationError:
            return None

    def clear(self) -> None:
        with self._lock:
            self.store.clear()
            self._client = None
            self._authenticated = False
            self._action_required = "LOGIN"

    def status(self) -> Dict[str, Any]:
        config = self._effective_config()
        token, source = self._token_and_source()
        configured = config.kite_credentials_configured
        authenticated = configured and self._authenticated
        return {
            "configured": configured,
            "authenticated": authenticated,
            "session_valid": authenticated,
            "action_required": "READY" if authenticated else self._action_required,
            "token_present": bool(token),
            "token_length": len(token) if token else 0,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest() if token else None,
            "session_source": source,
        }


_DEFAULT_AUTH_SESSION = KiteAuthSession()


def _auth_session(config: Settings = settings) -> KiteAuthSession:
    return _DEFAULT_AUTH_SESSION if config is settings else KiteAuthSession(config)


def get_access_token(config: Settings = settings) -> Optional[str]:
    return _auth_session(config).get_access_token()


def _effective_config(config: Settings = settings) -> Settings:
    return _auth_session(config)._effective_config()


def get_kite_login_url(config: Settings = settings) -> str:
    return _auth_session(config).get_login_url()


def authenticate_request_token(request_token: str, config: Settings = settings) -> Dict[str, Any]:
    return _auth_session(config).authenticate(request_token)


def clear_session() -> None:
    _DEFAULT_AUTH_SESSION.clear()


def status(config: Settings = settings) -> Dict[str, Any]:
    return _auth_session(config).status()


def create_kite_client(config: Settings = settings, *, verify_profile: bool = False) -> KiteConnect:
    return _auth_session(config).create_kite_client(verify_profile=verify_profile)
