from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from autoquant_backend.auth.store import UserStore


DEFAULT_SESSION_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    username: str
    display_name: str
    role: str
    auth_type: str = "session"

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "auth_type": self.auth_type,
        }


@dataclass(frozen=True, slots=True)
class _Session:
    user_id: str
    expires_at: float


class AuthService:
    def __init__(
        self, store: UserStore | None = None, *, session_seconds: int = DEFAULT_SESSION_SECONDS
    ) -> None:
        self.store = store or UserStore()
        self.session_seconds = max(300, int(session_seconds))
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _principal(user: Any) -> Principal:
        return Principal(
            user_id=user.user_id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
        )

    def login(self, username: str, password: str) -> dict[str, Any]:
        user = self.store.authenticate(username, password)
        if user is None:
            raise ValueError("用户名或密码错误，或账号已停用")
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.session_seconds
        with self._lock:
            self._sessions[self._token_key(token)] = _Session(user.user_id, expires_at)
        return {
            "token": token,
            "expires_at": int(expires_at * 1000),
            "user": self._principal(user).public(),
        }

    def authenticate_token(self, token: str) -> Principal | None:
        key = self._token_key(str(token))
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.expires_at <= time.time():
                self._sessions.pop(key, None)
                return None
        user = self.store.get(session.user_id)
        if user is None or not user.active:
            self.revoke_user(session.user_id)
            return None
        return self._principal(user)

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(self._token_key(str(token)), None)

    def revoke_user(self, user_id: str) -> None:
        with self._lock:
            keys = [key for key, value in self._sessions.items() if value.user_id == user_id]
            for key in keys:
                self._sessions.pop(key, None)

    def change_password(self, user_id: str, current_password: str, password: str) -> None:
        user = self.store.get(user_id)
        if user is None or self.store.authenticate(user.username, current_password) is None:
            raise ValueError("当前密码错误")
        self.store.set_password(user_id, password)
        self.revoke_user(user_id)

