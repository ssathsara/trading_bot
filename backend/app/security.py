"""Authentication: Argon2 password hashing, JWT access tokens, simple rate limiting."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User

_ph = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)
ALGO = "HS256"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_token(username: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": username, "iat": now, "exp": now + timedelta(minutes=s.jwt_expire_minutes), "typ": "access"}
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=ALGO)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, get_settings().jwt_secret.get_secret_value(), algorithms=[ALGO],
                             options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc
    return str(payload["sub"])


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    username = decode_token(creds.credentials)
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown user")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin privileges required")
    return user


def ws_user(ws: WebSocket) -> str | None:
    token = ws.query_params.get("token")
    if not token:
        return None
    try:
        return decode_token(token)
    except HTTPException:
        return None


class RateLimiter:
    """In-memory sliding-window limiter keyed by client IP + bucket."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limit(bucket: str, limit: int, window: float = 60.0):
    def dep(request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        if not limiter.check(f"{bucket}:{ip}", limit, window):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"rate limit exceeded for {bucket}")
    return dep
