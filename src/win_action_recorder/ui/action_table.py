"""Action list widget with edit and ordering controls."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QWidget,
)

from ..models import Action, ActionType

TYPE_LABELS = {
    ActionType.MOUSE_MOVE: "鼠标移动",
    ActionType.MOUSE_BUTTON: "鼠标按钮",
    ActionType.MOUSE_SCROLL: "鼠标滚轮",
    ActionType.KEY_DOWN: "按键按下",
    ActionType.KEY_UP: "按键释放",
}

BUTTON_LABELS = {"left": "左键", "right": "右键", "middle": "中键"}
OPERATION_LABELS = ("编辑", "↑", "↓", "删除")
TYPE_COLORS = {
    # These two tones remain recognizably cyan/green while reaching 4.5:1 on
    # both paper rows and the darker alternating-row surface.
    ActionType.MOUSE_MOVE: "#06717D",
    ActionType.MOUSE_BUTTON: "#147252",
    ActionType.MOUSE_SCROLL: "#8A651C",
    ActionType.KEY_DOWN: "#B5423B",
    ActionType.KEY_UP: "#17233B",
}

OPERATION_COLORS = ("#36C5D8", "#67D5A5", "#F6D66F", "#F06B62")


def action_description(action: Action) -> str:
    """Return a privacy-conscious, concise display description."""
    params = action.params
    if action.type is ActionType.MOUSE_MOVE:
        return "移动指针"
    if action.type is ActionType.MOUSE_BUTTON:
        button = BUTTON_LABELS.get(str(params.get("button")), str(params.get("button", "")))
        return f"{button}{'按下' if params.get('pressed') else '释放'}"
    if action.type is ActionType.MOUSE_SCROLL:
        return f"滚动 dx={params.get('dx', 0)}, dy={params.get('dy', 0)}"
    # Deliberately avoid showing literal printable characters in the overview.
    key = str(params.get("key", ""))
    shown = "普通字符" if len(key) == 1 else key
    return f"{TYPE_LABELS[action.type]}：{shown}"


def action_target(action: Action) -> str:
    params = action.params
    if action.type in {
        ActionType.MOUSE_MOVE,
        ActionType.MOUSE_BUTTON,
        ActionType.MOUSE_SCROLL,
    }:
        return f"({params.get('x', '—')}, {params.get('y', '—')})"
    key = str(params.get("key", ""))
    return "字符" if len(key) == 1 else key


class OperationDelegate(QStyledItemDelegate):
    """Paint lightweight row buttons without creating thousands of widgets."""

    operation_clicked = Signal(int, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pressed_operation: tuple[QPersistentModelIndex, str] | None = None

    @staticmethod
    def _button_rects(rect: QRect) -> list[QRect]:
        margin = 2
        gap = 2
        available = max(4, rect.width() - margin * 2 - gap * 3)
        width = available // 4
        return [
            QRect(
                rect.left() + margin + index * (width + gap),
                rect.top() + 4,
                width,
                max(20, rect.height() - 8),
            )
            for index in range(4)
        ]

    @staticmethod
    def _operation_enabled(index: QModelIndex, label: str) -> bool:
        if label == "↑":
            return index.row() > 0
        if label == "↓":
            model = index.model()
            return model is not None and index.row() + 1 < model.rowCount()
        return True

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[no-untyped-def]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Weight.DemiBold))
        ink = QColor("#17233B")
        muted = QColor("#858D96")
        view_enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        for label, color, rect in zip(
            OPERATION_LABELS,
            OPERATION_COLORS,
            self._button_rects(option.rect),
        ):
            command_enabled = view_enabled and self._operation_enabled(index, label)
            edge = ink if command_enabled else muted
            # Two-pixel offset shadow plus an unsmoothed face gives every row
            # command the same tactile 16-bit treatment as the main controls.
            shadow = rect.adjusted(2, 2, 0, 0)
            face = rect.adjusted(0, 0, -2, -2)
            painter.fillRect(shadow, edge)
            painter.fillRect(
                face,
                QColor(color) if command_enabled else QColor("#D5D8D2"),
            )
            painter.setPen(QPen(edge, 1))
            painter.drawRect(face.adjusted(0, 0, -1, -1))
            painter.drawText(face, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:  # type: ignore[no-untyped-def]
        if not isinstance(event, QMouseEvent):
            return False
        event_type = event.type()
        if event_type not in {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
        } or event.button() is not Qt.MouseButton.LeftButton:
            return False
        point = event.position().toPoint()
        hit_label = next(
            (
                label
                for label, rect in zip(
                    OPERATION_LABELS,
                    self._button_rects(option.rect),
                )
                if rect.contains(point)
            ),
            None,
        )
        if hit_label is not None and not self._operation_enabled(index, hit_label):
            hit_label = None
        if event_type == QEvent.Type.MouseButtonPress:
            self._pressed_operation = (
                (QPersistentModelIndex(index), hit_label)
                if hit_label is not None
                else None
            )
            return hit_label is not None

        pressed = self._pressed_operation
        self._pressed_operation = None
        if pressed is None:
            return False
        pressed_index, pressed_label = pressed
        same_cell = (
            pressed_index.isValid()
            and pressed_index.model() is index.model()
            and pressed_index.row() == index.row()
            and pressed_index.column() == index.column()
        )
        if (
            same_cell
            and hit_label == pressed_label
            and self._operation_enabled(index, pressed_label)
        ):
            self.operation_clicked.emit(pressed_index.row(), pressed_label)
        # The matching press belonged to this delegate even when the pointer
        # was dragged away, so consume its release without firing another cell.
        return True


class ActionTableModel(QAbstractTableModel):
    """Virtual action model that materializes data only for visible cells."""

    enabled_change_requested = Signal(int, bool)

    def __init__(
        self,
        headers: Sequence[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._headers = tuple(headers)
        self._header_tooltips = (
            "是否执行",
            "动作序号",
            "动作类型",
            "动作说明",
            "动作前延迟",
            "坐标或按键",
            "行操作",
        )
        self._actions: list[Action] = []
        self._mono_font = QFont("Cascadia Mono")
        self._type_font = QFont()
        self._type_font.setBold(True)
        self._type_brushes = {
            action_type: QBrush(QColor(color))
            for action_type, color in TYPE_COLORS.items()
        }

    @property
    def actions(self) -> list[Action]:
        return self._actions

    def set_actions(self, actions: Sequence[Action]) -> None:
        self.beginResetModel()
        self._actions = list(actions)
        self.endResetModel()

    def refresh(self) -> None:
        """Notify the view after an existing action object changed in place."""

        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._actions)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._actions):
            return None
        action = self._actions[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(index.row(), column, action)
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return Qt.CheckState.Checked if action.enabled else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {0, 1, 4, 5}:
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ForegroundRole and column == 2:
            return self._type_brushes.get(action.type)
        if role == Qt.ItemDataRole.FontRole:
            if column in {1, 4, 5}:
                return self._mono_font
            if column == 2:
                return self._type_font
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 0:
                return "取消勾选后回放会跳过此动作"
            if column == 6:
                return "回车编辑，Delete 删除，Alt+↑/↓ 调整顺序"
            return self._display_value(index.row(), column, action)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            if column == 0:
                return "启用" if action.enabled else "停用"
            if column == 6:
                return "行操作：回车编辑，Delete 删除，Alt 加上或下方向键调整顺序"
            return self._display_value(index.row(), column, action)
        return None

    @staticmethod
    def _display_value(row: int, column: int, action: Action) -> str:
        return {
            1: str(row + 1),
            2: TYPE_LABELS.get(action.type, action.type.value),
            3: action_description(action),
            4: f"{action.delay_ms} ms",
            5: action_target(action),
        }.get(column, "")

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation is not Qt.Orientation.Horizontal:
            return None
        if not 0 <= section < len(self._headers):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._headers[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._header_tooltips[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            not index.isValid()
            or index.column() != 0
            or role != Qt.ItemDataRole.CheckStateRole
            or not 0 <= index.row() < len(self._actions)
        ):
            return False
        try:
            checked = Qt.CheckState(value) is Qt.CheckState.Checked
        except (TypeError, ValueError):
            return False
        if self._actions[index.row()].enabled == checked:
            return False
        self._actions[index.row()] = self._actions[index.row()].clone(
            enabled=checked
        )
        self.dataChanged.emit(
            index,
            index,
            [
                Qt.ItemDataRole.CheckStateRole,
                Qt.ItemDataRole.AccessibleTextRole,
            ],
        )
        # The owning window mirrors the request into its canonical action list
        # and dirty state. Updating this lightweight model first also keeps the
        # standard QAbstractItemModel contract when ActionTable is used alone.
        self.enabled_change_requested.emit(index.row(), checked)
        return True


class ActionTable(QTableView):
    """A read-mostly action table; mutations are requested through signals."""

    enabled_changed = Signal(int, bool)
    edit_requested = Signal(int)
    delete_requested = Signal(int)
    move_requested = Signal(int, int)

    HEADERS = ("启用", "序号", "类型", "动作说明", "前置延迟", "目标", "操作")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._action_model = ActionTableModel(self.HEADERS, self)
        self.setModel(self._action_model)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setObjectName("action_table")
        self.setAccessibleName("动作序列")
        self.setAccessibleDescription(
            "录制或载入的鼠标键盘动作列表；空格切换启用，按回车编辑、"
            "Delete 删除、Alt 加上下方向键调整顺序。"
        )
        vertical_header = self.verticalHeader()
        vertical_header.setVisible(False)
        vertical_header.setDefaultSectionSize(40)
        vertical_header.setMinimumSectionSize(40)
        vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(36)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        for column in range(len(self.HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for column, width in {
            0: 44,
            1: 40,
            2: 76,
            4: 76,
            5: 98,
            6: 140,
        }.items():
            self.setColumnWidth(column, width)
        self._operation_delegate = OperationDelegate(self)
        self.setItemDelegateForColumn(6, self._operation_delegate)
        self._operation_delegate.operation_clicked.connect(self._on_operation)
        self._action_model.enabled_change_requested.connect(
            self.enabled_changed.emit
        )
        self.doubleClicked.connect(self._on_index_double_clicked)

    def set_actions(self, actions: Sequence[Action]) -> None:
        selected_row = self.currentRow()
        self._action_model.set_actions(actions)
        if selected_row >= 0 and self.rowCount():
            self.selectRow(min(selected_row, self.rowCount() - 1))

    def refresh(self) -> None:
        self._action_model.refresh()

    def rowCount(self) -> int:
        return self._action_model.rowCount()

    def columnCount(self) -> int:
        return self._action_model.columnCount()

    def currentRow(self) -> int:
        index = self.currentIndex()
        return index.row() if index.isValid() else -1

    def _on_index_double_clicked(self, index: QModelIndex) -> None:
        if index.isValid() and index.column() != 6:
            self.edit_requested.emit(index.row())

    def _on_operation(self, row: int, label: str) -> None:
        if label == "编辑":
            self.edit_requested.emit(row)
        elif label == "↑":
            self.move_requested.emit(row, -1)
        elif label == "↓":
            self.move_requested.emit(row, 1)
        elif label == "删除":
            self.delete_requested.emit(row)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Expose the common row operations without requiring a mouse."""

        row = self.currentRow()
        key = event.key()
        modifiers = event.modifiers()
        if row >= 0 and key in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.edit_requested.emit(row)
            event.accept()
            return
        if row >= 0 and key == Qt.Key.Key_Delete:
            self.delete_requested.emit(row)
            event.accept()
            return
        if row >= 0 and modifiers & Qt.KeyboardModifier.AltModifier:
            if key == Qt.Key.Key_Up:
                self.move_requested.emit(row, -1)
                event.accept()
                return
            if key == Qt.Key.Key_Down:
                self.move_requested.emit(row, 1)
                event.accept()
                return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if self.rowCount():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        viewport = self.viewport().rect()
        width = max(180, min(430, viewport.width() - 64))
        panel = QRect(0, 0, width, 104)
        panel.moveCenter(viewport.center())
        shadow = panel.translated(5, 5)
        painter.fillRect(shadow, QColor("#17233B"))
        painter.fillRect(panel, QColor("#EAF0E9"))
        painter.setPen(QPen(QColor("#17233B"), 2))
        painter.drawRect(panel.adjusted(1, 1, -1, -1))
        painter.fillRect(QRect(panel.left() + 8, panel.top() + 8, 8, 8), QColor("#36C5D8"))
        painter.fillRect(QRect(panel.right() - 15, panel.bottom() - 15, 8, 8), QColor("#67D5A5"))
        painter.setPen(QColor("#465367"))
        painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
        painter.drawText(
            panel.adjusted(24, 18, -24, -18),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            "ACTION TRACK // EMPTY\n按 F8 开始录制，或用 Ctrl+O 打开动作脚本",
        )


__all__ = [
    "TYPE_LABELS",
    "ActionTable",
    "ActionTableModel",
    "OperationDelegate",
    "action_description",
    "action_target",
]
