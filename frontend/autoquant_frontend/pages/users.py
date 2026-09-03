from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoquant_frontend.components import show_error, show_info
from autoquant_frontend.components.dialogs import ask_yes_no
from autoquant_frontend.services.client import BackendClient, BackendClientError
from autoquant_frontend.ui.theme import COLORS


class AuthenticationDialog(QDialog):
    def __init__(
        self, client: BackendClient, *, setup: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.setup = setup
        self.user: dict[str, Any] = {}
        self.setWindowTitle("初始化管理员" if setup else "登录 AutoQuant")
        self.setModal(True)
        self.setMinimumWidth(390)

        layout = QVBoxLayout(self)
        note = QLabel(
            "首次使用，请创建管理员账号。该账号用于登录并管理其他用户。"
            if setup
            else "请输入 AutoQuant 用户名和密码。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("3-32 位字母、数字、点、下划线或连字符")
        form.addRow("用户名", self.username_edit)
        self.display_name_edit = QLineEdit()
        if setup:
            form.addRow("显示名称", self.display_name_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("至少 8 个字符")
        form.addRow("密码", self.password_edit)
        if setup:
            self.confirm_edit = QLineEdit()
            self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("确认密码", self.confirm_edit)
        layout.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {COLORS['negative']};")
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "创建并登录" if setup else "登录"
        )
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.password_edit.returnPressed.connect(self._submit)

    def _submit(self) -> None:
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if self.setup and password != self.confirm_edit.text():
            self.error_label.setText("两次输入的密码不一致")
            return
        try:
            result = (
                self.client.setup_admin(
                    username, password, self.display_name_edit.text().strip()
                )
                if self.setup
                else self.client.login(username, password)
            )
        except Exception as exc:
            self.error_label.setText(str(exc))
            return
        user = result.get("user", {})
        self.user = user if isinstance(user, dict) else {}
        self.accept()


def authenticate_client(client: BackendClient) -> dict[str, Any] | None:
    """Run first-user setup or login. Return None when the user cancels."""
    try:
        status = client.auth_status()
    except BackendClientError as exc:
        # A 404 keeps the frontend compatible with an older backend.
        if "HTTP 404" in str(exc):
            return {}
        show_error("无法连接后端", str(exc))
        return None
    if client.api_token:
        try:
            return client.current_user()
        except BackendClientError:
            pass
    setup = bool(status.get("setup_required"))
    if setup and not bool(status.get("local_setup_allowed")):
        show_error("需要初始化", "请先在后端服务器本机启动客户端并创建首位管理员。")
        return None
    dialog = AuthenticationDialog(client, setup=setup)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.user


class UsersPageMixin:
    def _build_users_page(self) -> None:
        layout = QVBoxLayout(self.users_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(10)

        current_group = QGroupBox("当前账号")
        current_layout = QHBoxLayout(current_group)
        self.current_user_label = QLabel()
        current_layout.addWidget(self.current_user_label, 1)
        self.change_password_button = self._button("修改密码", self._change_my_password)
        current_layout.addWidget(self.change_password_button)
        current_layout.addWidget(self._button("退出登录", self._logout))
        layout.addWidget(current_group)

        self.user_admin_group = QGroupBox("用户管理")
        admin_layout = QVBoxLayout(self.user_admin_group)
        create_row = QHBoxLayout()
        self.new_username_edit = QLineEdit()
        self.new_username_edit.setPlaceholderText("用户名")
        self.new_display_name_edit = QLineEdit()
        self.new_display_name_edit.setPlaceholderText("显示名称")
        self.new_password_edit = QLineEdit()
        self.new_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.new_password_edit.setPlaceholderText("初始密码（至少 8 位）")
        self.new_role_combo = QComboBox()
        self.new_role_combo.addItem("操作员", "OPERATOR")
        self.new_role_combo.addItem("管理员", "ADMIN")
        create_row.addWidget(self.new_username_edit)
        create_row.addWidget(self.new_display_name_edit)
        create_row.addWidget(self.new_password_edit)
        create_row.addWidget(self.new_role_combo)
        create_row.addWidget(self._button("添加用户", self._create_user, primary=True))
        admin_layout.addLayout(create_row)

        self.users_table = QTableWidget(0, 6)
        self.users_table.setHorizontalHeaderLabels(
            ["用户名", "显示名称", "角色", "状态", "最近登录", "创建时间"]
        )
        self.users_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.users_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.users_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.users_table.horizontalHeader().setStretchLastSection(True)
        admin_layout.addWidget(self.users_table, 1)
        actions = QHBoxLayout()
        actions.addWidget(self._button("刷新", self._refresh_users))
        actions.addWidget(self._button("启用/停用", self._toggle_selected_user))
        actions.addWidget(self._button("切换角色", self._toggle_selected_role))
        actions.addWidget(self._button("修改显示名称", self._rename_selected_user))
        actions.addWidget(self._button("重置密码", self._reset_selected_password))
        actions.addWidget(self._button("删除", self._delete_selected_user))
        actions.addStretch()
        admin_layout.addLayout(actions)
        layout.addWidget(self.user_admin_group, 1)

        self._users: list[dict[str, Any]] = []
        self._load_current_user()

    def _load_current_user(self) -> None:
        user = getattr(self, "authenticated_user", None)
        if not isinstance(user, dict):
            user = {}
        self.authenticated_user = user
        role = str(user.get("role", ""))
        role_text = "管理员" if role == "ADMIN" else "操作员"
        name = str(user.get("display_name") or user.get("username") or "未登录")
        username = str(user.get("username", ""))
        self.current_user_label.setText(
            f"{name}（{username}） · {role_text}" if username else name
        )
        self.change_password_button.setEnabled(user.get("auth_type") == "session")
        self.user_admin_group.setVisible(role == "ADMIN")
        if role == "ADMIN":
            self._refresh_users()

    @staticmethod
    def _user_datetime(value: Any) -> str:
        if value in (None, "", 0):
            return "—"
        try:
            return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return "—"

    def _refresh_users(self) -> None:
        try:
            self._users = self.backend_client.users()
        except Exception as exc:
            show_error("加载用户失败", str(exc))
            return
        self.users_table.setRowCount(len(self._users))
        for row, user in enumerate(self._users):
            values = (
                str(user.get("username", "")),
                str(user.get("display_name", "")),
                "管理员" if user.get("role") == "ADMIN" else "操作员",
                "启用" if user.get("active") else "停用",
                self._user_datetime(user.get("last_login_at")),
                self._user_datetime(user.get("created_at")),
            )
            for column, value in enumerate(values):
                self.users_table.setItem(row, column, QTableWidgetItem(value))
        self.users_table.resizeColumnsToContents()

    def _selected_user(self) -> dict[str, Any] | None:
        row = self.users_table.currentRow()
        if row < 0 or row >= len(self._users):
            show_info("用户管理", "请先选择一个用户。")
            return None
        return self._users[row]

    def _create_user(self) -> None:
        try:
            self.backend_client.create_user(
                self.new_username_edit.text(),
                self.new_password_edit.text(),
                display_name=self.new_display_name_edit.text(),
                role=str(self.new_role_combo.currentData()),
            )
        except Exception as exc:
            show_error("添加用户失败", str(exc))
            return
        self.new_username_edit.clear()
        self.new_display_name_edit.clear()
        self.new_password_edit.clear()
        self._refresh_users()
        show_info("用户管理", "用户已添加。")

    def _toggle_selected_user(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        try:
            self.backend_client.update_user(
                str(user["user_id"]), active=not bool(user.get("active"))
            )
        except Exception as exc:
            show_error("更新用户失败", str(exc))
            return
        self._refresh_users()

    def _toggle_selected_role(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        role = "OPERATOR" if user.get("role") == "ADMIN" else "ADMIN"
        try:
            self.backend_client.update_user(str(user["user_id"]), role=role)
        except Exception as exc:
            show_error("更新角色失败", str(exc))
            return
        self._refresh_users()

    def _rename_selected_user(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        display_name, accepted = QInputDialog.getText(
            self.users_page,
            "修改显示名称",
            "显示名称",
            QLineEdit.EchoMode.Normal,
            str(user.get("display_name", "")),
        )
        if not accepted:
            return
        try:
            self.backend_client.update_user(
                str(user["user_id"]), display_name=display_name
            )
        except Exception as exc:
            show_error("修改显示名称失败", str(exc))
            return
        self._refresh_users()

    @staticmethod
    def _password_dialog(title: str, label: str) -> tuple[str, bool]:
        dialog = QDialog()
        dialog.setWindowTitle(title)
        layout = QFormLayout(dialog)
        password = QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow(label, password)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return password.text(), accepted

    def _reset_selected_password(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        password, accepted = self._password_dialog("重置密码", "新密码")
        if not accepted:
            return
        try:
            self.backend_client.reset_user_password(str(user["user_id"]), password)
        except Exception as exc:
            show_error("重置密码失败", str(exc))
            return
        show_info("用户管理", "密码已重置，该用户的现有会话已退出。")

    def _delete_selected_user(self) -> None:
        user = self._selected_user()
        if user is None or not ask_yes_no(
            "删除用户", f"确定删除用户 {user.get('username', '')}？此操作不可撤销。"
        ):
            return
        try:
            self.backend_client.delete_user(str(user["user_id"]))
        except Exception as exc:
            show_error("删除用户失败", str(exc))
            return
        self._refresh_users()

    def _change_my_password(self) -> None:
        current, accepted = self._password_dialog("修改密码", "当前密码")
        if not accepted:
            return
        password, accepted = self._password_dialog("修改密码", "新密码")
        if not accepted:
            return
        try:
            self.backend_client.change_password(current, password)
        except Exception as exc:
            show_error("修改密码失败", str(exc))
            return
        show_info("修改密码", "密码已修改，请重新登录。")
        QApplication.quit()

    def _logout(self) -> None:
        try:
            self.backend_client.logout()
        except Exception:
            pass
        QApplication.quit()


__all__ = ["AuthenticationDialog", "UsersPageMixin", "authenticate_client"]
