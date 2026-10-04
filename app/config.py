from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _read_secret(env_name: str, secret_name: str) -> str:
    env_val = os.getenv(env_name, "").strip()
    if env_val:
        return env_val
    secrets_dir = Path(os.getenv("SECRETS_DIR", "/run/secrets"))
    secret_path = secrets_dir / secret_name
    if secret_path.is_file():
        try:
            return secret_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


@dataclass(frozen=True)
class Settings:
    kite_api_key: str
    kite_api_secret: str
    kite_access_token: str
    archive_path: Path
    log_level: str
    host: str
    port: int
    request_timeout_seconds: int
    nse_retries: int
    auth_session_path: Path = Path("data/auth/zerodha_session.json")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            kite_api_key=_read_secret("KITE_API_KEY", "kite_api_key"),
            kite_api_secret=_read_secret("KITE_API_SECRET", "kite_api_secret"),
            kite_access_token=os.getenv("KITE_ACCESS_TOKEN", "").strip(),
            archive_path=Path(os.getenv("STAGE1_ARCHIVE_PATH", "data/stage1/runs")).resolve(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10")),
            nse_retries=int(os.getenv("NSE_RETRIES", "2")),
            auth_session_path=Path(
                os.getenv("MOBILE_AUTH_SESSION_PATH", "data/auth/zerodha_session.json")
            ).resolve(),
        )

    @property
    def kite_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret and self.kite_access_token)

    @property
    def kite_credentials_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret)


settings = Settings.from_env()
