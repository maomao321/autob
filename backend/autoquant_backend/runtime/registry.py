from __future__ import annotations

import re
import threading
from pathlib import Path

from autoquant_backend.runtime.service import BackendRuntime
from autoquant_backend.state import OrderLedger
from autoquant_shared.config import ConfigStore


USER_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)


class UserRuntimeRegistry:
    """Creates an isolated runtime and durable data directory for each user."""

    def __init__(
        self,
        legacy_runtime: BackendRuntime,
        *,
        legacy_owner_id: str | None = None,
        data_root: Path | None = None,
    ) -> None:
        self.legacy_runtime = legacy_runtime
        self.data_root = (
            data_root or legacy_runtime.config_store.path.parent / "user-data"
        )
        self._legacy_owner_path = self.data_root / "legacy-owner"
        persisted_owner = self._load_legacy_owner()
        self._legacy_owner_id = persisted_owner or legacy_owner_id
        self._runtimes: dict[str, BackendRuntime] = {}
        self._lock = threading.RLock()
        if persisted_owner is None and legacy_owner_id is not None:
            self._persist_legacy_owner(legacy_owner_id)

    def claim_legacy_runtime(self, user_id: str) -> None:
        """Assign pre-user-scoping data to the first initialized account."""
        self._validate_user_id(user_id)
        with self._lock:
            if self._legacy_owner_id is None:
                self._persist_legacy_owner(user_id)
                self._legacy_owner_id = user_id

    def runtime_for(self, user_id: str, *, auth_type: str) -> BackendRuntime:
        # Service-token and local-bootstrap callers retain access to the legacy
        # runtime for backwards-compatible unattended operation and setup.
        if auth_type != "session":
            return self.legacy_runtime
        self._validate_user_id(user_id)
        with self._lock:
            if user_id == self._legacy_owner_id:
                return self.legacy_runtime
            runtime = self._runtimes.get(user_id)
            if runtime is None:
                scope = self.data_root / user_id
                runtime = BackendRuntime(
                    config_store=ConfigStore(scope / "config.json"),
                    ledger=OrderLedger(scope / "orders.sqlite3"),
                    desired_state_path=scope / "running.json",
                    allow_environment_credentials=False,
                )
                self._runtimes[user_id] = runtime
            return runtime

    def restore_user(self, user_id: str) -> list[str]:
        self._validate_user_id(user_id)
        with self._lock:
            is_legacy_owner = user_id == self._legacy_owner_id
            has_scoped_data = (self.data_root / user_id).exists()
        if not is_legacy_owner and not has_scoped_data:
            return []
        return self.runtime_for(user_id, auth_type="session").restore_desired_runners()

    def deactivate(self, user_id: str, *, timeout: float = 10.0) -> None:
        """Stop a user's active work while preserving their durable data."""
        self._validate_user_id(user_id)
        with self._lock:
            if user_id == self._legacy_owner_id:
                runtime = self.legacy_runtime
            else:
                runtime = self._runtimes.pop(user_id, None)
        if runtime is not None:
            runtime.shutdown(timeout=timeout)

    def shutdown(self, *, timeout: float = 10.0) -> None:
        with self._lock:
            runtimes = [self.legacy_runtime, *self._runtimes.values()]
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.shutdown(timeout=timeout)

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if USER_SCOPE_PATTERN.fullmatch(str(user_id)) is None:
            raise ValueError("用户 ID 格式不正确")

    def _load_legacy_owner(self) -> str | None:
        if not self._legacy_owner_path.exists():
            return None
        try:
            user_id = self._legacy_owner_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("旧数据归属记录读取失败") from exc
        try:
            self._validate_user_id(user_id)
        except ValueError as exc:
            raise RuntimeError("旧数据归属记录已损坏") from exc
        return user_id

    def _persist_legacy_owner(self, user_id: str) -> None:
        self._validate_user_id(user_id)
        self.data_root.mkdir(parents=True, exist_ok=True)
        temporary = self._legacy_owner_path.with_suffix(".tmp")
        temporary.write_text(user_id, encoding="utf-8")
        temporary.replace(self._legacy_owner_path)
