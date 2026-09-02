from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def imported_modules(package_root: Path) -> set[str]:
    modules: set[str] = set()
    for source_path in package_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), source_path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def imported_modules_in_file(source_path: Path) -> set[str]:
    modules: set[str] = set()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), source_path.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


class PackageBoundaryTests(unittest.TestCase):
    def test_frontend_does_not_import_backend(self) -> None:
        modules = imported_modules(
            PROJECT_ROOT / "frontend" / "autoquant_frontend"
        )

        self.assertFalse(
            any(module.startswith("autoquant_backend") for module in modules)
        )

    def test_backend_does_not_import_frontend(self) -> None:
        modules = imported_modules(
            PROJECT_ROOT / "backend" / "autoquant_backend"
        )

        self.assertFalse(
            any(module.startswith("autoquant_frontend") for module in modules)
        )

    def test_frontend_components_do_not_depend_on_main_window(self) -> None:
        frontend = PROJECT_ROOT / "frontend" / "autoquant_frontend"
        for name in (
            "backtest_page.py",
            "ai_decision_page.py",
            "config_page.py",
            "constants.py",
            "contract_pool_page.py",
            "dialogs.py",
            "experience_page.py",
            "strategy_config_page.py",
            "theme.py",
            "trade_history_page.py",
            "trading_page.py",
            "widgets.py",
        ):
            with self.subTest(module=name):
                modules = imported_modules_in_file(frontend / name)
                self.assertNotIn("autoquant_frontend.app", modules)


if __name__ == "__main__":
    unittest.main()
