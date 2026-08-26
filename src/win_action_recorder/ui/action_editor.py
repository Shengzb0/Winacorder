"""Modal editor for one recorded action."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..models import Action, ActionType
from .action_table import TYPE_LABELS

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


class ActionEditor(QDialog):
    """Edit delay and type-specific parameters with model validation."""

    def __init__(self, action: Action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = action
        self._result: Action | None = None
        self.setWindowTitle("编辑动作")
        self.setModal(True)
        self.setMinimumWidth(420)

        self.type_combo = QComboBox()
        for action_type in ActionType:
            self.type_combo.addItem(
                TYPE_LABELS.get(action_type, action_type.value), action_type.value
            )
        self.type_combo.setCurrentIndex(self.type_combo.findData(action.type.value))

        self.enabled_checkbox = QCheckBox("回放时执行此动作")
        self.enabled_checkbox.setChecked(action.enabled)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, INT32_MAX)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setValue(action.delay_ms)

        common_form = QFormLayout()
        common_form.addRow("动作类型", self.type_combo)
        common_form.addRow("动作前延迟", self.delay_spin)
        common_form.addRow("", self.enabled_checkbox)

        self.mouse_group = QGroupBox("鼠标参数")
        mouse_form = QFormLayout(self.mouse_group)
        self.x_spin = self._coordinate_spin()
        self.y_spin = self._coordinate_spin()
        position = QWidget()
        position_layout = QHBoxLayout(position)
        position_layout.setContentsMargins(0, 0, 0, 0)
        position_layout.addWidget(QLabel("X"))
        position_layout.addWidget(self.x_spin)
        position_layout.addWidget(QLabel("Y"))
        position_layout.addWidget(self.y_spin)
        mouse_form.addRow("坐标", position)

        self.button_combo = QComboBox()
        self.button_combo.addItem("左键", "left")
        self.button_combo.addItem("右键", "right")
        self.button_combo.addItem("中键", "middle")
        mouse_form.addRow("鼠标按钮", self.button_combo)
        self.pressed_checkbox = QCheckBox("按下（取消勾选表示释放）")
        mouse_form.addRow("按钮状态", self.pressed_checkbox)
        self.dx_spin = QSpinBox()
        self.dx_spin.setRange(INT32_MIN, INT32_MAX)
        self.dy_spin = QSpinBox()
        self.dy_spin.setRange(INT32_MIN, INT32_MAX)
        mouse_form.addRow("水平滚动 dx", self.dx_spin)
        mouse_form.addRow("垂直滚动 dy", self.dy_spin)

        self.key_group = QGroupBox("键盘参数")
        key_form = QFormLayout(self.key_group)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("例如 a、enter、ctrl_l、f5")
        key_form.addRow("按键", self.key_edit)

        hint = QLabel("更改动作顺序或延迟后，主界面会自动重算时间偏移。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #556377;")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(common_form)
        layout.addWidget(self.mouse_group)
        layout.addWidget(self.key_group)
        layout.addWidget(hint)
        layout.addWidget(buttons)

        params = action.params
        self.x_spin.setValue(int(params.get("x", 0)))
        self.y_spin.setValue(int(params.get("y", 0)))
        self.button_combo.setCurrentIndex(
            max(0, self.button_combo.findData(str(params.get("button", "left"))))
        )
        self.pressed_checkbox.setChecked(bool(params.get("pressed", True)))
        self.dx_spin.setValue(int(params.get("dx", 0)))
        self.dy_spin.setValue(int(params.get("dy", 0)))
        self.key_edit.setText(str(params.get("key", "")))
        self.type_combo.currentIndexChanged.connect(self._update_fields)
        self._update_fields()

    @staticmethod
    def _coordinate_spin() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(INT32_MIN, INT32_MAX)
        return spin

    @property
    def edited_action(self) -> Action | None:
        return self._result

    def _update_fields(self) -> None:
        action_type = ActionType(self.type_combo.currentData())
        is_mouse = action_type in {
            ActionType.MOUSE_MOVE,
            ActionType.MOUSE_BUTTON,
            ActionType.MOUSE_SCROLL,
        }
        self.mouse_group.setVisible(is_mouse)
        self.key_group.setVisible(
            action_type in {ActionType.KEY_DOWN, ActionType.KEY_UP}
        )
        button_mode = action_type is ActionType.MOUSE_BUTTON
        scroll_mode = action_type is ActionType.MOUSE_SCROLL
        self.button_combo.setEnabled(button_mode)
        self.pressed_checkbox.setEnabled(button_mode)
        self.dx_spin.setEnabled(scroll_mode)
        self.dy_spin.setEnabled(scroll_mode)
        self.adjustSize()

    def _validate_and_accept(self) -> None:
        action_type = ActionType(self.type_combo.currentData())
        if action_type is ActionType.MOUSE_MOVE:
            params = {"x": self.x_spin.value(), "y": self.y_spin.value()}
        elif action_type is ActionType.MOUSE_BUTTON:
            params = {
                "x": self.x_spin.value(),
                "y": self.y_spin.value(),
                "button": self.button_combo.currentData(),
                "pressed": self.pressed_checkbox.isChecked(),
            }
        elif action_type is ActionType.MOUSE_SCROLL:
            params = {
                "x": self.x_spin.value(),
                "y": self.y_spin.value(),
                "dx": self.dx_spin.value(),
                "dy": self.dy_spin.value(),
            }
        else:
            params = {"key": self.key_edit.text().strip()}
        try:
            self._result = Action(
                id=self._source.id,
                type=action_type,
                offset_ms=self._source.offset_ms,
                delay_ms=self.delay_spin.value(),
                enabled=self.enabled_checkbox.isChecked(),
                params=params,
            )
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "动作参数无效", str(exc))
            return
        self.accept()


__all__ = ["ActionEditor"]
