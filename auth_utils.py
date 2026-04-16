from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from config import settings


def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = {"sub": subject}
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        sub: str = payload.get("sub")
        if sub is None:
            return None
        return sub
    except JWTError:
        return None


def verify_google_token(credential: str) -> dict:
    """
    Verify a Google ID token (from Sign In With Google / GSI).
    Returns the token claims dict on success.
    Raises ValueError with a human-readable message on failure.

    Requires GOOGLE_CLIENT_ID to be set in config.
    Install: google-auth>=2.0.0
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise ValueError("Google login is not configured on this server")

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as g_requests

        idinfo = id_token.verify_oauth2_token(
            credential,
            g_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except Exception as exc:
        raise ValueError(f"Invalid Google token: {exc}") from exc

    if not idinfo.get("email_verified"):
        raise ValueError("Google account email is not verified")

    return idinfo  # keys: sub, email, name, picture, hd (hosted domain), etc.

