from __future__ import annotations

import json
import os
from typing import Any

from autoquant_shared.models import Direction


class LifecycleRuntimeMixin:
    """Desired-runner restoration and runtime shutdown."""
    def restore_desired_runners(self) -> list[str]:
        if not self.desired_state_path.exists():
            return []
        try:
            payload = json.loads(self.desired_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        config = self.config_store.load()
        if config.trading_mode == "REAL" and os.environ.get(
            "AUTOQUANT_RESTORE_REAL", ""
        ).strip() != "1":
            self._on_log(
                "ERROR",
                "SYSTEM",
                "检测到 REAL 模式的恢复记录；未设置 AUTOQUANT_RESTORE_REAL=1，"
                "为安全起见没有自动重启实盘策略",
            )
            return []
        restored: list[str] = []
        for symbol, direction in payload.get("runners", {}).items():
            try:
                self.start(symbol, direction)
                restored.append(symbol)
            except Exception as exc:
                self._on_log("ERROR", symbol, f"恢复运行失败：{exc}")
        return restored

    def _set_desired(
        self, symbol: str, direction: Direction, *, running: bool
    ) -> None:
        with self._lock:
            payload: dict[str, Any] = {"runners": {}}
            if self.desired_state_path.exists():
                try:
                    loaded = json.loads(
                        self.desired_state_path.read_text(encoding="utf-8")
                    )
                    if isinstance(loaded, dict) and isinstance(
                        loaded.get("runners"), dict
                    ):
                        payload = loaded
                except (OSError, json.JSONDecodeError):
                    pass
            runners = payload.setdefault("runners", {})
            if running:
                runners[symbol] = direction.value
            else:
                runners.pop(symbol, None)
            self.desired_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.desired_state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.desired_state_path)

    def shutdown(self, timeout: float = 10.0) -> None:
        # Do not close positions on service shutdown. Desired runners remain on
        # disk so a supervised restart can restore them safely.
        self.controller.stop_all(close_position=False)
        self.controller.wait_for_all(timeout=timeout)
