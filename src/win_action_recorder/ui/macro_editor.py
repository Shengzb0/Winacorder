"""Pixel-workbench editor used by the custom automation mode."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

from PySide6.QtCore import QRegularExpression, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QResizeEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..macro_dsl import MacroSyntaxError, MouseStatement, parse_macro

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_CLICK_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<slash>/)?mouse[ \t]+"
    r"(?P<button>left|right|middle)"
    r"(?:[ \t]*\[(?P<options>[^\]\r\n]*)\])?"
    r"(?:[ \t]+at[ \t]+(?P<x>[+-]?\d+)[ \t]+(?P<y>[+-]?\d+))?"
    r"[ \t]*(?P<comment>(?:#|//).*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _ClickLine:
    """A safely parsed, visualizable mouse-click source line."""

    indent: str
    slash: bool
    button: str
    last_ms: int
    count: int
    interval_ms: int
    after_ms: int
    x: int | None
    y: int | None
    comment: str


@dataclass(frozen=True, slots=True)
class SchemeChoice:
    """One selectable built-in or user-created macro scheme."""

    scheme_id: str
    label: str
    built_in: bool = False


class _MacroHighlighter(QSyntaxHighlighter):
    """Small deterministic highlighter for the line-oriented macro language."""

    def __init__(self, document: object) -> None:
        super().__init__(document)  # type: ignore[arg-type]
        keyword = QTextCharFormat()
        keyword.setForeground(QColor("#137A8B"))
        keyword.setFontWeight(QFont.Weight.Bold)
        number = QTextCharFormat()
        number.setForeground(QColor("#B64B44"))
        comment = QTextCharFormat()
        comment.setForeground(QColor("#667085"))
        comment.setFontItalic(True)
        brace = QTextCharFormat()
        brace.setForeground(QColor("#8A641B"))
        brace.setFontWeight(QFont.Weight.DemiBold)
        self._rules = (
            (
                QRegularExpression(
                    r"\b(?:let|function|func|call|next|wait|mouse|key|hotkey|repeat|"
                    r"while|true|break|at|move|random|down|up|left|right|middle|last|"
                    r"hold|count|interval|after|scroll)\b"
                ),
                keyword,
            ),
            (QRegularExpression(r"\b\d+(?:\.\d+)?(?:ms|s)?\b"), number),
            (QRegularExpression(r"[{}\[\](),:=+*/%\-]"), brace),
            (QRegularExpression(r"(?:#|//).*?$"), comment),
        )

    def highlightBlock(self, text: str) -> None:
        for expression, text_format in self._rules:
            iterator = expression.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(
                    match.capturedStart(),
                    match.capturedLength(),
                    text_format,
                )


class MacroEditorPane(QWidget):
    """Editor surface with scheme actions and explicit validation/run controls."""

    source_changed = Signal(str)
    scheme_selected = Signal(str)
    new_requested = Signal()
    save_requested = Signal()
    save_as_requested = Signal()
    delete_requested = Signal()
    validate_requested = Signal()
    run_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    command_preview_changed = Signal(str)
    command_inserted = Signal(str)
    command_updated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("macroEditorPane")
        self.setAccessibleName("自定义方案编辑器")

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        self.outer_layout = outer_layout
        self.editor_scroll_area = QScrollArea()
        self.editor_scroll_area.setObjectName("macroEditorScrollArea")
        self.editor_scroll_area.setWidgetResizable(True)
        # The scroll surface itself is not an action.  Keeping it out of the
        # tab chain lets keyboard users enter the visible form directly; each
        # child still asks QScrollArea to reveal it when focus moves.
        self.editor_scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.editor_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.editor_scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._set_accessibility(
            self.editor_scroll_area,
            "自定义方案工作区",
            "窗口较矮时可垂直滚动，鼠标指令积木、代码与运行控制均可到达。",
        )
        self.editor_canvas = QWidget()
        self.editor_canvas.setObjectName("macroEditorCanvas")
        layout = QVBoxLayout(self.editor_canvas)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.editor_canvas_layout = layout
        self.editor_scroll_area.setWidget(self.editor_canvas)
        outer_layout.addWidget(self.editor_scroll_area)

        self.scheme_toolbar = QFrame()
        self.scheme_toolbar.setObjectName("macroSchemeBar")
        self.scheme_toolbar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.scheme_toolbar_layout = QGridLayout(self.scheme_toolbar)
        self.scheme_toolbar_layout.setContentsMargins(10, 8, 10, 8)
        self.scheme_toolbar_layout.setHorizontalSpacing(6)
        self.scheme_toolbar_layout.setVerticalSpacing(5)
        self.scheme_caption = QLabel("方案")
        self.scheme_caption.setObjectName("pixelCaption")
        self.scheme_combo = QComboBox()
        self.scheme_combo.setObjectName("macroSchemeCombo")
        self._set_accessibility(
            self.scheme_combo,
            "方案列表",
            "选择内置示例或已保存的自定义方案。",
        )
        self.scheme_combo.setMinimumContentsLength(16)
        self.new_button = QPushButton("新建")
        self.save_button = QPushButton("保存")
        self.save_as_button = QPushButton("另存")
        self.delete_button = QPushButton("删除")
        self.delete_button.setProperty("danger", True)
        self._set_accessibility(
            self.new_button,
            "新建方案",
            "清空编辑器并开始一个未保存的自定义方案。",
        )
        self._set_accessibility(
            self.save_button,
            "保存当前方案",
            "将修改写入当前用户方案；内置示例不可覆盖。",
        )
        self._set_accessibility(
            self.save_as_button,
            "另存方案",
            "用新名称保存当前源码，内置示例也可另存。",
        )
        self._set_accessibility(
            self.delete_button,
            "删除当前方案",
            "删除当前用户方案；内置示例不可删除。",
        )
        self._scheme_toolbar_widgets = (
            self.scheme_caption,
            self.scheme_combo,
            self.new_button,
            self.save_button,
            self.save_as_button,
            self.delete_button,
        )
        self._scheme_toolbar_mode = ""
        layout.addWidget(self.scheme_toolbar)

        self.command_builder = QFrame()
        self.command_builder.setObjectName("macroCommandBuilder")
        self.command_builder.setAccessibleName("鼠标指令积木")
        self.command_builder.setAccessibleDescription(
            "用表单组合鼠标按键、保持时间、重复次数、间隔、后等待和固定坐标。"
        )
        self.command_builder.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        builder_layout = QVBoxLayout(self.command_builder)
        builder_layout.setContentsMargins(0, 0, 0, 0)
        builder_layout.setSpacing(0)

        self.builder_header = QFrame()
        self.builder_header.setObjectName("macroBuilderHeader")
        self.builder_header_layout = QGridLayout(self.builder_header)
        self.builder_header_layout.setContentsMargins(10, 5, 8, 5)
        self.builder_header_layout.setHorizontalSpacing(7)
        self.builder_header_layout.setVerticalSpacing(4)
        self.builder_title = QLabel("COMMAND BUILDER // 鼠标指令积木")
        self.builder_title.setObjectName("macroBuilderTitle")
        self.builder_title.setMinimumWidth(0)
        self.builder_title.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.builder_title.setAccessibleName("鼠标指令积木标题")
        self.builder_title.setAccessibleDescription("可折叠的鼠标点击指令构建区域。")
        self.builder_toggle_button = QPushButton("收起")
        self.builder_toggle_button.setObjectName("macroBuilderToggle")
        self.builder_toggle_button.setCheckable(True)
        self.builder_toggle_button.setChecked(True)
        self.builder_toggle_button.setAccessibleName("展开或收起鼠标指令积木")
        self.builder_toggle_button.setAccessibleDescription(
            "切换鼠标指令积木参数区域的显示状态。"
        )
        self._builder_user_toggled = False
        builder_layout.addWidget(self.builder_header)

        self.builder_content = QWidget()
        self.builder_content.setObjectName("macroBuilderContent")
        self.builder_content.setAccessibleName("鼠标指令参数")
        self.builder_content.setAccessibleDescription(
            "编辑鼠标点击指令并将预览插入源码，或更新当前鼠标点击行。"
        )
        self.builder_content.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        builder_content_layout = QVBoxLayout(self.builder_content)
        builder_content_layout.setContentsMargins(10, 6, 10, 7)
        builder_content_layout.setSpacing(4)

        self.builder_parameters = QWidget()
        self.builder_parameters.setObjectName("macroBuilderParameters")
        self.builder_parameters.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.builder_parameters_layout = QGridLayout(self.builder_parameters)
        self.builder_parameters_layout.setContentsMargins(0, 0, 0, 0)
        self.builder_parameters_layout.setHorizontalSpacing(7)
        self.builder_parameters_layout.setVerticalSpacing(5)

        self.mouse_button_combo = QComboBox()
        self.mouse_button_combo.setObjectName("macroMouseButton")
        self.mouse_button_combo.addItem("left // 左键", "left")
        self.mouse_button_combo.addItem("right // 右键", "right")
        self.mouse_button_combo.addItem("middle // 中键", "middle")
        self._set_accessibility(
            self.mouse_button_combo,
            "鼠标按键",
            "选择指令使用 left 左键、right 右键或 middle 中键。",
        )

        self.last_spin = self._time_spin_box(
            "按键保持时间",
            "鼠标按下到释放之间的毫秒数，范围 1 到 60000。",
            minimum=1,
            maximum=60_000,
            value=20,
        )
        self.last_spin.setObjectName("macroMouseLast")
        self.count_spin = QSpinBox()
        self.count_spin.setObjectName("macroMouseCount")
        self.count_spin.setRange(1, 10_000)
        self.count_spin.setValue(1)
        self.count_spin.setAccelerated(True)
        self._set_accessibility(
            self.count_spin,
            "点击次数",
            "一条鼠标指令连续执行的次数，范围 1 到 10000。",
        )
        self.interval_spin = self._time_spin_box(
            "重复点击间隔",
            "相邻点击之间等待的毫秒数；点击次数大于 1 时可编辑，最长 24 小时。",
            minimum=0,
            maximum=86_400_000,
            value=100,
        )
        self.interval_spin.setObjectName("macroMouseInterval")
        self.after_spin = self._time_spin_box(
            "指令后等待",
            "整条鼠标指令完成后等待的毫秒数，最长 24 小时。",
            minimum=0,
            maximum=86_400_000,
            value=0,
        )
        self.after_spin.setObjectName("macroMouseAfter")

        self.fixed_position_check = QCheckBox("固定坐标 // AT")
        self.fixed_position_check.setObjectName("macroMouseFixedPosition")
        self._set_accessibility(
            self.fixed_position_check,
            "使用固定坐标",
            "启用后在指令末尾加入 at X Y；关闭时在当前光标位置点击。",
        )
        self.x_spin = QSpinBox()
        self.x_spin.setObjectName("macroMouseX")
        self.x_spin.setRange(_INT32_MIN, _INT32_MAX)
        self._set_accessibility(
            self.x_spin,
            "固定坐标 X",
            "固定点击位置的 X 物理屏幕坐标，支持 32 位有符号整数。",
        )
        self.y_spin = QSpinBox()
        self.y_spin.setObjectName("macroMouseY")
        self.y_spin.setRange(_INT32_MIN, _INT32_MAX)
        self._set_accessibility(
            self.y_spin,
            "固定坐标 Y",
            "固定点击位置的 Y 物理屏幕坐标，支持 32 位有符号整数。",
        )

        self._builder_common_fields = (
            self._field_widget("BUTTON", self.mouse_button_combo),
            self._field_widget("LAST (MS)", self.last_spin),
            self._field_widget("COUNT", self.count_spin),
            self._field_widget("INTERVAL (MS)", self.interval_spin),
        )
        self._builder_advanced_fields = (
            self._field_widget("AFTER (MS)", self.after_spin),
            self._field_widget("POSITION", self.fixed_position_check),
            self._field_widget("X", self.x_spin),
            self._field_widget("Y", self.y_spin),
        )
        # Compatibility for integrations that only inspect the aggregate list.
        self._builder_fields = (
            *self._builder_common_fields,
            *self._builder_advanced_fields,
        )
        self._builder_column_count = 0
        self._builder_advanced_column_count = 0
        builder_content_layout.addWidget(self.builder_parameters)

        self.builder_line_hint = QLabel(
            "光标到 mouse 行即可回填 · 不兼容行不覆盖"
        )
        self.builder_line_hint.setObjectName("macroBuilderLineHint")
        self.builder_line_hint.setWordWrap(False)
        self.builder_line_hint.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self._set_accessibility(
            self.builder_line_hint,
            "当前源码行同步状态",
            "说明光标所在源码行能否由鼠标指令积木无损编辑。",
        )
        self.builder_line_hint.setToolTip(
            "光标移到 mouse 点击行即可回填；不兼容的行不会被积木覆盖。"
        )
        self.builder_more_toggle_button = QPushButton("更多参数")
        # Reuse the compact pixel-toggle treatment without adding a second
        # one-off stylesheet token.
        self.builder_more_toggle_button.setObjectName("macroBuilderToggle")
        self.builder_more_toggle_button.setCheckable(True)
        self.builder_more_toggle_button.setChecked(False)
        self.builder_more_toggle_button.setMaximumHeight(34)
        self._set_accessibility(
            self.builder_more_toggle_button,
            "展开或收起更多鼠标参数",
            "展开后可编辑指令后等待、固定坐标，并查看详细流程与 DSL 预览。",
        )
        self.builder_more_toggle_button.setToolTip(
            "展开指令后等待、固定坐标和详细执行预览"
        )

        sync_row = QWidget()
        sync_row.setObjectName("macroBuilderActions")
        sync_row_layout = QHBoxLayout(sync_row)
        sync_row_layout.setContentsMargins(0, 0, 0, 0)
        sync_row_layout.setSpacing(6)
        sync_row_layout.addWidget(self.builder_line_hint, 1)
        sync_row_layout.addWidget(self.builder_more_toggle_button, 0)
        builder_content_layout.addWidget(sync_row)

        self.builder_advanced_content = QWidget()
        self.builder_advanced_content.setObjectName("macroBuilderAdvancedContent")
        self.builder_advanced_content.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self._set_accessibility(
            self.builder_advanced_content,
            "更多鼠标参数",
            "包含指令后等待、固定坐标和详细执行预览；默认收起。",
        )
        advanced_layout = QVBoxLayout(self.builder_advanced_content)
        advanced_layout.setContentsMargins(0, 2, 0, 0)
        advanced_layout.setSpacing(5)
        self.builder_advanced_parameters = QWidget()
        self.builder_advanced_parameters.setObjectName("macroBuilderParameters")
        self.builder_advanced_parameters.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.builder_advanced_parameters_layout = QGridLayout(
            self.builder_advanced_parameters
        )
        self.builder_advanced_parameters_layout.setContentsMargins(0, 0, 0, 0)
        self.builder_advanced_parameters_layout.setHorizontalSpacing(7)
        self.builder_advanced_parameters_layout.setVerticalSpacing(5)
        advanced_layout.addWidget(self.builder_advanced_parameters)

        flow_caption = QLabel("FLOW // 执行流程")
        flow_caption.setObjectName("macroBuilderCaption")
        flow_caption.setAccessibleName("执行流程预览标题")
        flow_caption.setAccessibleDescription("以下标签用可读步骤说明鼠标指令。")
        self.builder_flow_preview = QLabel()
        self.builder_flow_preview.setObjectName("macroBuilderFlow")
        self.builder_flow_preview.setWordWrap(True)
        self.builder_flow_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.builder_flow_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._set_accessibility(
            self.builder_flow_preview,
            "鼠标指令执行流程预览",
            "以按下、保持、释放、重复、间隔和后等待步骤显示当前参数。",
        )

        preview_caption = QLabel("DSL // 实时源码预览")
        preview_caption.setObjectName("macroBuilderCaption")
        preview_caption.setAccessibleName("DSL 源码预览标题")
        preview_caption.setAccessibleDescription("以下标签显示将写入编辑器的指令源码。")
        self.builder_preview = QLabel()
        self.builder_preview.setObjectName("macroBuilderPreview")
        self.builder_preview.setWordWrap(True)
        self.builder_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.builder_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._set_accessibility(
            self.builder_preview,
            "鼠标指令 DSL 预览",
            "显示按当前积木参数生成、可插入源码编辑器的安全文本。",
        )
        self.builder_previews = QWidget()
        self.builder_previews.setObjectName("macroBuilderPreviews")
        self.builder_previews.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        self.builder_previews_layout = QGridLayout(self.builder_previews)
        self.builder_previews_layout.setContentsMargins(0, 0, 0, 0)
        self.builder_previews_layout.setHorizontalSpacing(7)
        self.builder_previews_layout.setVerticalSpacing(2)
        self._builder_preview_widgets = (
            flow_caption,
            self.builder_flow_preview,
            preview_caption,
            self.builder_preview,
        )
        self._builder_previews_mode = ""
        advanced_layout.addWidget(self.builder_previews)
        self.builder_advanced_content.setVisible(False)
        builder_content_layout.addWidget(self.builder_advanced_content)

        self.insert_command_button = QPushButton("插入指令")
        self.insert_command_button.setObjectName("macroBuilderInsert")
        self._set_accessibility(
            self.insert_command_button,
            "插入鼠标指令",
            "将 DSL 预览作为完整新行插入当前源码行之后；空白行会直接替换。",
        )
        self.update_line_button = QPushButton("更新当前行")
        self.update_line_button.setObjectName("macroBuilderUpdate")
        self._set_accessibility(
            self.update_line_button,
            "更新当前鼠标指令行",
            "用 DSL 预览替换光标所在的可视化鼠标点击行，并保留原缩进。",
        )
        self.insert_command_button.setMaximumHeight(38)
        self.update_line_button.setMaximumHeight(38)
        self.builder_actions = QWidget()
        self.builder_actions.setObjectName("macroBuilderActions")
        builder_actions_layout = QHBoxLayout(self.builder_actions)
        builder_actions_layout.setContentsMargins(0, 0, 0, 0)
        builder_actions_layout.setSpacing(7)
        builder_actions_layout.addWidget(self.insert_command_button, 1)
        builder_actions_layout.addWidget(self.update_line_button, 1)
        builder_content_layout.addWidget(self.builder_actions)
        self._builder_header_mode = ""
        self._builder_header_widgets = (
            self.builder_title,
            self.builder_toggle_button,
        )
        builder_layout.addWidget(self.builder_content)
        layout.addWidget(self.command_builder)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("macroSourceEditor")
        self.editor.setAccessibleName("自定义方案代码")
        self.editor.setAccessibleDescription(
            "每行一条命令；next 可设置 random 随机偏移，鼠标点击可设置 "
            "last、count、interval、after 和 at 坐标；支持 let 数值变量、算术表达式、"
            "function/call 复用流程、组合键、显式按键状态与滚轮。"
        )
        self.editor.setTabChangesFocus(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.editor.setTabStopDistance(28.0)
        self.editor.setPlaceholderText(
            "# 左键保持 10ms，双击间隔 80ms，结束后等待 200ms\n"
            "mouse left[last:10ms, count:2, interval:80ms, after:200ms]\n"
            "# 固定位置：在末尾追加 at X Y"
        )
        self._highlighter = _MacroHighlighter(self.editor.document())
        layout.addWidget(self.editor)

        self.help_bar = QFrame()
        self.help_bar.setObjectName("macroHelpBar")
        self.help_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        help_layout = QHBoxLayout(self.help_bar)
        help_layout.setContentsMargins(10, 6, 10, 6)
        help_label = QLabel(
            "点击参数：last/hold 保持 · count 次数 · interval 间隔 · after 后等 · "
            "at X Y 固定位置；数值：let 变量 · + - * / % · function/call；"
            "其他：next T random D · mouse move/scroll · key/hotkey · repeat/while/break"
        )
        help_label.setObjectName("macroHelpText")
        help_label.setWordWrap(True)
        help_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        help_layout.addWidget(help_label, 1)
        layout.addWidget(self.help_bar)

        self.footer = QFrame()
        self.footer.setObjectName("macroRunBar")
        self._set_accessibility(
            self.footer,
            "方案运行控制栏",
            "显示语法检查状态，并提供检查、运行、暂停和停止操作。",
        )
        self.footer_layout = QGridLayout(self.footer)
        self.footer_layout.setContentsMargins(10, 8, 10, 8)
        self.footer_layout.setHorizontalSpacing(7)
        self.footer_layout.setVerticalSpacing(5)
        self.status_label = QLabel("等待检查")
        self.status_label.setObjectName("macroValidationStatus")
        self.status_label.setProperty("state", "idle")
        self._set_accessibility(
            self.status_label,
            "方案检查状态",
            "报告语法是否通过，或给出错误所在的行列。",
        )
        self.validate_button = QPushButton("检查语法")
        self.run_button = QPushButton("运行方案")
        self.run_button.setProperty("variant", "run")
        self.pause_button = QPushButton("暂停")
        self.pause_button.setProperty("variant", "pause")
        self.stop_button = QPushButton("停止")
        self.stop_button.setProperty("variant", "stop")
        self._set_accessibility(
            self.validate_button,
            "检查方案语法",
            "解析当前源码并报告错误行列，不执行任何输入动作。",
        )
        self._set_accessibility(
            self.run_button,
            "运行当前方案",
            "检查并运行当前源码中的鼠标和键盘动作。",
        )
        self._set_accessibility(
            self.pause_button,
            "暂停或继续方案",
            "暂停正在运行的方案，或继续已暂停的方案。",
        )
        self._set_accessibility(
            self.stop_button,
            "停止当前方案",
            "停止方案并释放由方案保持的鼠标或键盘输入。",
        )
        self._footer_widgets = (
            self.status_label,
            self.validate_button,
            self.run_button,
            self.pause_button,
            self.stop_button,
        )
        self._footer_mode = ""
        outer_layout.addWidget(self.footer)

        self.scheme_combo.currentIndexChanged.connect(self._emit_scheme_selected)
        self.editor.textChanged.connect(self._on_source_changed)
        self.new_button.clicked.connect(self.new_requested)
        self.save_button.clicked.connect(self.save_requested)
        self.save_as_button.clicked.connect(self.save_as_requested)
        self.delete_button.clicked.connect(self.delete_requested)
        self.validate_button.clicked.connect(self.validate_requested)
        self.run_button.clicked.connect(self.run_requested)
        self.pause_button.clicked.connect(self.pause_requested)
        self.stop_button.clicked.connect(self.stop_requested)
        self.builder_toggle_button.toggled.connect(self._set_builder_expanded)
        self.builder_toggle_button.clicked.connect(self._remember_builder_toggle)
        self.builder_more_toggle_button.toggled.connect(self._set_more_expanded)
        self.mouse_button_combo.currentIndexChanged.connect(
            self._refresh_command_preview
        )
        self.last_spin.valueChanged.connect(self._refresh_command_preview)
        self.count_spin.valueChanged.connect(self._refresh_command_preview)
        self.interval_spin.valueChanged.connect(self._refresh_command_preview)
        self.after_spin.valueChanged.connect(self._refresh_command_preview)
        self.fixed_position_check.toggled.connect(self._refresh_command_preview)
        self.x_spin.valueChanged.connect(self._refresh_command_preview)
        self.y_spin.valueChanged.connect(self._refresh_command_preview)
        self.insert_command_button.clicked.connect(self._insert_built_command)
        self.update_line_button.clicked.connect(self._update_current_click_line)
        self.editor.cursorPositionChanged.connect(self._sync_builder_from_cursor)
        self._running = False
        self._current_line_visualizable = False
        # Start from the smallest intrinsic layout so a window may genuinely
        # open narrow.  The first resize event promotes each region to the
        # wider arrangement when space is actually available.
        self._relayout_scheme_toolbar(0)
        self._relayout_builder(0)
        self._relayout_footer(0)
        self._configure_tab_order()
        self._refresh_command_preview()
        self._sync_builder_from_cursor()
        self.set_running(False)
        QTimer.singleShot(0, self._refresh_canvas_extent)

    @property
    def source(self) -> str:
        return self.editor.toPlainText()

    def set_source(self, source: str) -> None:
        if self.editor.toPlainText() == source:
            return
        self.editor.setPlainText(source)

    def set_scheme_choices(
        self,
        choices: Iterable[SchemeChoice],
        *,
        selected_id: str | None = None,
    ) -> None:
        self.scheme_combo.blockSignals(True)
        self.scheme_combo.clear()
        selected_index = -1
        for index, choice in enumerate(choices):
            suffix = "  [示例]" if choice.built_in else ""
            self.scheme_combo.addItem(f"{choice.label}{suffix}", choice)
            if choice.scheme_id == selected_id:
                selected_index = index
        if self.scheme_combo.count():
            self.scheme_combo.setCurrentIndex(max(0, selected_index))
        self.scheme_combo.blockSignals(False)
        self._update_scheme_actions()

    def current_choice(self) -> SchemeChoice | None:
        value = self.scheme_combo.currentData()
        return value if isinstance(value, SchemeChoice) else None

    def set_validation(self, message: str, *, state: str) -> None:
        self.status_label.setText(message)
        self.status_label.setToolTip(message)
        self.status_label.setProperty("state", state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_label.update()

    def set_running(self, running: bool, *, paused: bool = False) -> None:
        self._running = running
        self.editor.setReadOnly(running)
        self.scheme_combo.setEnabled(not running)
        self.command_builder.setEnabled(not running)
        for button in (
            self.new_button,
            self.save_button,
            self.save_as_button,
            self.delete_button,
            self.validate_button,
        ):
            button.setEnabled(not running)
        self.run_button.setEnabled(not running and bool(self.source.strip()))
        self.pause_button.setEnabled(running)
        self.pause_button.setText("继续" if paused else "暂停")
        self.stop_button.setEnabled(running)
        if not running:
            self._update_scheme_actions()
        self._refresh_builder_control_states()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._relayout_scheme_toolbar(event.size().width())
        self._relayout_builder(event.size().width())
        self._relayout_footer(event.size().width())
        self._refresh_canvas_extent()

    @staticmethod
    def _set_accessibility(widget: QWidget, name: str, description: str) -> None:
        widget.setAccessibleName(name)
        widget.setAccessibleDescription(description)

    @classmethod
    def _time_spin_box(
        cls,
        name: str,
        description: str,
        *,
        minimum: int,
        maximum: int,
        value: int,
    ) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(" ms")
        spin.setAccelerated(True)
        cls._set_accessibility(spin, name, description)
        return spin

    @staticmethod
    def _field_widget(caption: str, control: QWidget) -> QWidget:
        field = QWidget()
        field.setObjectName("macroBuilderField")
        field.setMinimumWidth(0)
        field.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        field_layout = QVBoxLayout(field)
        field_layout.setContentsMargins(0, 0, 0, 0)
        field_layout.setSpacing(2)
        label = QLabel(caption)
        label.setObjectName("macroBuilderFieldLabel")
        label.setAccessibleName(f"{control.accessibleName()}字段")
        label.setAccessibleDescription(control.accessibleDescription())
        control.setMinimumWidth(0)
        control.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        field_layout.addWidget(label)
        field_layout.addWidget(control)
        return field

    def _set_builder_expanded(self, expanded: bool) -> None:
        self.builder_content.setVisible(expanded)
        self.builder_toggle_button.setText("收起" if expanded else "展开")
        QTimer.singleShot(0, self._refresh_canvas_extent)

    def _remember_builder_toggle(self, _checked: bool) -> None:
        self._builder_user_toggled = True

    def _set_more_expanded(self, expanded: bool) -> None:
        self.builder_advanced_content.setVisible(expanded)
        self._update_more_toggle_text()
        QTimer.singleShot(0, self._refresh_canvas_extent)

    def _update_more_toggle_text(self) -> None:
        if self.builder_more_toggle_button.isChecked():
            text = "收起更多参数"
        else:
            advanced_active = (
                self.after_spin.value() > 0
                or self.fixed_position_check.isChecked()
            )
            text = "更多参数 · 已启用" if advanced_active else "更多参数"
        self.builder_more_toggle_button.setText(text)
        state = (
            "当前包含已启用的高级参数。"
            if self.after_spin.value() > 0 or self.fixed_position_check.isChecked()
            else "当前使用默认高级参数。"
        )
        self.builder_more_toggle_button.setAccessibleDescription(
            "展开后可编辑指令后等待、固定坐标，并查看详细流程与 DSL 预览。"
            + state
        )

    def _configure_tab_order(self) -> None:
        """Keep keyboard traversal aligned with the visible workbench flow."""

        controls = (
            self.scheme_combo,
            self.new_button,
            self.save_button,
            self.save_as_button,
            self.delete_button,
            self.builder_toggle_button,
            self.mouse_button_combo,
            self.last_spin,
            self.count_spin,
            self.interval_spin,
            self.builder_more_toggle_button,
            self.after_spin,
            self.fixed_position_check,
            self.x_spin,
            self.y_spin,
            self.insert_command_button,
            self.update_line_button,
            self.editor,
            self.validate_button,
            self.run_button,
            self.pause_button,
            self.stop_button,
        )
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)

    def _refresh_canvas_extent(self) -> None:
        layouts = (
            self.scheme_toolbar_layout,
            self.command_builder.layout(),
            self.builder_content.layout(),
            self.builder_parameters_layout,
            self.builder_advanced_content.layout(),
            self.builder_advanced_parameters_layout,
            self.builder_previews_layout,
            self.footer_layout,
            self.editor_canvas_layout,
        )
        for current_layout in layouts:
            if current_layout is not None:
                current_layout.invalidate()
                current_layout.activate()
        widgets = (
            self.scheme_toolbar,
            self.command_builder,
            self.editor,
            self.help_bar,
        )
        heights: list[int] = []
        for widget in widgets:
            if not widget.isHidden():
                if widget is self.editor:
                    heights.append(
                        max(
                            widget.minimumSizeHint().height(),
                            min(160, widget.sizeHint().height()),
                        )
                    )
                else:
                    heights.append(
                        max(
                            widget.minimumSizeHint().height(),
                            widget.sizeHint().height(),
                        )
                    )
        margins = self.editor_canvas_layout.contentsMargins()
        spacing = self.editor_canvas_layout.spacing() * max(0, len(heights) - 1)
        required_height = (
            margins.top()
            + margins.bottom()
            + spacing
            + sum(heights)
        )
        if self.editor_canvas.minimumHeight() != required_height:
            self.editor_canvas.setMinimumHeight(required_height)
            self.editor_canvas.updateGeometry()

    def _relayout_scheme_toolbar(self, width: int) -> None:
        if width >= 760:
            mode = "wide"
        elif width >= 360:
            mode = "stacked"
        else:
            mode = "narrow"
        if mode == self._scheme_toolbar_mode:
            return
        for widget in self._scheme_toolbar_widgets:
            self.scheme_toolbar_layout.removeWidget(widget)
        for column in range(6):
            self.scheme_toolbar_layout.setColumnStretch(column, 0)
        if mode == "wide":
            self.scheme_toolbar_layout.addWidget(self.scheme_caption, 0, 0)
            self.scheme_toolbar_layout.addWidget(self.scheme_combo, 0, 1)
            for column, button in enumerate(
                (
                    self.new_button,
                    self.save_button,
                    self.save_as_button,
                    self.delete_button,
                ),
                start=2,
            ):
                self.scheme_toolbar_layout.addWidget(button, 0, column)
            self.scheme_toolbar_layout.setColumnStretch(1, 1)
        else:
            button_columns = 4 if mode == "stacked" else 2
            self.scheme_toolbar_layout.addWidget(self.scheme_caption, 0, 0)
            self.scheme_toolbar_layout.addWidget(
                self.scheme_combo,
                0,
                1,
                1,
                max(1, button_columns - 1),
            )
            for index, button in enumerate(
                (
                    self.new_button,
                    self.save_button,
                    self.save_as_button,
                    self.delete_button,
                )
            ):
                row, column = divmod(index, button_columns)
                self.scheme_toolbar_layout.addWidget(button, row + 1, column)
            for column in range(button_columns):
                self.scheme_toolbar_layout.setColumnStretch(column, 1)
        self._scheme_toolbar_mode = mode

    def _relayout_builder(self, width: int) -> None:
        header_mode = "wide" if width >= 480 else "stacked"
        if header_mode != self._builder_header_mode:
            for widget in self._builder_header_widgets:
                self.builder_header_layout.removeWidget(widget)
            for column in range(4):
                self.builder_header_layout.setColumnStretch(column, 0)
            self.builder_header_layout.addWidget(self.builder_title, 0, 0)
            self.builder_header_layout.addWidget(self.builder_toggle_button, 0, 1)
            self.builder_header_layout.setColumnStretch(0, 1)
            self._builder_header_mode = header_mode
        preview_mode = "wide" if width >= 520 else "stacked"
        if preview_mode != self._builder_previews_mode:
            for widget in self._builder_preview_widgets:
                self.builder_previews_layout.removeWidget(widget)
            flow_caption, flow_preview, dsl_caption, dsl_preview = (
                self._builder_preview_widgets
            )
            if preview_mode == "wide":
                self.builder_previews_layout.addWidget(flow_caption, 0, 0)
                self.builder_previews_layout.addWidget(dsl_caption, 0, 1)
                self.builder_previews_layout.addWidget(flow_preview, 1, 0)
                self.builder_previews_layout.addWidget(dsl_preview, 1, 1)
                self.builder_previews_layout.setColumnStretch(0, 1)
                self.builder_previews_layout.setColumnStretch(1, 1)
            else:
                self.builder_previews_layout.addWidget(flow_caption, 0, 0)
                self.builder_previews_layout.addWidget(flow_preview, 1, 0)
                self.builder_previews_layout.addWidget(dsl_caption, 2, 0)
                self.builder_previews_layout.addWidget(dsl_preview, 3, 0)
                self.builder_previews_layout.setColumnStretch(0, 1)
                self.builder_previews_layout.setColumnStretch(1, 0)
            self._builder_previews_mode = preview_mode
        if width >= 520:
            columns = 4
        elif width >= 360:
            columns = 2
        else:
            columns = 1
        if columns != self._builder_column_count:
            self._relayout_builder_fields(
                self._builder_common_fields,
                self.builder_parameters_layout,
                columns,
            )
            self._builder_column_count = columns
        if columns != self._builder_advanced_column_count:
            self._relayout_builder_fields(
                self._builder_advanced_fields,
                self.builder_advanced_parameters_layout,
                columns,
            )
            self._builder_advanced_column_count = columns

    @staticmethod
    def _relayout_builder_fields(
        fields: tuple[QWidget, ...],
        layout: QGridLayout,
        columns: int,
    ) -> None:
        for field in fields:
            layout.removeWidget(field)
        for column in range(4):
            layout.setColumnStretch(column, 0)
        for index, field in enumerate(fields):
            row, column = divmod(index, columns)
            layout.addWidget(field, row, column)
        for column in range(columns):
            layout.setColumnStretch(column, 1)

    def _relayout_footer(self, width: int) -> None:
        if width >= 480:
            mode = "wide"
        elif width >= 360:
            mode = "stacked"
        else:
            mode = "narrow"
        if mode == self._footer_mode:
            return
        for widget in self._footer_widgets:
            self.footer_layout.removeWidget(widget)
        for column in range(5):
            self.footer_layout.setColumnStretch(column, 0)
        if mode == "wide":
            for column, widget in enumerate(self._footer_widgets):
                self.footer_layout.addWidget(widget, 0, column)
            self.footer_layout.setColumnStretch(0, 1)
        else:
            button_columns = 4 if mode == "stacked" else 2
            self.footer_layout.addWidget(
                self.status_label,
                0,
                0,
                1,
                button_columns,
            )
            for index, button in enumerate(self._footer_widgets[1:]):
                row, column = divmod(index, button_columns)
                self.footer_layout.addWidget(button, row + 1, column)
            for column in range(button_columns):
                self.footer_layout.setColumnStretch(column, 1)
        self._footer_mode = mode

    def _built_command(self) -> str:
        button = self.mouse_button_combo.currentData()
        if button not in {"left", "right", "middle"}:
            button = "left"
        options = [f"last:{self.last_spin.value()}ms"]
        if self.count_spin.value() > 1:
            options.extend(
                (
                    f"count:{self.count_spin.value()}",
                    f"interval:{self.interval_spin.value()}ms",
                )
            )
        if self.after_spin.value() > 0:
            options.append(f"after:{self.after_spin.value()}ms")
        command = f"mouse {button}[{', '.join(options)}]"
        if self.fixed_position_check.isChecked():
            command += f" at {self.x_spin.value()} {self.y_spin.value()}"
        return command

    def _refresh_command_preview(self, *_args: object) -> None:
        command = self._built_command()
        self.builder_preview.setText(command)
        button_name = {
            "left": "左键",
            "right": "右键",
            "middle": "中键",
        }.get(str(self.mouse_button_combo.currentData()), "左键")
        count = self.count_spin.value()
        flow = f"（{button_name}按下  →  保持 {self.last_spin.value()}ms  →  释放）"
        if count > 1:
            flow += (
                f" ×{count}  ·  相邻点击间：释放后等待 "
                f"{self.interval_spin.value()}ms 再按下"
            )
        if self.after_spin.value() > 0:
            flow += f"  ·  最终释放后等待 {self.after_spin.value()}ms"
        if self.fixed_position_check.isChecked():
            flow += f"  ·  固定位置 ({self.x_spin.value()}, {self.y_spin.value()})"
        self.builder_flow_preview.setText(flow)
        self._update_more_toggle_text()
        if command != getattr(self, "_last_command_preview", ""):
            self._last_command_preview = command
            self.command_preview_changed.emit(command)
        self._refresh_builder_control_states()

    def _refresh_builder_control_states(self) -> None:
        if not hasattr(self, "interval_spin"):
            return
        editable = not getattr(self, "_running", False)
        self.interval_spin.setEnabled(editable and self.count_spin.value() > 1)
        fixed = editable and self.fixed_position_check.isChecked()
        self.x_spin.setEnabled(fixed)
        self.y_spin.setEnabled(fixed)
        self.insert_command_button.setEnabled(editable)
        self.update_line_button.setEnabled(
            editable and self._current_line_visualizable
        )

    @staticmethod
    def _parse_click_line(text: str) -> _ClickLine | None:
        match = _CLICK_LINE_RE.fullmatch(text)
        if match is None:
            return None
        try:
            program = parse_macro(text)
        except MacroSyntaxError:
            return None
        if len(program.statements) != 1:
            return None
        statement = program.statements[0]
        if not isinstance(statement, MouseStatement) or statement.operation != "click":
            return None
        timings = (
            statement.last_ms,
            statement.interval_ms,
            statement.after_ms,
        )
        if any(not float(value).is_integer() for value in timings):
            # QSpinBox intentionally represents whole milliseconds.  A legal
            # sub-millisecond source line stays untouched instead of being
            # silently rounded by the visual editor.
            return None
        last_ms = int(statement.last_ms)
        interval_ms = int(statement.interval_ms)
        after_ms = int(statement.after_ms)
        if not 1 <= last_ms <= 60_000:
            return None
        if not 1 <= statement.count <= 10_000:
            return None
        if not 0 <= interval_ms <= 86_400_000:
            return None
        if not 0 <= after_ms <= 86_400_000:
            return None
        return _ClickLine(
            indent=match.group("indent"),
            slash=bool(match.group("slash")),
            button=statement.button or "left",
            last_ms=last_ms,
            count=statement.count,
            interval_ms=interval_ms,
            after_ms=after_ms,
            x=statement.x,
            y=statement.y,
            comment=match.group("comment") or "",
        )

    def _sync_builder_from_cursor(self) -> None:
        parsed = self._parse_click_line(self.editor.textCursor().block().text())
        self._current_line_visualizable = parsed is not None
        if parsed is None:
            current_text = self.editor.textCursor().block().text().strip()
            if current_text:
                self.builder_line_hint.setText(
                    "不能由积木无损编辑 · 更新已禁用"
                )
                self.builder_line_hint.setToolTip(
                    "当前行不能由积木无损编辑；更新已禁用，源码保持不变。"
                )
                self.builder_line_hint.setProperty("state", "limited")
            else:
                self.builder_line_hint.setText(
                    "空白行可插入 · mouse 行会自动回填"
                )
                self.builder_line_hint.setToolTip(
                    "空白行可直接插入；移到 mouse 点击行可自动回填。"
                )
                self.builder_line_hint.setProperty("state", "idle")
            self.builder_line_hint.style().unpolish(self.builder_line_hint)
            self.builder_line_hint.style().polish(self.builder_line_hint)
            self._refresh_builder_control_states()
            return
        self.builder_line_hint.setText(
            "当前 mouse 行已同步 · 更新保留原格式"
        )
        self.builder_line_hint.setToolTip(
            "已从当前 mouse 点击行回填；更新会保留缩进、斜杠前缀与尾注释。"
        )
        self.builder_line_hint.setProperty("state", "synced")
        self.builder_line_hint.style().unpolish(self.builder_line_hint)
        self.builder_line_hint.style().polish(self.builder_line_hint)
        controls = (
            self.mouse_button_combo,
            self.last_spin,
            self.count_spin,
            self.interval_spin,
            self.after_spin,
            self.fixed_position_check,
            self.x_spin,
            self.y_spin,
        )
        blockers = [QSignalBlocker(control) for control in controls]
        try:
            self.mouse_button_combo.setCurrentIndex(
                self.mouse_button_combo.findData(parsed.button)
            )
            self.last_spin.setValue(parsed.last_ms)
            self.count_spin.setValue(parsed.count)
            self.interval_spin.setValue(parsed.interval_ms)
            self.after_spin.setValue(parsed.after_ms)
            has_position = parsed.x is not None and parsed.y is not None
            self.fixed_position_check.setChecked(has_position)
            self.x_spin.setValue(parsed.x if parsed.x is not None else 0)
            self.y_spin.setValue(parsed.y if parsed.y is not None else 0)
        finally:
            del blockers
        self._refresh_command_preview()

    def _insert_built_command(self) -> None:
        if self._running:
            return
        command = self._built_command()
        cursor = self.editor.textCursor()
        cursor.clearSelection()
        block_text = cursor.block().text()
        indent_match = re.match(r"^[ \t]*", block_text)
        indent = indent_match.group(0) if indent_match is not None else ""
        if block_text.rstrip().endswith("{"):
            indent += "  "
        cursor.beginEditBlock()
        if block_text.strip():
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertText(f"\n{indent}{command}")
        else:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.insertText(f"{indent}{command}")
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self.command_inserted.emit(command)
        self._sync_builder_from_cursor()

    def _update_current_click_line(self) -> None:
        if self._running:
            return
        cursor = self.editor.textCursor()
        parsed = self._parse_click_line(cursor.block().text())
        if parsed is None:
            self._current_line_visualizable = False
            self._refresh_builder_control_states()
            return
        command = self._built_command()
        slash = "/" if parsed.slash else ""
        replacement = f"{parsed.indent}{slash}{command}"
        if parsed.comment:
            replacement += f"  {parsed.comment}"
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.insertText(replacement)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self.command_updated.emit(command)
        self._sync_builder_from_cursor()

    def _emit_scheme_selected(self, _index: int) -> None:
        self._update_scheme_actions()
        choice = self.current_choice()
        if choice is not None:
            self.scheme_selected.emit(choice.scheme_id)

    def _on_source_changed(self) -> None:
        source = self.editor.toPlainText()
        self.source_changed.emit(source)
        self._sync_builder_from_cursor()
        if not self.editor.isReadOnly():
            self.run_button.setEnabled(bool(source.strip()))
            self._update_scheme_actions()

    def _update_scheme_actions(self) -> None:
        choice = self.current_choice()
        built_in = bool(choice and choice.built_in)
        has_choice = choice is not None
        self.save_button.setEnabled(has_choice and not built_in)
        self.delete_button.setEnabled(has_choice and not built_in)
        self.save_as_button.setEnabled(has_choice or bool(self.source.strip()))


__all__ = ["MacroEditorPane", "SchemeChoice"]
