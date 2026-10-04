from app.config import settings


def safe_error(error: BaseException) -> str:
    message = str(error)
    from app.auth.zerodha import get_access_token

    for secret in (settings.kite_api_key, settings.kite_api_secret, settings.kite_access_token, get_access_token() or ""):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message or type(error).__name__