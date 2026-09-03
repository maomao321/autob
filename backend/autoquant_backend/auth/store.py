from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoquant_shared.config import default_config_path


USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}", re.ASCII)
PASSWORD_ITERATIONS = 600_000
ALLOWED_ROLES = {"ADMIN", "OPERATOR"}


def default_user_store_path() -> Path:
    return default_config_path().with_name("users.sqlite3")


@dataclass(frozen=True, slots=True)
class StoredUser:
    user_id: str
    username: str
    display_name: str
    role: str
    active: bool
    created_at: int
    updated_at: int
    last_login_at: int | None

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
        }


def _normalize_username(value: str) -> str:
    username = str(value).strip()
    if USERNAME_PATTERN.fullmatch(username) is None:
        raise ValueError("用户名须为 3-32 位字母、数字、点、下划线或连字符")
    return username


def _validate_password(value: str) -> str:
    password = str(value)
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度须为 8-128 个字符")
    return password


def _normalize_display_name(value: str, username: str) -> str:
    display_name = str(value).strip() or username
    if len(display_name) > 64:
        raise ValueError("显示名称不能超过 64 个字符")
    return display_name


def _password_digest(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=32
    )


class UserStore:
    """Durable local users with salted password hashes and admin safeguards."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_user_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash BLOB NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_iterations INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_login_at INTEGER
                )
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _from_row(row: sqlite3.Row) -> StoredUser:
        return StoredUser(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            active=bool(row["active"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_login_at=(
                None if row["last_login_at"] is None else int(row["last_login_at"])
            ),
        )

    def has_users(self) -> bool:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            return row is not None

    def create(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        role: str = "OPERATOR",
        require_empty: bool = False,
    ) -> StoredUser:
        username = _normalize_username(username)
        password = _validate_password(password)
        display_name = _normalize_display_name(display_name, username)
        role = str(role).strip().upper()
        if role not in ALLOWED_ROLES:
            raise ValueError("角色必须是 ADMIN 或 OPERATOR")
        salt = os.urandom(16)
        digest = _password_digest(password, salt, PASSWORD_ITERATIONS)
        now = int(time.time() * 1000)
        user_id = uuid.uuid4().hex
        with self._lock, closing(self._connect()) as connection, connection:
            if require_empty and connection.execute(
                "SELECT 1 FROM users LIMIT 1"
            ).fetchone() is not None:
                raise RuntimeError("系统已经完成管理员初始化")
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        user_id, username, display_name, password_hash,
                        password_salt, password_iterations, role, active,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        display_name,
                        digest,
                        salt,
                        PASSWORD_ITERATIONS,
                        role,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名已存在") from exc
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        assert row is not None
        return self._from_row(row)

    def authenticate(self, username: str, password: str) -> StoredUser | None:
        submitted = str(password)
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
            if row is None:
                # Keep unknown-user requests computationally similar to bad passwords.
                _password_digest(submitted, b"\0" * 16, PASSWORD_ITERATIONS)
                return None
            expected = bytes(row["password_hash"])
            actual = _password_digest(
                submitted,
                bytes(row["password_salt"]),
                int(row["password_iterations"]),
            )
            if not hmac.compare_digest(actual, expected) or not bool(row["active"]):
                return None
            now = int(time.time() * 1000)
            connection.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
                (now, now, str(row["user_id"])),
            )
            refreshed = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (str(row["user_id"]),)
            ).fetchone()
        assert refreshed is not None
        return self._from_row(refreshed)

    def get(self, user_id: str) -> StoredUser | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def list(self) -> list[StoredUser]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _active_admin_count(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE role = 'ADMIN' AND active = 1"
        ).fetchone()
        return int(row["count"])

    def update(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        role: str | None = None,
        active: bool | None = None,
    ) -> StoredUser:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (str(user_id),)
            ).fetchone()
            if row is None:
                raise ValueError("用户不存在")
            next_name = (
                str(row["display_name"])
                if display_name is None
                else _normalize_display_name(display_name, str(row["username"]))
            )
            next_role = str(row["role"]) if role is None else str(role).strip().upper()
            if next_role not in ALLOWED_ROLES:
                raise ValueError("角色必须是 ADMIN 或 OPERATOR")
            next_active = bool(row["active"]) if active is None else bool(active)
            removes_active_admin = (
                str(row["role"]) == "ADMIN"
                and bool(row["active"])
                and (next_role != "ADMIN" or not next_active)
            )
            if removes_active_admin and self._active_admin_count(connection) <= 1:
                raise RuntimeError("不能停用或降级最后一名管理员")
            now = int(time.time() * 1000)
            connection.execute(
                """
                UPDATE users
                SET display_name = ?, role = ?, active = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (next_name, next_role, int(next_active), now, str(user_id)),
            )
            refreshed = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        assert refreshed is not None
        return self._from_row(refreshed)

    def set_password(self, user_id: str, password: str) -> None:
        password = _validate_password(password)
        salt = os.urandom(16)
        digest = _password_digest(password, salt, PASSWORD_ITERATIONS)
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET password_hash = ?, password_salt = ?,
                    password_iterations = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (digest, salt, PASSWORD_ITERATIONS, now, str(user_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("用户不存在")

    def delete(self, user_id: str) -> StoredUser:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (str(user_id),)
            ).fetchone()
            if row is None:
                raise ValueError("用户不存在")
            if (
                str(row["role"]) == "ADMIN"
                and bool(row["active"])
                and self._active_admin_count(connection) <= 1
            ):
                raise RuntimeError("不能删除最后一名管理员")
            connection.execute("DELETE FROM users WHERE user_id = ?", (str(user_id),))
        return self._from_row(row)
