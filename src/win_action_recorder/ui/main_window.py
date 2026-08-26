"""Main application window and worker-to-Qt integration."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QObject,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QResizeEvent, QShowEvent, QTextCursor
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..autoclicker import (
    AutoClicker,
    AutoClickEvent,
    AutoClickSettings,
    ClickLocationMode,
)
from ..branding import application_icon
from ..hotkeys import GlobalHotkeyManager, HotkeyError
from ..macro_dsl import (
    CompiledMacro,
    MacroLanguageError,
    MacroRunError,
    MacroRunner,
    compile_macro,
)
from ..macro_schemes import (
    BUILTIN_SCHEMES,
    MacroSchemeStore,
    SchemeStorageError,
    SchemeValidationError,
)
from ..models import (
    Action,
    ActionType,
    delete_action,
    recalculate_offsets,
    reorder_actions,
    utc_now_iso,
)
from ..player import PlaybackEvent, PlaybackOutcome, PlaybackSettings, Player
from ..recorder import InputRecorder, RecorderError
from ..screen import (
    CoordinateAnchor,
    ScreenConfigError,
    VirtualDesktop,
    capture_screen_config,
    compare_screen_configs,
    validate_action_coordinates,
    validate_coordinate_for_screen,
)
from ..state import AppState, StateManager, StateSnapshot
from ..storage import StorageError, load_script, save_script
from .action_editor import ActionEditor
from .action_table import ActionTable
from .coordinate_picker import CoordinatePicker
from .icons import tool_icon
from .macro_editor import MacroEditorPane, SchemeChoice

logger = logging.getLogger(__name__)


class WorkerBridge(QObject):
    """Queue worker callbacks onto the Qt GUI thread."""

    action_recorded = Signal(object, int)
    recorder_error = Signal(str)
    hotkey_error = Signal(str)
    record_hotkey = Signal()
    play_hotkey = Signal()
    auto_click_hotkey = Signal()
    emergency_hotkey = Signal()
    playback_event = Signal(object)
    macro_event = Signal(object)
    auto_click_event = Signal(object)
    state_changed = Signal(object)


class MainWindow(QMainWindow):
    """Chinese desktop UI coordinating independent recorder/player services."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("WinActionRecorder")
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1280, 800)
        self.setMinimumSize(960, 640)

        self._settings = QSettings()
        self._actions: list[Action] = []
        self._action_inventory_cache: tuple[int, int, int] | None = None
        self._script_path: Path | None = None
        self._screen_config: dict[str, object] | None = None
        self._created_at = utc_now_iso()
        self._pending_recording_screen_config: dict[str, object] | None = None
        self._pending_recording_created_at: str | None = None
        self._recording_session = 0
        self._dirty = False
        self._auto_click_completed = 0
        self._auto_click_target: int | None = 10
        self._auto_click_phase = "idle"
        self._fixed_target_confirmed = False
        self._coordinate_anchor: CoordinateAnchor | None = None
        self._macro_phase = "idle"
        self._macro_actions_executed = 0
        self._macro_dirty = False
        self._macro_loading = False
        self._macro_active_scheme_id: str | None = None
        self._macro_last_error: str | None = None
        self._restoring_settings = False
        self._closing = False
        self._close_prompt_active = False
        self._close_pending = False
        self._final_close = False
        self._close_deadline = 0.0
        self._close_timeout_reported = False
        self._close_warning: QMessageBox | None = None
        self._header_compact: bool | None = None
        self._responsive_update_pending = False
        self._screen_signal_handle: object | None = None

        self._bridge = WorkerBridge(self)
        self._state_manager = StateManager()
        self._state_unsubscribe = self._state_manager.subscribe(
            lambda snapshot: self._bridge.state_changed.emit(snapshot),
            emit_current=False,
        )
        self._recorder = InputRecorder(
            on_action=self._emit_recorded_action,
            on_error=lambda error: self._bridge.recorder_error.emit(str(error)),
        )
        self._hotkeys = GlobalHotkeyManager(
            on_record_toggle=self._bridge.record_hotkey.emit,
            on_play_pause=self._bridge.play_hotkey.emit,
            on_auto_click_toggle=self._bridge.auto_click_hotkey.emit,
            on_emergency_stop=self._bridge.emergency_hotkey.emit,
            on_error=lambda error: self._bridge.hotkey_error.emit(str(error)),
        )
        self._player = Player(
            state_manager=self._state_manager,
            on_event=lambda event: self._bridge.playback_event.emit(event),
            injection_guard=self._hotkeys.suppression,
        )
        self._autoclicker = AutoClicker(
            state_manager=self._state_manager,
            on_event=lambda event: self._bridge.auto_click_event.emit(event),
        )
        self._macro_runner = MacroRunner(
            state_manager=self._state_manager,
            on_event=lambda event: self._bridge.macro_event.emit(event),
            injection_guard=self._hotkeys.suppression,
        )
        app_data = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        scheme_root = Path(app_data or str(Path.home() / ".win_action_recorder"))
        self._macro_store = MacroSchemeStore(scheme_root / "macro_schemes")
        self._coordinate_picker = CoordinatePicker(self)

        self._record_countdown_timer = QTimer(self)
        self._record_countdown_timer.setInterval(1000)
        self._record_countdown_timer.timeout.connect(self._record_countdown_tick)
        self._record_countdown_remaining = 0
        self._record_status_timer = QTimer(self)
        self._record_status_timer.setSingleShot(True)
        self._record_status_timer.setInterval(100)
        self._record_status_timer.timeout.connect(self._flush_recording_status)
        self._close_poll_timer = QTimer(self)
        self._close_poll_timer.setInterval(50)
        self._close_poll_timer.timeout.connect(self._poll_async_close)
        self._input_worker_completion_timer = QTimer(self)
        self._input_worker_completion_timer.setInterval(10)
        self._input_worker_completion_timer.timeout.connect(
            self._poll_input_worker_completion
        )

        self._create_actions()
        self._build_ui()
        self._connect_signals()
        self._reload_macro_schemes()
        self._restore_settings()
        self._apply_state(self._state_manager.snapshot())

        QTimer.singleShot(0, self._start_hotkeys)
        QTimer.singleShot(100, self._show_first_run_notice)

    # ------------------------------------------------------------------ UI
    def _create_actions(self) -> None:
        self.new_action = QAction("新建", self)
        self.new_action.setShortcut("Ctrl+N")
        self.open_action = QAction("打开", self)
        self.open_action.setShortcut("Ctrl+O")
        self.save_action = QAction("保存", self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_as_action = QAction("另存为", self)
        self.save_as_action.setShortcut("Ctrl+Shift+S")
        self.record_action = QAction("录制 (F8)", self)
        self.stop_action = QAction("停止", self)
        self.play_action = QAction("播放 (F9)", self)
        self.pause_action = QAction("暂停/继续 (F9)", self)
        self.auto_click_action = QAction("连点 (F10)", self)
        self.emergency_action = QAction("紧急停止 (Esc)", self)
        # File commands collapse to icons in a narrow toolbar so the active
        # input controls, especially F10 and emergency stop, remain visible.
        for action in (
            self.new_action,
            self.open_action,
            self.save_action,
            self.save_as_action,
        ):
            action.setPriority(QAction.Priority.LowPriority)
        self.record_action.setStatusTip("开始或停止动作录制")
        self.play_action.setStatusTip("开始脚本回放")
        self.auto_click_action.setStatusTip("开始或停止鼠标连点")
        self.emergency_action.setStatusTip("立即停止当前输入任务并安全释放输入")
        icon_specs = {
            self.new_action: ("new", "#17233B"),
            self.open_action: ("open", "#17233B"),
            self.save_action: ("save", "#17233B"),
            self.save_as_action: ("save_as", "#17233B"),
            self.record_action: ("record", "#B5423B"),
            self.stop_action: ("stop", "#B5423B"),
            self.play_action: ("play", "#18825E"),
            self.pause_action: ("pause", "#8A651C"),
            self.auto_click_action: ("click", "#087C8A"),
            self.emergency_action: ("emergency", "#17233B"),
        }
        for action, (name, color) in icon_specs.items():
            action.setIcon(tool_icon(name, color))
            # Keep shortcuts owned by the main window even though the visible
            # controls are custom tool buttons instead of a QToolBar.
            self.addAction(action)

    @staticmethod
    def _action_button(
        action: QAction,
        *,
        variant: str,
        icon_only: bool = False,
    ) -> QToolButton:
        button = QToolButton()
        button.setDefaultAction(action)
        button.setProperty("variant", variant)
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonIconOnly
            if icon_only
            else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        button.setIconSize(QSize(20, 20))
        button.setAutoRaise(False)
        if icon_only:
            button.setFixedSize(36, 34)
            button.setToolTip(action.text())
            button.setAccessibleName(action.text())
        return button

    def _build_ui(self) -> None:
        self.app_header = QFrame()
        self.app_header.setObjectName("appHeader")
        self.header_layout = QGridLayout(self.app_header)
        self.header_layout.setContentsMargins(11, 8, 11, 8)
        self.header_layout.setHorizontalSpacing(11)
        self.header_layout.setVerticalSpacing(7)

        self.brand_block = QWidget()
        self.brand_block.setObjectName("brandBlock")
        brand_layout = QHBoxLayout(self.brand_block)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(9)
        self.brand_mark = QLabel()
        self.brand_mark.setObjectName("brandMark")
        self.brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_mark.setFixedSize(46, 46)
        self.brand_mark.setAccessibleName("WinActionRecorder 应用图标")
        brand_pixmap = self.windowIcon().pixmap(QSize(44, 44))
        if brand_pixmap.isNull():
            self.brand_mark.setText("WA")
        else:
            self.brand_mark.setPixmap(brand_pixmap)
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(0)
        brand_title = QLabel("WIN ACTION")
        brand_title.setObjectName("brandTitle")
        brand_title_row = QHBoxLayout()
        brand_title_row.setContentsMargins(0, 0, 0, 0)
        brand_title_row.setSpacing(7)
        brand_title_row.addWidget(brand_title)
        self.brand_version = QLabel(f"v{__version__}")
        self.brand_version.setObjectName("brandVersion")
        self.brand_version.setAccessibleName(f"应用版本 {__version__}")
        brand_title_row.addWidget(self.brand_version)
        brand_title_row.addStretch(1)
        self.brand_subtitle = QLabel("LOCAL AUTOMATION // 本地工作台")
        self.brand_subtitle.setObjectName("brandSubtitle")
        brand_copy.addLayout(brand_title_row)
        brand_copy.addWidget(self.brand_subtitle)
        brand_layout.addWidget(self.brand_mark)
        brand_layout.addLayout(brand_copy)

        self.command_deck = QWidget()
        self.command_deck.setObjectName("commandDeck")
        command_layout = QVBoxLayout(self.command_deck)
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(5)
        self.command_layout = command_layout

        self.file_deck = QWidget()
        self.file_deck.setObjectName("fileDeck")
        file_row = QHBoxLayout(self.file_deck)
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.setSpacing(5)
        file_caption = QLabel("FILE // 方案")
        file_caption.setObjectName("microLabel")
        file_row.addWidget(file_caption)
        for action in (
            self.new_action,
            self.open_action,
            self.save_action,
            self.save_as_action,
        ):
            file_row.addWidget(
                self._action_button(action, variant="file", icon_only=True)
            )
        file_row.addStretch(1)

        self.task_deck = QWidget()
        self.task_deck.setObjectName("taskDeck")
        task_row = QHBoxLayout(self.task_deck)
        task_row.setContentsMargins(0, 0, 0, 0)
        task_row.setSpacing(6)
        for action, variant in (
            (self.record_action, "record"),
            (self.stop_action, "stop"),
            (self.play_action, "play"),
            (self.pause_action, "pause"),
            (self.auto_click_action, "click"),
        ):
            task_row.addWidget(self._action_button(action, variant=variant))
        command_layout.addWidget(self.file_deck)
        command_layout.addWidget(self.task_deck)

        self.emergency_button = self._action_button(
            self.emergency_action,
            variant="emergency",
        )
        self.emergency_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        self.emergency_button.setIconSize(QSize(24, 24))

        self.action_table = ActionTable(self)
        self.action_table.setMinimumWidth(500)

        workspace = QFrame()
        workspace.setObjectName("workspacePaper")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(11, 10, 11, 11)
        workspace_layout.setSpacing(9)
        workspace_header = QHBoxLayout()
        workspace_header.setContentsMargins(2, 0, 2, 0)
        workspace_title = QLabel("ACTION TRACK // 动作轨道")
        workspace_title.setObjectName("workspaceTitle")
        self.action_count_badge = QLabel("0 个动作")
        self.action_count_badge.setProperty("badge", True)
        self.action_count_badge.setAccessibleName("动作总数")
        self.clear_button = QPushButton("清空")
        self.clear_button.setProperty("danger", True)
        self.clear_button.setToolTip("删除当前脚本中的全部动作")
        workspace_header.addWidget(workspace_title)
        workspace_header.addWidget(self.action_count_badge)
        workspace_header.addStretch(1)
        workspace_header.addWidget(self.clear_button)
        workspace_layout.addLayout(workspace_header)
        workspace_layout.addWidget(self.action_table, 1)
        self.action_workspace = workspace

        self.macro_editor = MacroEditorPane(self)
        macro_workspace = QFrame()
        macro_workspace.setObjectName("workspacePaper")
        macro_workspace_layout = QVBoxLayout(macro_workspace)
        macro_workspace_layout.setContentsMargins(11, 10, 11, 11)
        macro_workspace_layout.setSpacing(9)
        macro_header = QHBoxLayout()
        macro_title = QLabel("MACRO LAB // 自定义指令")
        macro_title.setObjectName("workspaceTitle")
        self.macro_mode_badge = QLabel("安全沙盒语法")
        self.macro_mode_badge.setProperty("badge", True)
        self.macro_mode_badge.setAccessibleName("自定义方案运行状态")
        macro_header.addWidget(macro_title)
        macro_header.addWidget(self.macro_mode_badge)
        macro_header.addStretch(1)
        macro_workspace_layout.addLayout(macro_header)
        macro_workspace_layout.addWidget(self.macro_editor, 1)
        self.macro_workspace = macro_workspace

        self.workspace_stack = QStackedWidget()
        self.workspace_stack.setObjectName("workspaceStack")
        self.workspace_stack.addWidget(self.action_workspace)
        self.workspace_stack.addWidget(self.macro_workspace)
        self.workspace_stack.setCurrentWidget(self.action_workspace)

        settings_panel = self._build_settings_panel()
        settings_panel.setMinimumWidth(306)
        settings_panel.setMaximumWidth(430)
        self.settings_panel = settings_panel

        self.workspace_splitter = QSplitter()
        self.workspace_splitter.setObjectName("workspace_splitter")
        self.workspace_splitter.addWidget(self.workspace_stack)
        self.workspace_splitter.addWidget(settings_panel)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setSizes([820, 380])

        central = QWidget()
        central.setObjectName("centralCanvas")
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(11, 9, 11, 6)
        central_layout.setSpacing(8)
        privacy_banner = QFrame()
        privacy_banner.setObjectName("privacyBanner")
        privacy_layout = QHBoxLayout(privacy_banner)
        privacy_layout.setContentsMargins(10, 7, 10, 7)
        privacy_layout.setSpacing(8)
        privacy_icon = QLabel()
        privacy_icon.setPixmap(tool_icon("privacy", "#17233B").pixmap(20, 20))
        privacy_icon.setAccessibleName("隐私警告")
        warning = QLabel(
            "LOCAL ONLY // 只在本机保存；录制时请避开密码、聊天内容等敏感输入"
        )
        warning.setWordWrap(True)
        warning.setProperty("tone", "warning")
        privacy_layout.addWidget(privacy_icon, 0, Qt.AlignmentFlag.AlignTop)
        privacy_layout.addWidget(warning, 1)
        central_layout.addWidget(self.app_header)
        central_layout.addWidget(privacy_banner)
        central_layout.addWidget(self.workspace_splitter, 1)
        self.setCentralWidget(central)

        self._build_status_bar()
        self._update_responsive_layout(force=True)

    def _update_responsive_layout(self, *, force: bool = False) -> None:
        """Reflow the command deck after resize, DPI, or monitor changes."""

        compact = self.width() < 1120
        if not force and compact is self._header_compact:
            return
        self._header_compact = compact
        focused_widget = QApplication.focusWidget()
        focused_header_widget = (
            focused_widget
            if isinstance(focused_widget, QWidget)
            and (
                focused_widget is self.app_header
                or self.app_header.isAncestorOf(focused_widget)
            )
            else None
        )

        for widget in (
            self.brand_block,
            self.file_deck,
            self.command_deck,
            self.emergency_button,
        ):
            self.header_layout.removeWidget(widget)
        self.command_layout.removeWidget(self.file_deck)
        self.command_layout.removeWidget(self.task_deck)

        if compact:
            # At 960px the file deck shares the brand row.  Keeping only the
            # five task controls on row two turns the former three-tier header
            # into a compact two-tier command bar without hiding any action.
            self.header_layout.addWidget(self.brand_block, 0, 0)
            self.header_layout.addWidget(
                self.file_deck,
                0,
                1,
                alignment=Qt.AlignmentFlag.AlignVCenter,
            )
            self.header_layout.addWidget(
                self.emergency_button,
                0,
                2,
                alignment=Qt.AlignmentFlag.AlignRight,
            )
            self.command_layout.addWidget(self.task_deck)
            self.header_layout.addWidget(self.command_deck, 1, 0, 1, 3)
            self.header_layout.setColumnStretch(0, 1)
            self.header_layout.setColumnStretch(1, 1)
            self.header_layout.setColumnStretch(2, 0)
            self.emergency_button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            self.emergency_button.setMinimumSize(154, 40)
            self.settings_panel.setMaximumWidth(340)
            self.brand_subtitle.setText("LOCAL AUTOMATION // 本地工作台")
        else:
            self.command_layout.addWidget(self.file_deck)
            self.command_layout.addWidget(self.task_deck)
            self.header_layout.addWidget(self.brand_block, 0, 0)
            self.header_layout.addWidget(self.command_deck, 0, 1)
            self.header_layout.addWidget(self.emergency_button, 0, 2)
            self.header_layout.setColumnStretch(0, 0)
            self.header_layout.setColumnStretch(1, 1)
            self.header_layout.setColumnStretch(2, 0)
            self.emergency_button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextUnderIcon
            )
            self.emergency_button.setMinimumSize(126, 64)
            self.settings_panel.setMaximumWidth(430)
            self.brand_subtitle.setText("LOCAL AUTOMATION // 本地工作台")

        self.app_header.updateGeometry()
        if self.centralWidget() is not None:
            self.centralWidget().updateGeometry()
        if (
            focused_header_widget is not None
            and focused_header_widget.isEnabled()
            and not focused_header_widget.isHidden()
        ):
            # Reparenting file_deck between the header grid and command deck
            # makes Qt choose a different focus child. Preserve the user's
            # exact command so maximize/restore cannot turn Enter into Record.
            focused_header_widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def _schedule_responsive_update(self) -> None:
        if not hasattr(self, "header_layout") or self._responsive_update_pending:
            return
        self._responsive_update_pending = True
        QTimer.singleShot(0, self._run_responsive_update)

    def _run_responsive_update(self) -> None:
        self._responsive_update_pending = False
        if self._closing:
            return
        self._update_responsive_layout()
        for index in range(self.settings_tabs.count()):
            page = self.settings_tabs.widget(index)
            page.updateGeometry()
        if (
            self.auto_click_location_combo.currentData()
            == ClickLocationMode.FIXED.value
        ):
            # Startup settings and monitor moves can reflow a taller page
            # after the original mode-change callback has already run.
            QTimer.singleShot(0, self._ensure_auto_click_picker_visible)

    def _on_window_screen_changed(self, _screen: object) -> None:
        # Qt has already converted the window to the new screen's logical
        # coordinate system when this signal fires.  A queued reflow lets new
        # font/DPI metrics settle before size hints are evaluated.
        self._schedule_responsive_update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_responsive_update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and handle is not self._screen_signal_handle:
            handle.screenChanged.connect(self._on_window_screen_changed)
            self._screen_signal_handle = handle
        self._schedule_responsive_update()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.WindowStateChange,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
        }:
            self._schedule_responsive_update()

    @staticmethod
    def _configure_form(form: QFormLayout) -> None:
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

    @staticmethod
    def _scroll_page(content: QWidget, accessible_name: str) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setAccessibleName(accessible_name)
        scroll.setAccessibleDescription(
            "内容较长时可用鼠标滚轮或聚焦其中的控件后滚动；不会产生横向滚动。"
        )
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scroll.setWidget(content)
        return scroll

    def _build_settings_panel(self) -> QTabWidget:
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setObjectName("settingsTabs")
        self.settings_tabs.setAccessibleName("任务控制台")
        self.settings_tabs.setDocumentMode(True)
        self._settings_pages: dict[str, QWidget] = {}
        self._register_settings_page("playback", "回放", self._build_playback_page())
        self._register_settings_page("autoclick", "连点", self._build_auto_click_page())
        self._register_settings_page("recording", "录制", self._build_recording_page())
        self._register_settings_page(
            "custom",
            "自定义",
            self._build_custom_page_placeholder(),
        )
        return self.settings_tabs

    def _register_settings_page(
        self,
        key: str,
        title: str,
        page: QWidget,
    ) -> int:
        """Register or replace a right-console page while preserving order."""

        existing = self._settings_pages.get(key)
        if existing is None:
            index = self.settings_tabs.addTab(page, title)
        else:
            index = self.settings_tabs.indexOf(existing)
            current = self.settings_tabs.currentIndex()
            self.settings_tabs.removeTab(index)
            existing.setParent(None)
            self.settings_tabs.insertTab(index, page, title)
            if current == index:
                self.settings_tabs.setCurrentIndex(index)
        page.setProperty("settingsPageKey", key)
        self._settings_pages[key] = page
        return index

    def _build_custom_page_placeholder(self) -> QScrollArea:
        """Build the compact reference console for the macro editor."""

        panel = QWidget()
        panel.setObjectName("settingsPage")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 3, 9, 9)
        layout.setSpacing(8)

        intro = QLabel("像写游戏指令一样，组合鼠标、按键、等待和循环")
        intro.setObjectName("pageIntro")

        command_group = QGroupBox("指令速查")
        command_group.setProperty("accent", "sky")
        command_layout = QVBoxLayout(command_group)
        command_copy = QLabel(
            "mouse left[last:10ms]\n"
            "  按住 10ms 后释放\n"
            "mouse left[count:2, interval:80ms]\n"
            "  本条双击与间隔\n"
            "mouse left[after:200ms]\n"
            "  最后释放后等待\n"
            "mouse right[...] at X Y\n"
            "mouse move X Y · mouse scroll DX DY\n"
            "key f[last:80ms] · key down/up ctrl\n"
            "hotkey ctrl shift s[last:80ms]\n"
            "let gap = 0.5s · next gap random gap / 2\n"
            "function burst(times, gap) { ... }\n"
            "call burst(5, 120ms) · repeat N { } · while true { }"
        )
        command_copy.setObjectName("placeholderTerminal")
        command_copy.setWordWrap(True)
        command_copy.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        command_copy.setAccessibleName("自定义指令速查")
        command_layout.addWidget(command_copy)

        scheme_group = QGroupBox("方案与安全")
        scheme_group.setProperty("accent", "mint")
        scheme_layout = QVBoxLayout(scheme_group)
        scheme_copy = QLabel(
            "内置示例不会被覆盖；修改后可“另存”为自己的方案。\n\n"
            "自定义页可用 F9 运行、暂停或继续当前方案。数值表达式由内置安全解析器"
            "编译，不使用 eval，也不提供文件或网络访问；持续循环可用停止按钮或 Esc "
            "中止，并在退出前释放已按下的鼠标和键盘输入。"
        )
        scheme_copy.setWordWrap(True)
        scheme_copy.setProperty("tone", "muted")
        scheme_layout.addWidget(scheme_copy)

        self.macro_console_status_label = QLabel("选择左侧方案，检查后即可运行")
        self.macro_console_status_label.setObjectName("macroValidationStatus")
        self.macro_console_status_label.setProperty("state", "idle")
        self.macro_console_status_label.setAccessibleName("自定义方案状态")
        self.macro_console_status_label.setWordWrap(True)

        layout.addWidget(intro)
        layout.addWidget(self.macro_console_status_label)
        layout.addWidget(command_group)
        layout.addWidget(scheme_group)
        layout.addStretch(1)
        return self._scroll_page(panel, "自定义指令参考")

    def _build_playback_page(self) -> QScrollArea:
        panel = QWidget()
        panel.setObjectName("settingsPage")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 3, 9, 9)
        layout.setSpacing(7)

        page_intro = QLabel("按自己的节奏，把动作稳稳地重放")
        page_intro.setObjectName("pageIntro")
        playback_group = QGroupBox("脚本回放")
        playback_group.setProperty("accent", "mint")
        playback_form = QFormLayout(playback_group)
        self._configure_form(playback_form)
        self.loop_count_spin = QSpinBox()
        self.loop_count_spin.setRange(1, 1_000_000)
        self.loop_count_spin.setValue(1)
        self.infinite_checkbox = QCheckBox("无限循环")
        self.infinite_checkbox.setToolTip("启用后忽略固定循环次数，直到手动停止")
        self.loop_interval_spin = QDoubleSpinBox()
        self.loop_interval_spin.setRange(0.0, 3600.0)
        self.loop_interval_spin.setDecimals(1)
        self.loop_interval_spin.setSuffix(" 秒")
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 10.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setDecimals(1)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setSuffix(" ×")
        self.playback_countdown_spin = QSpinBox()
        self.playback_countdown_spin.setRange(0, 60)
        self.playback_countdown_spin.setValue(3)
        self.playback_countdown_spin.setSuffix(" 秒")
        self.ignore_mouse_moves_checkbox = QCheckBox("跳过鼠标移动轨迹")
        self.ignore_mouse_moves_checkbox.setToolTip(
            "仅在回放时跳过鼠标移动并保留点击位置；不会修改动作列表或启用状态，"
            "取消勾选即可恢复。"
        )
        self.ignore_mouse_moves_checkbox.setAccessibleName("跳过鼠标移动轨迹")
        self.ignore_mouse_moves_checkbox.setAccessibleDescription(
            "跳过鼠标移动及其延迟，保留点击和滚轮的记录位置；"
            "不会修改动作列表或启用状态，取消勾选即可恢复。"
        )
        self.skip_move_click_interval_spin = QSpinBox()
        self.skip_move_click_interval_spin.setRange(0, 60_000)
        self.skip_move_click_interval_spin.setSingleStep(50)
        self.skip_move_click_interval_spin.setValue(200)
        self.skip_move_click_interval_spin.setSuffix(" ms")
        self.skip_move_click_interval_spin.setSpecialValueText("不限制")
        self.skip_move_click_interval_spin.setAccessibleName("跳过轨迹时点击间隔")
        click_interval_description = (
            "跳过鼠标移动轨迹时，从一次鼠标手势的最后一个按钮释放完成到下一次新手势按下的最小等待；"
            "不会改变鼠标按住时长，也不随播放速度倍率缩放。"
            "设置较大时，双击可能变为两次单击。"
        )
        self.skip_move_click_interval_spin.setToolTip(click_interval_description)
        self.skip_move_click_interval_spin.setAccessibleDescription(
            click_interval_description
        )
        self.ignore_mouse_moves_hint = QLabel("移动轨迹：已启用 0 / 共 0")
        self.ignore_mouse_moves_hint.setWordWrap(True)
        self.ignore_mouse_moves_hint.setProperty("tone", "muted")
        playback_form.addRow("固定循环次数", self.loop_count_spin)
        playback_form.addRow("", self.infinite_checkbox)
        playback_form.addRow("循环间隔", self.loop_interval_spin)
        playback_form.addRow("播放速度", self.speed_spin)
        playback_form.addRow("", self.ignore_mouse_moves_checkbox)
        playback_form.addRow("点击间隔", self.skip_move_click_interval_spin)
        playback_form.addRow("", self.ignore_mouse_moves_hint)
        playback_form.addRow("开始前倒计时", self.playback_countdown_spin)

        playback_hint = QLabel(
            "F9 在回放页控制普通脚本；切到自定义页则运行、暂停或继续当前方案。"
            "Esc 可随时紧急停止。"
        )
        playback_hint.setWordWrap(True)
        playback_hint.setProperty("tone", "muted")

        layout.addWidget(page_intro)
        layout.addWidget(playback_group)
        layout.addWidget(playback_hint)
        layout.addStretch(1)
        return self._scroll_page(panel, "回放设置")

    def _build_recording_page(self) -> QScrollArea:
        panel = QWidget()
        panel.setObjectName("settingsPage")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 3, 9, 9)
        layout.setSpacing(7)

        page_intro = QLabel("先留出倒计时，再开始捕捉每一步")
        page_intro.setObjectName("pageIntro")
        recording_group = QGroupBox("动作录制")
        recording_group.setProperty("accent", "peach")
        recording_form = QFormLayout(recording_group)
        self._configure_form(recording_form)
        self.record_countdown_spin = QSpinBox()
        self.record_countdown_spin.setRange(0, 60)
        self.record_countdown_spin.setValue(3)
        self.record_countdown_spin.setSuffix(" 秒")
        self.sample_interval_spin = QSpinBox()
        self.sample_interval_spin.setRange(1, 1000)
        self.sample_interval_spin.setValue(20)
        self.sample_interval_spin.setSuffix(" ms")
        recording_form.addRow("开始前倒计时", self.record_countdown_spin)
        recording_form.addRow("鼠标移动采样", self.sample_interval_spin)

        shortcut_group = QGroupBox("全局快捷键")
        shortcut_group.setProperty("accent", "sky")
        shortcut_layout = QVBoxLayout(shortcut_group)
        shortcut_hint = QLabel(
            "F8  开始/停止录制\nF9  当前页运行/暂停/继续\n"
            "F10 开始/停止连点\nEsc 紧急停止"
        )
        shortcut_hint.setProperty("tone", "muted")
        shortcut_layout.addWidget(shortcut_hint)

        layout.addWidget(page_intro)
        layout.addWidget(recording_group)
        layout.addWidget(shortcut_group)
        layout.addStretch(1)
        return self._scroll_page(panel, "录制设置")

    def _build_auto_click_page(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("settingsPage")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(9, 3, 9, 9)
        layout.setSpacing(7)

        page_intro = QLabel("选好目标，让每一次点击都有明确落点")
        page_intro.setObjectName("pageIntro")
        layout.addWidget(page_intro)

        self.auto_click_status_label = QLabel("未运行 · 已完成 0 / 10")
        self.auto_click_status_label.setProperty("state", "idle")
        self.auto_click_status_label.setAccessibleName("鼠标连点状态")
        self.auto_click_status_label.setWordWrap(True)
        layout.addWidget(self.auto_click_status_label)

        click_group = QGroupBox("点击参数")
        click_group.setProperty("accent", "sky")
        click_form = QFormLayout(click_group)
        self._configure_form(click_form)
        self.auto_click_button_combo = QComboBox()
        self.auto_click_button_combo.addItem("左键", "left")
        self.auto_click_button_combo.addItem("右键", "right")
        self.auto_click_button_combo.addItem("中键", "middle")
        self.auto_click_interval_spin = QSpinBox()
        self.auto_click_interval_spin.setRange(10, 60_000)
        self.auto_click_interval_spin.setValue(100)
        self.auto_click_interval_spin.setSingleStep(10)
        self.auto_click_interval_spin.setSuffix(" ms")
        self.auto_click_interval_spin.setToolTip(
            "一次点击释放完成到下一次按下之间的等待时间"
        )
        self.auto_click_count_spin = QSpinBox()
        self.auto_click_count_spin.setRange(1, 1_000_000)
        self.auto_click_count_spin.setValue(10)
        self.auto_click_infinite_checkbox = QCheckBox("无限连点")
        self.auto_click_countdown_spin = QSpinBox()
        self.auto_click_countdown_spin.setRange(0, 60)
        self.auto_click_countdown_spin.setValue(3)
        self.auto_click_countdown_spin.setSuffix(" 秒")
        click_form.addRow("鼠标按钮", self.auto_click_button_combo)
        click_form.addRow("点击间隔", self.auto_click_interval_spin)
        click_form.addRow("点击次数", self.auto_click_count_spin)
        click_form.addRow("", self.auto_click_infinite_checkbox)
        click_form.addRow("开始前倒计时", self.auto_click_countdown_spin)

        location_group = QGroupBox("目标位置")
        location_group.setProperty("accent", "peach")
        location_form = QFormLayout(location_group)
        self._configure_form(location_form)
        self.auto_click_location_combo = QComboBox()
        self.auto_click_location_combo.addItem(
            "跟随当前光标", ClickLocationMode.FOLLOW_CURSOR.value
        )
        self.auto_click_location_combo.addItem(
            "固定坐标", ClickLocationMode.FIXED.value
        )
        self.auto_click_x_spin = QSpinBox()
        self.auto_click_y_spin = QSpinBox()
        for spin, name in (
            (self.auto_click_x_spin, "固定坐标 X"),
            (self.auto_click_y_spin, "固定坐标 Y"),
        ):
            spin.setRange(-(2**31), 2**31 - 1)
            spin.setAccessibleName(name)
        self.capture_cursor_button = QPushButton("在屏幕上选取位置")
        self.capture_cursor_button.setIcon(tool_icon("target", "#087C8A"))
        self.capture_cursor_button.setIconSize(QSize(20, 20))
        self.capture_cursor_button.setProperty("role", "picker")
        self.capture_cursor_button.setToolTip(
            "打开全屏半透明取点层；在目标位置单击，按 Esc 取消"
        )
        self.capture_cursor_button.setAccessibleDescription(
            "屏幕变暗后，在需要自动点击的位置单击；按 Esc 可取消"
        )
        self.auto_click_adapt_checkbox = QCheckBox("切屏后按当前画面等比适配")
        self.auto_click_adapt_checkbox.setChecked(True)
        self.auto_click_adapt_checkbox.setToolTip(
            "仅对全屏取点记录的位置生效。进入限制鼠标范围的全屏程序后，"
            "会按原屏幕内的相对位置映射到当前可用区域；手动坐标和普通脚本回放不变。"
        )
        self.auto_click_adapt_checkbox.setAccessibleDescription(
            "使用全屏取点后，如果切换到锁定鼠标范围的全屏画面，"
            "保持目标在画面中的相对位置。"
        )
        self.auto_click_location_hint = QLabel(
            "跟随模式不会主动移动鼠标，每次点击使用当时的光标位置。"
        )
        self.auto_click_location_hint.setWordWrap(True)
        self.auto_click_location_hint.setProperty("tone", "muted")
        location_form.addRow("位置模式", self.auto_click_location_combo)
        # Separate rows keep full signed 32-bit coordinates readable in the
        # minimum-width window and avoid a hidden horizontal overflow.
        location_form.addRow("固定坐标 X", self.auto_click_x_spin)
        location_form.addRow("固定坐标 Y", self.auto_click_y_spin)
        location_form.addRow("", self.capture_cursor_button)
        location_form.addRow("", self.auto_click_adapt_checkbox)
        location_form.addRow("", self.auto_click_location_hint)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.auto_click_start_button = QPushButton("开始连点")
        self.auto_click_start_button.setProperty("role", "primary")
        self.auto_click_start_button.setIcon(tool_icon("click", "#17233B"))
        self.auto_click_start_button.setAccessibleDescription(
            "使用当前连点参数启动；全局快捷键 F10 也可开始或停止"
        )
        self.auto_click_pause_button = QPushButton("暂停")
        self.auto_click_pause_button.setIcon(tool_icon("pause", "#556377"))
        self.auto_click_stop_button = QPushButton("停止")
        self.auto_click_stop_button.setProperty("role", "danger")
        self.auto_click_stop_button.setIcon(tool_icon("stop", "#B5423B"))
        controls.addWidget(self.auto_click_start_button, 2)
        controls.addWidget(self.auto_click_pause_button, 1)
        controls.addWidget(self.auto_click_stop_button, 1)

        safety_hint = QLabel("F10 开始/停止 · Esc 紧急释放鼠标")
        safety_hint.setWordWrap(False)
        safety_hint.setProperty("tone", "muted")

        layout.addWidget(location_group)
        layout.addWidget(click_group)
        layout.addStretch(1)

        footer = QFrame()
        footer.setObjectName("autoClickFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(8, 6, 8, 6)
        footer_layout.setSpacing(4)
        footer_layout.addLayout(controls)
        footer_layout.addWidget(safety_hint)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        self.auto_click_scroll_area = self._scroll_page(panel, "鼠标连点参数")
        page_layout.addWidget(self.auto_click_scroll_area, 1)
        page_layout.addWidget(footer)
        return page

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)
        self.state_label = QLabel("状态：空闲")
        self.state_label.setProperty("state", "idle")
        self.state_label.setAccessibleName("应用状态")
        self.loop_label = QLabel("循环：0/1")
        self.loop_label.setProperty("badge", True)
        self.action_label = QLabel("动作：0/0")
        self.action_label.setProperty("badge", True)
        self.status_message_label = QLabel("就绪")
        self.status_message_label.setMinimumWidth(120)
        self.status_message_label.setAccessibleName("状态消息")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedSize(132, 18)
        self.progress_bar.setAccessibleName("任务进度")
        bar.addWidget(self.status_message_label, 1)
        for widget in (
            self.state_label,
            self.loop_label,
            self.action_label,
            self.progress_bar,
        ):
            bar.addPermanentWidget(widget)

    def _connect_signals(self) -> None:
        self.new_action.triggered.connect(self._new_script)
        self.open_action.triggered.connect(self._open_script)
        self.save_action.triggered.connect(self._save_script)
        self.save_as_action.triggered.connect(lambda: self._save_script(save_as=True))
        self.record_action.triggered.connect(self._toggle_recording)
        self.stop_action.triggered.connect(self._stop_current_operation)
        self.play_action.triggered.connect(self._play_hotkey)
        self.pause_action.triggered.connect(self._toggle_playback_pause)
        self.auto_click_action.triggered.connect(self._auto_click_hotkey)
        self.emergency_action.triggered.connect(self._emergency_stop)
        self.clear_button.clicked.connect(self._clear_actions)
        self.infinite_checkbox.toggled.connect(
            lambda checked: self.loop_count_spin.setEnabled(not checked)
        )
        self.ignore_mouse_moves_checkbox.toggled.connect(
            self._on_ignore_mouse_moves_toggled
        )
        self.auto_click_infinite_checkbox.toggled.connect(
            self._on_auto_click_infinite_toggled
        )
        self.auto_click_count_spin.valueChanged.connect(
            self._on_auto_click_count_changed
        )
        self.auto_click_location_combo.currentIndexChanged.connect(
            self._update_auto_click_location_controls
        )
        self.auto_click_x_spin.valueChanged.connect(self._on_fixed_coordinate_edited)
        self.auto_click_y_spin.valueChanged.connect(self._on_fixed_coordinate_edited)
        self.auto_click_adapt_checkbox.toggled.connect(
            self._update_auto_click_location_controls
        )
        self.capture_cursor_button.clicked.connect(self._capture_cursor_position)
        self.auto_click_start_button.clicked.connect(self._start_auto_click)
        self.auto_click_pause_button.clicked.connect(self._toggle_auto_click_pause)
        self.auto_click_stop_button.clicked.connect(self._stop_auto_click)
        self._coordinate_picker.coordinate_picked.connect(self._on_coordinate_picked)
        self._coordinate_picker.cancelled.connect(self._on_coordinate_pick_cancelled)
        self._coordinate_picker.failed.connect(self._on_coordinate_pick_failed)
        self.settings_tabs.currentChanged.connect(self._on_settings_tab_changed)

        self.macro_editor.source_changed.connect(self._on_macro_source_changed)
        self.macro_editor.scheme_selected.connect(self._on_macro_scheme_selected)
        self.macro_editor.new_requested.connect(self._new_macro_scheme)
        self.macro_editor.save_requested.connect(self._save_macro_scheme)
        self.macro_editor.save_as_requested.connect(self._save_macro_scheme_as)
        self.macro_editor.delete_requested.connect(self._delete_macro_scheme)
        self.macro_editor.validate_requested.connect(self._validate_macro_source)
        self.macro_editor.run_requested.connect(self._start_macro)
        self.macro_editor.pause_requested.connect(self._toggle_macro_pause)
        self.macro_editor.stop_requested.connect(self._stop_macro)

        self.action_table.enabled_changed.connect(self._set_action_enabled)
        self.action_table.edit_requested.connect(self._edit_action)
        self.action_table.delete_requested.connect(self._delete_action)
        self.action_table.move_requested.connect(self._move_action)

        self._bridge.action_recorded.connect(self._on_action_recorded)
        self._bridge.recorder_error.connect(self._on_recorder_error)
        self._bridge.hotkey_error.connect(self._on_hotkey_error)
        self._bridge.record_hotkey.connect(self._toggle_recording)
        self._bridge.play_hotkey.connect(self._play_hotkey)
        self._bridge.auto_click_hotkey.connect(self._auto_click_hotkey)
        self._bridge.emergency_hotkey.connect(self._emergency_stop)
        self._bridge.playback_event.connect(self._on_playback_event)
        self._bridge.macro_event.connect(self._on_macro_event)
        self._bridge.auto_click_event.connect(self._on_auto_click_event)
        self._bridge.state_changed.connect(self._apply_state)

    # ----------------------------------------------------------- file model
    def _new_script(self) -> None:
        if not self._confirm_discard_changes():
            return
        self._actions.clear()
        self._script_path = None
        self._screen_config = None
        self._created_at = utc_now_iso()
        self._set_dirty(False)
        self._refresh_actions()
        self._state_manager.reset("已新建空白脚本")

    def _open_script(self) -> None:
        if not self._confirm_discard_changes():
            return
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "打开动作脚本",
            str(self._script_path.parent if self._script_path else Path.home()),
            "动作脚本 (*.json);;所有文件 (*.*)",
        )
        if not filename:
            return
        try:
            script = load_script(filename)
        except StorageError as exc:
            self._show_error("无法打开脚本", str(exc))
            return
        self._actions = [action.clone() for action in script.actions]
        self._screen_config = dict(script.screen_config)
        self._created_at = script.created_at
        self._script_path = Path(filename)
        interval = script.recording_settings.get(
            "mouse_move_sample_interval_ms",
            script.recording_settings.get("move_sample_interval_ms", 20),
        )
        if isinstance(interval, int) and not isinstance(interval, bool):
            self.sample_interval_spin.setValue(max(1, min(1000, interval)))
        self._set_dirty(False)
        self._refresh_actions()
        self._state_manager.reset(f"已加载 {len(self._actions)} 个动作")

    def _save_script(self, _checked: bool = False, *, save_as: bool = False) -> bool:
        target = None if save_as else self._script_path
        if target is None:
            filename, _ = QFileDialog.getSaveFileName(
                self,
                "保存动作脚本",
                str(self._script_path or (Path.home() / "actions.json")),
                "动作脚本 (*.json);;所有文件 (*.*)",
            )
            if not filename:
                return False
            target = Path(filename)
            if target.suffix.lower() != ".json":
                target = target.with_suffix(".json")
        try:
            screen_config = self._screen_config or capture_screen_config(
                QApplication.instance()
            )
            save_script(
                target,
                self._actions,
                screen_config=screen_config,
                recording_settings={
                    "mouse_move_sample_interval_ms": self.sample_interval_spin.value()
                },
                app_version=__version__,
                created_at=self._created_at,
            )
        except (StorageError, ScreenConfigError, TypeError, ValueError) as exc:
            self._show_error("无法保存脚本", str(exc))
            return False
        self._screen_config = screen_config
        self._script_path = target
        self._set_dirty(False)
        self.status_message_label.setText("脚本已保存")
        return True

    # --------------------------------------------------------------- record
    def _toggle_recording(self) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or self._coordinate_picker.is_active
        ):
            return
        if self._recorder.is_recording or self._record_countdown_timer.isActive():
            self._stop_recording()
            return
        if (
            self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        ):
            self.status_message_label.setText("已有输入任务运行，不能开始录制")
            return
        if self._actions:
            answer = QMessageBox.question(
                self,
                "替换现有动作？",
                "开始新录制会替换当前动作列表。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        try:
            pending_screen = capture_screen_config(QApplication.instance())
        except ScreenConfigError as exc:
            self._show_error("无法开始录制", str(exc))
            return
        self._pending_recording_screen_config = pending_screen
        self._pending_recording_created_at = utc_now_iso()
        countdown = self.record_countdown_spin.value()
        if countdown:
            self._record_countdown_remaining = countdown
            self._state_manager.transition(
                AppState.COUNTDOWN,
                message=f"录制将在 {countdown} 秒后开始；Esc 可取消",
                current_loop=0,
                total_loops=1,
                current_action=0,
                total_actions=len(self._actions),
                progress=0.0,
            )
            self._record_countdown_timer.start()
            self._apply_state(self._state_manager.snapshot())
        else:
            self._begin_recording_now()

    def _record_countdown_tick(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        self._record_countdown_remaining -= 1
        if self._record_countdown_remaining <= 0:
            self._record_countdown_timer.stop()
            self._begin_recording_now()
            return
        self._state_manager.update_progress(
            message=f"录制将在 {self._record_countdown_remaining} 秒后开始；Esc 可取消"
        )

    def _begin_recording_now(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if (
            self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        ):
            self._record_countdown_timer.stop()
            self._state_manager.transition(
                AppState.STOPPED,
                message="已有输入任务运行，录制已取消",
                force=True,
            )
            return
        self._recording_session += 1
        try:
            self._recorder.configure_mouse_sampling(
                interval_ms=self.sample_interval_spin.value()
            )
            self._recorder.start()
        except (RecorderError, ValueError) as exc:
            self._pending_recording_screen_config = None
            self._pending_recording_created_at = None
            self._state_manager.transition(AppState.ERROR, message=str(exc), force=True)
            self._show_error("无法开始录制", str(exc))
            return
        self._screen_config = self._pending_recording_screen_config
        self._created_at = self._pending_recording_created_at or utc_now_iso()
        self._pending_recording_screen_config = None
        self._pending_recording_created_at = None
        self._actions.clear()
        self._set_dirty(True)
        self._refresh_actions()
        self._state_manager.transition(
            AppState.RECORDING,
            message="正在录制；按 F8 停止，Esc 紧急停止",
            current_action=0,
            total_actions=0,
            progress=0.0,
        )

    def _stop_recording(self, *, emergency: bool = False) -> None:
        if self._record_countdown_timer.isActive():
            self._record_countdown_timer.stop()
            self._record_countdown_remaining = 0
            self._pending_recording_screen_config = None
            self._pending_recording_created_at = None
            self._state_manager.transition(
                AppState.STOPPED,
                message="录制倒计时已取消" if emergency else "录制已取消",
                force=True,
            )
            return
        if not self._recorder.is_recording:
            return
        recorded = self._recorder.stop()
        self._record_status_timer.stop()
        self._actions = recorded
        recalculate_offsets(self._actions)
        self._set_dirty(True)
        self._refresh_actions()
        self._state_manager.transition(
            AppState.STOPPED,
            message=("已紧急停止录制" if emergency else "录制已停止"),
            current_action=len(self._actions),
            total_actions=len(self._actions),
            progress=0.0,
            force=True,
        )

    def _emit_recorded_action(self, action: Action) -> None:
        """Tag worker events so queued callbacks cannot cross recording sessions."""
        self._bridge.action_recorded.emit(action, self._recording_session)

    def _on_action_recorded(self, action: Action, session: int) -> None:
        if session != self._recording_session or not self._recorder.is_recording:
            return
        self._actions.append(action)
        self._action_inventory_cache = None
        if not self._dirty:
            self._set_dirty(True)
        # Rebinding and repainting the action view for every 20 ms mouse sample
        # is needless UI churn. Keep capture responsive and publish the full
        # virtual model once recording stops; refresh only lightweight status
        # while the listener is live.
        if self._recorder.is_recording and not self._record_status_timer.isActive():
            self._record_status_timer.start()

    def _flush_recording_status(self) -> None:
        if not self._recorder.is_recording:
            return
        self._state_manager.update_progress(
            current_action=len(self._actions),
            total_actions=len(self._actions),
            message=f"已录制 {len(self._actions)} 个动作",
        )

    def _on_recorder_error(self, message: str) -> None:
        if self._closing:
            return
        start_failed = (
            not self._recorder.is_recording
            and self._pending_recording_screen_config is not None
        )
        if self._recorder.is_recording:
            self._actions = self._recorder.stop()
        elif not start_failed:
            self._actions = self._recorder.actions
        self._record_status_timer.stop()
        recalculate_offsets(self._actions)
        self._refresh_actions()
        self._state_manager.transition(AppState.ERROR, message=message, force=True)
        self._show_error("录制器错误", message)

    # ------------------------------------------------------------- playback
    def _start_playback(self) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or self._coordinate_picker.is_active
        ):
            return
        if self._recorder.is_recording or self._record_countdown_timer.isActive():
            self.status_message_label.setText("录制期间不能开始回放")
            return
        if self._autoclicker.is_running or self._macro_runner.is_running:
            self.status_message_label.setText("已有输入任务运行，不能开始脚本回放")
            return
        if self._player.is_running:
            return
        enabled_actions = self._playable_actions()
        if not enabled_actions:
            message = (
                "当前已启用的动作全部是鼠标移动轨迹。请取消“跳过鼠标移动轨迹”，"
                "或启用其他动作。"
                if self.ignore_mouse_moves_checkbox.isChecked()
                and any(action.enabled for action in self._actions)
                else "请先录制、加载或启用至少一个动作。"
            )
            QMessageBox.information(self, "没有可回放动作", message)
            return
        try:
            current_screen = capture_screen_config(QApplication.instance())
            recorded_screen = self._screen_config or current_screen
            comparison = compare_screen_configs(recorded_screen, current_screen)
            if comparison.changed and not self._confirm_screen_mismatch(
                comparison.warning_message
            ):
                self.status_message_label.setText("已取消回放")
                return
            validate_action_coordinates(enabled_actions, current_screen)
            settings = PlaybackSettings(
                countdown_seconds=float(self.playback_countdown_spin.value()),
                loop_count=self.loop_count_spin.value(),
                infinite=self.infinite_checkbox.isChecked(),
                loop_interval_ms=self.loop_interval_spin.value() * 1000.0,
                speed=self.speed_spin.value(),
                ignore_mouse_moves=self.ignore_mouse_moves_checkbox.isChecked(),
                skip_move_click_interval_ms=(
                    self.skip_move_click_interval_spin.value()
                ),
            )
            self._player.start(self._actions, settings)
        except (ScreenConfigError, ValueError, RuntimeError) as exc:
            self._state_manager.transition(AppState.ERROR, message=str(exc), force=True)
            self._show_error("无法开始回放", str(exc))

    def _toggle_playback_pause(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if not self._player.is_running:
            return
        self._player.toggle_pause()
        self._apply_state(self._state_manager.snapshot())

    def _play_hotkey(self, _checked: bool = False) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or self._coordinate_picker.is_active
        ):
            return
        if self._macro_runner.is_running:
            if self._macro_phase not in {"stopping", "finishing"}:
                self._toggle_macro_pause()
        elif self._player.is_running:
            self._toggle_playback_pause()
        elif (
            not self._recorder.is_recording
            and not self._record_countdown_timer.isActive()
            and not self._autoclicker.is_running
        ):
            if self._custom_mode_selected():
                self._start_macro()
            else:
                self._start_playback()

    def _on_playback_event(self, event: PlaybackEvent) -> None:
        if event.kind == "error" and event.error and not self._closing:
            self._show_error("回放错误", event.error)
        if event.kind == "finished":
            self._input_worker_completion_timer.start()
        self._apply_state(self._state_manager.snapshot())

    # --------------------------------------------------------------- macro
    def _reload_macro_schemes(self, selected_id: str | None = None) -> None:
        load_warning: str | None = None
        try:
            schemes = self._macro_store.list_schemes()
        except (SchemeStorageError, SchemeValidationError) as exc:
            # A damaged user file must not make the safe built-in examples
            # disappear. Keep the editor usable and surface the local error.
            schemes = BUILTIN_SCHEMES
            load_warning = f"用户方案读取失败；已保留内置示例：{exc}"
        self._macro_schemes = {scheme.id: scheme for scheme in schemes}
        selected = selected_id or self._macro_active_scheme_id
        if selected not in self._macro_schemes:
            selected = schemes[0].id if schemes else None
        self.macro_editor.set_scheme_choices(
            (
                SchemeChoice(scheme.id, scheme.name, built_in=scheme.builtin)
                for scheme in schemes
            ),
            selected_id=selected,
        )
        if selected is not None:
            self._load_macro_scheme(selected)
        if load_warning is not None:
            self._set_macro_status(load_warning, state="error")

    def _load_macro_scheme(self, scheme_id: str) -> bool:
        scheme = self._macro_schemes.get(scheme_id)
        if scheme is None:
            try:
                scheme = self._macro_store.load(scheme_id)
            except (SchemeStorageError, SchemeValidationError) as exc:
                self._set_macro_status(f"无法加载方案：{exc}", state="error")
                return False
            self._macro_schemes[scheme.id] = scheme
        self._macro_loading = True
        try:
            self._macro_active_scheme_id = scheme.id
            self.macro_editor.set_source(scheme.source)
        finally:
            self._macro_loading = False
        self._macro_dirty = False
        kind = "内置示例" if scheme.builtin else "已保存方案"
        self._set_macro_status(f"{kind}：{scheme.name}", state="idle")
        self.macro_mode_badge.setText(kind)
        self._update_controls()
        return True

    def _set_macro_combo_selection(self, scheme_id: str | None) -> None:
        combo = self.macro_editor.scheme_combo
        combo.blockSignals(True)
        try:
            target = -1
            for index in range(combo.count()):
                choice = combo.itemData(index)
                if isinstance(choice, SchemeChoice) and choice.scheme_id == scheme_id:
                    target = index
                    break
            combo.setCurrentIndex(target)
        finally:
            combo.blockSignals(False)

    def _on_macro_scheme_selected(self, scheme_id: str) -> None:
        if self._macro_runner.is_running:
            self._set_macro_combo_selection(self._macro_active_scheme_id)
            return
        if scheme_id == self._macro_active_scheme_id:
            return
        if not self._confirm_macro_changes():
            self._set_macro_combo_selection(self._macro_active_scheme_id)
            return
        self._load_macro_scheme(scheme_id)

    def _on_macro_source_changed(self, _source: str) -> None:
        if self._macro_loading:
            return
        scheme = self._macro_schemes.get(self._macro_active_scheme_id or "")
        self._macro_dirty = scheme is None or self.macro_editor.source != scheme.source
        self._macro_phase = "idle"
        self.macro_mode_badge.setText("未保存" if self._macro_dirty else "已保存")
        self._set_macro_status("内容已修改，运行前请检查", state="idle")
        self._update_controls()

    def _confirm_macro_changes(self) -> bool:
        if not self._macro_dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("保存自定义方案？")
        box.setText("当前自定义方案有未保存的更改。")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return self._save_macro_scheme()
        if result == QMessageBox.StandardButton.Discard:
            # "放弃" must not be persisted as a recoverable draft during the
            # final close path.  Scheme switching immediately loads another
            # source, while closing will now save dirty=False to QSettings.
            self._macro_dirty = False
            return True
        return False

    def _new_macro_scheme(self) -> None:
        if self._macro_runner.is_running or not self._confirm_macro_changes():
            return
        self._macro_active_scheme_id = None
        self._set_macro_combo_selection(None)
        self._macro_loading = True
        try:
            self.macro_editor.set_source(
                "# 新方案：每次按住 20ms，每 100ms 点击一次，共 10 次\n"
                "mouse left[last:20ms, count:10, interval:100ms]\n"
            )
        finally:
            self._macro_loading = False
        self._macro_dirty = True
        self.macro_mode_badge.setText("新方案")
        self._set_macro_status("新方案尚未保存", state="idle")
        self.macro_editor.editor.setFocus()
        self._update_controls()

    def _save_macro_scheme(self) -> bool:
        if self._validate_macro_source() is None:
            return False
        current = self._macro_schemes.get(self._macro_active_scheme_id or "")
        if current is None or current.builtin:
            return self._save_macro_scheme_as()
        try:
            saved = self._macro_store.save(
                replace(current, source=self.macro_editor.source)
            )
        except (SchemeStorageError, SchemeValidationError) as exc:
            self._show_error("无法保存自定义方案", str(exc))
            return False
        self._macro_schemes[saved.id] = saved
        self._macro_active_scheme_id = saved.id
        self._macro_dirty = False
        self._reload_macro_schemes(saved.id)
        self._set_macro_status(f"方案已保存：{saved.name}", state="success")
        return True

    def _save_macro_scheme_as(self) -> bool:
        if self._validate_macro_source() is None:
            return False
        current = self._macro_schemes.get(self._macro_active_scheme_id or "")
        default_name = (
            f"{current.name} 副本" if current is not None else "我的自定义方案"
        )
        name, accepted = QInputDialog.getText(
            self,
            "另存自定义方案",
            "方案名称",
            text=default_name,
        )
        name = name.strip()
        if not accepted or not name:
            return False
        try:
            saved = self._macro_store.create(name, self.macro_editor.source)
        except (SchemeStorageError, SchemeValidationError) as exc:
            self._show_error("无法保存自定义方案", str(exc))
            return False
        self._macro_active_scheme_id = saved.id
        self._macro_dirty = False
        self._reload_macro_schemes(saved.id)
        self._set_macro_status(f"已另存为：{saved.name}", state="success")
        return True

    def _delete_macro_scheme(self) -> None:
        scheme = self._macro_schemes.get(self._macro_active_scheme_id or "")
        if scheme is None or scheme.builtin or self._macro_runner.is_running:
            return
        answer = QMessageBox.question(
            self,
            "删除自定义方案？",
            f"“{scheme.name}”会从本机永久删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        try:
            self._macro_store.delete(scheme.id)
        except (SchemeStorageError, SchemeValidationError) as exc:
            self._show_error("无法删除自定义方案", str(exc))
            return
        self._macro_active_scheme_id = None
        self._macro_dirty = False
        self._reload_macro_schemes()
        self._set_macro_status("方案已删除", state="idle")

    def _validate_macro_source(self) -> CompiledMacro | None:
        try:
            plan = compile_macro(self.macro_editor.source)
        except MacroLanguageError as exc:
            self._focus_macro_error(exc.line, exc.column)
            self._set_macro_status(str(exc), state="error")
            return None
        except (TypeError, ValueError) as exc:
            self._set_macro_status(str(exc), state="error")
            return None
        action_count = len(plan.prefix_actions) + len(plan.loop_actions or ())
        summary = (
            f"语法通过 · 持续循环 · 每轮 {len(plan.loop_actions or ())} 个动作"
            if plan.is_infinite
            else f"语法通过 · 共 {action_count} 个动作"
        )
        self._set_macro_status(summary, state="success")
        return plan

    def _focus_macro_error(self, line: int, column: int) -> None:
        block = self.macro_editor.editor.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + min(max(0, column - 1), len(block.text())))
        self.macro_editor.editor.setTextCursor(cursor)
        self.macro_editor.editor.setFocus()

    def _start_macro(self) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or self._coordinate_picker.is_active
            or self._macro_runner.is_running
        ):
            return
        if (
            self._recorder.is_recording
            or self._record_countdown_timer.isActive()
            or self._player.is_running
            or self._autoclicker.is_running
        ):
            self._set_macro_status("已有输入任务运行，不能启动自定义方案", state="error")
            return
        plan = self._validate_macro_source()
        if plan is None:
            return
        all_actions = plan.prefix_actions + tuple(plan.loop_actions or ())
        try:
            if any(
                "x" in action.params or "y" in action.params for action in all_actions
            ):
                current_screen = capture_screen_config(QApplication.instance())
                validate_action_coordinates(all_actions, current_screen)
            self._macro_runner.start(plan)
        except (MacroLanguageError, MacroRunError, ScreenConfigError, RuntimeError) as exc:
            self._macro_phase = "error"
            self._set_macro_status(str(exc), state="error")
            return
        self._macro_phase = "running"
        self._macro_actions_executed = 0
        self._macro_last_error = None
        custom_page = self._settings_pages.get("custom")
        if custom_page is not None:
            self.settings_tabs.setCurrentWidget(custom_page)
        self.workspace_stack.setCurrentWidget(self.macro_workspace)
        self._set_macro_status("方案运行中 · Esc 可紧急停止", state="running")
        self._input_worker_completion_timer.start()
        self._apply_state(self._state_manager.snapshot())

    def _toggle_macro_pause(self) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or not self._macro_runner.is_running
            or self._macro_phase in {"stopping", "finishing"}
        ):
            return
        self._macro_runner.toggle_pause()
        self._macro_phase = "paused" if self._macro_runner.is_paused else "running"
        self._set_macro_status(
            "方案已暂停" if self._macro_runner.is_paused else "方案继续运行",
            state="paused" if self._macro_runner.is_paused else "running",
        )
        self._apply_state(self._state_manager.snapshot())

    def _stop_macro(self) -> None:
        if not self._macro_runner.is_running or self._macro_phase == "stopping":
            return
        self._macro_phase = "stopping"
        self._macro_runner.stop()
        self._set_macro_status("正在停止方案并安全释放输入…", state="paused")
        self._input_worker_completion_timer.start()
        self._update_controls()

    def _on_macro_event(self, event: PlaybackEvent) -> None:
        if event.kind == "action_executed":
            self._macro_actions_executed += 1
            self._set_macro_status(
                f"方案运行中 · 已执行 {self._macro_actions_executed} 个动作",
                state="running",
            )
        elif event.kind == "started":
            self._macro_phase = "running"
        elif event.kind == "finished":
            self._macro_phase = "finishing"
            self._input_worker_completion_timer.start()
        if event.error and event.error != self._macro_last_error:
            self._macro_last_error = event.error
            self._set_macro_status(event.error, state="error")
            if not self._closing:
                self._show_error("自定义方案错误", event.error)
        self._apply_state(self._state_manager.snapshot())

    def _finish_macro_ui(self) -> None:
        result = self._macro_runner.last_result
        if result is None:
            self._macro_phase = "stopped"
            self._set_macro_status("方案已停止", state="idle")
        elif result.outcome is PlaybackOutcome.FAILED:
            self._macro_phase = "error"
            self._set_macro_status(result.error or "方案运行失败", state="error")
        elif result.outcome is PlaybackOutcome.COMPLETED:
            self._macro_phase = "finished"
            self._set_macro_status(
                f"方案已完成 · 共执行 {self._macro_actions_executed} 个动作",
                state="success",
            )
        else:
            self._macro_phase = "stopped"
            self._set_macro_status(
                f"方案已停止 · 共执行 {self._macro_actions_executed} 个动作",
                state="idle",
            )
        self.macro_mode_badge.setText(
            {
                "finished": "已完成",
                "stopped": "已停止",
                "error": "错误",
            }.get(self._macro_phase, "安全沙盒语法")
        )
        self._update_controls()

    def _set_macro_status(self, message: str, *, state: str) -> None:
        self.macro_editor.set_validation(message, state=state)
        self.macro_console_status_label.setText(message)
        self._set_dynamic_property(self.macro_console_status_label, "state", state)

    def _on_settings_tab_changed(self, _index: int) -> None:
        page = self.settings_tabs.currentWidget()
        show_macro = (
            self._macro_runner.is_running
            or (page is not None and page.property("settingsPageKey") == "custom")
        )
        self.workspace_stack.setCurrentWidget(
            self.macro_workspace if show_macro else self.action_workspace
        )
        self._update_controls()

    def _custom_mode_selected(self) -> bool:
        page = self.settings_tabs.currentWidget()
        return page is not None and page.property("settingsPageKey") == "custom"

    # ----------------------------------------------------------- auto click
    def _start_auto_click(self, _checked: bool = False) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or self._coordinate_picker.is_active
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        ):
            return
        if (
            self._recorder.is_recording
            or self._record_countdown_timer.isActive()
            or self._player.is_running
            or self._macro_runner.is_running
        ):
            self.status_message_label.setText("已有输入任务运行，不能开始鼠标连点")
            return
        try:
            location_mode = ClickLocationMode(
                str(self.auto_click_location_combo.currentData())
            )
            fixed = location_mode is ClickLocationMode.FIXED
            if fixed and not self._fixed_target_confirmed:
                self.settings_tabs.setCurrentIndex(1)
                self._ensure_auto_click_picker_visible()
                self.status_message_label.setText("请先在屏幕上选取或手动确认固定坐标")
                self._update_controls()
                return
            x = self.auto_click_x_spin.value() if fixed else None
            y = self.auto_click_y_spin.value() if fixed else None
            if fixed:
                assert x is not None and y is not None
                current_screen = capture_screen_config(QApplication.instance())
                validate_coordinate_for_screen(x, y, current_screen)
            settings = AutoClickSettings(
                button=str(self.auto_click_button_combo.currentData()),
                interval_ms=float(self.auto_click_interval_spin.value()),
                click_count=self.auto_click_count_spin.value(),
                infinite=self.auto_click_infinite_checkbox.isChecked(),
                location_mode=location_mode,
                x=x,
                y=y,
                countdown_seconds=float(self.auto_click_countdown_spin.value()),
                coordinate_anchor=self._coordinate_anchor if fixed else None,
                adapt_to_cursor_clip=(
                    self.auto_click_adapt_checkbox.isChecked() if fixed else False
                ),
            )
            self._auto_click_completed = 0
            self._auto_click_target = (
                None if settings.infinite else settings.click_count
            )
            self._auto_click_phase = (
                "countdown" if settings.countdown_seconds > 0 else "running"
            )
            self.settings_tabs.setCurrentIndex(1)
            self._autoclicker.start(settings)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._auto_click_phase = "error"
            self._update_auto_click_status()
            self._state_manager.transition(AppState.ERROR, message=str(exc), force=True)
            self._show_error("无法开始鼠标连点", str(exc))
            return
        self._update_auto_click_status()
        self._apply_state(self._state_manager.snapshot())

    def _toggle_auto_click_pause(self) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or not self._autoclicker.is_running
        ):
            return
        self._autoclicker.toggle_pause()
        snapshot = self._state_manager.snapshot()
        self._auto_click_phase = (
            "paused"
            if self._autoclicker.is_paused
            else ("countdown" if snapshot.state is AppState.COUNTDOWN else "running")
        )
        self._update_auto_click_status()
        self._apply_state(snapshot)

    def _stop_auto_click(self) -> None:
        if not self._autoclicker.is_running:
            return
        if self._auto_click_phase == "stopping":
            return
        self._auto_click_phase = "stopping"
        self._autoclicker.stop()
        self.status_message_label.setText("正在停止鼠标连点并释放输入…")
        if not self._autoclicker.is_running:
            self._auto_click_phase = "stopped"
        self._update_auto_click_status()
        self._update_controls()

    def _auto_click_hotkey(self, _checked: bool = False) -> None:
        if (
            self._closing
            or self._close_prompt_active
            or self._coordinate_picker.is_active
        ):
            return
        if self._autoclicker.is_running:
            if self._auto_click_phase == "stopping":
                return
            self._stop_auto_click()
        else:
            self._start_auto_click()

    def _on_auto_click_event(self, event: AutoClickEvent) -> None:
        self._auto_click_completed = event.clicks_completed
        self._auto_click_target = event.target_clicks
        if event.kind == "countdown":
            self._auto_click_phase = "countdown"
        elif event.kind in {"started", "resumed"}:
            self._auto_click_phase = (
                "countdown" if event.state is AppState.COUNTDOWN else "running"
            )
        elif event.kind == "click_completed":
            self._auto_click_phase = "running"
        elif event.kind == "paused":
            self._auto_click_phase = "paused"
        elif event.kind == "error":
            self._auto_click_phase = "error"
        elif event.kind == "finished":
            if event.error:
                self._auto_click_phase = "error"
            else:
                self._auto_click_phase = (
                    "stopped" if "停止" in event.message else "finished"
                )
            self._input_worker_completion_timer.start()
        if event.kind == "error" and event.error and not self._closing:
            self._show_error("鼠标连点错误", event.error)
        self._update_auto_click_status(event)
        self._apply_state(self._state_manager.snapshot())

    def _poll_input_worker_completion(self) -> None:
        """Refresh controls only after a terminal worker has actually exited."""

        if (
            self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        ):
            return
        self._input_worker_completion_timer.stop()
        if self._macro_phase in {"running", "paused", "stopping", "finishing"}:
            self._finish_macro_ui()
        if not self._closing:
            self._apply_state(self._state_manager.snapshot())

    def _update_auto_click_status(self, event: AutoClickEvent | None = None) -> None:
        phase = self._auto_click_phase
        if self._autoclicker.is_running:
            phase = "paused" if self._autoclicker.is_paused else phase
        title = {
            "idle": "未运行",
            "countdown": "准备中",
            "running": "连点中",
            "paused": "已暂停",
            "stopping": "正在停止",
            "stopped": "已停止",
            "finished": "已完成",
            "error": "发生错误",
        }.get(phase, "未运行")
        if event is not None and event.countdown_remaining is not None:
            title = f"{title} · {event.countdown_remaining} 秒"
        elif event is not None and event.kind in {"finished", "error"}:
            title = event.message or title
        target = (
            "∞" if self._auto_click_target is None else str(self._auto_click_target)
        )
        status_text = f"{title} · 已完成 {self._auto_click_completed} / {target}"
        self.auto_click_status_label.setText(status_text)
        self.auto_click_status_label.setAccessibleDescription(status_text)
        visual_state = {
            "countdown": "countdown",
            "running": "running",
            "paused": "paused",
            "stopping": "countdown",
            "error": "error",
        }.get(phase, "idle")
        self._set_dynamic_property(self.auto_click_status_label, "state", visual_state)

    def _on_auto_click_infinite_toggled(self, checked: bool) -> None:
        self.auto_click_count_spin.setEnabled(
            not checked and not self._autoclicker.is_running
        )
        if not self._autoclicker.is_running:
            self._auto_click_target = (
                None if checked else self.auto_click_count_spin.value()
            )
            self._auto_click_phase = "idle"
            self._update_auto_click_status()

    def _on_auto_click_count_changed(self, value: int) -> None:
        if (
            not self._autoclicker.is_running
            and not self.auto_click_infinite_checkbox.isChecked()
        ):
            self._auto_click_target = value
            self._auto_click_completed = 0
            self._auto_click_phase = "idle"
            self._update_auto_click_status()

    def _update_auto_click_location_controls(self, _index: int = -1) -> None:
        fixed = (
            self.auto_click_location_combo.currentData()
            == ClickLocationMode.FIXED.value
        )
        busy = (
            self._recorder.is_recording
            or self._record_countdown_timer.isActive()
            or self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
            or self._coordinate_picker.is_active
        )
        enabled = fixed and not busy
        self.auto_click_x_spin.setEnabled(enabled)
        self.auto_click_y_spin.setEnabled(enabled)
        self.capture_cursor_button.setEnabled(enabled)
        self.auto_click_adapt_checkbox.setEnabled(
            enabled and self._coordinate_anchor is not None
        )
        if not fixed:
            hint = "跟随模式不会主动移动鼠标，每次点击使用当时的光标位置。"
        elif self._fixed_target_confirmed and self._coordinate_anchor is not None:
            adapt_note = (
                "切到全屏画面后会保持屏幕内相对位置。"
                if self.auto_click_adapt_checkbox.isChecked()
                else "切屏适配已关闭，将严格使用当前物理坐标。"
            )
            hint = (
                f"已取点 ({self.auto_click_x_spin.value()}, "
                f"{self.auto_click_y_spin.value()})；{adapt_note}"
            )
        elif self._fixed_target_confirmed:
            hint = (
                f"手动坐标为 ({self.auto_click_x_spin.value()}, "
                f"{self.auto_click_y_spin.value()})，将严格使用当前桌面；"
                "重新屏幕取点可启用切屏适配。"
            )
        else:
            hint = (
                "请先点击“在屏幕上选取位置”；屏幕变暗后在目标处单击。"
                "确认目标前不会开始连点。"
            )
        self.auto_click_location_hint.setText(hint)
        if fixed and _index >= 0:
            QTimer.singleShot(0, self._ensure_auto_click_picker_visible)
        self._update_controls()

    def _ensure_auto_click_picker_visible(self) -> None:
        if (
            self.auto_click_location_combo.currentData()
            != ClickLocationMode.FIXED.value
        ):
            return
        self.auto_click_scroll_area.ensureWidgetVisible(
            self.capture_cursor_button,
            12,
            12,
        )

    def _on_fixed_coordinate_edited(self, _value: int) -> None:
        if self._restoring_settings:
            return
        if (
            self.auto_click_location_combo.currentData()
            != ClickLocationMode.FIXED.value
        ):
            return
        self._coordinate_anchor = None
        self._fixed_target_confirmed = True
        self._update_auto_click_location_controls()

    def _capture_cursor_position(self) -> None:
        if self._closing or self._close_prompt_active:
            return
        if (
            self.auto_click_location_combo.currentData()
            != ClickLocationMode.FIXED.value
        ):
            return
        if (
            self._recorder.is_recording
            or self._record_countdown_timer.isActive()
            or self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        ):
            self.status_message_label.setText("输入任务运行期间不能选择坐标")
            return
        if self._coordinate_picker.start():
            self.status_message_label.setText(
                "屏幕取点已开启：在目标位置单击，右键或 Esc 取消"
            )
            self._update_controls()

    def _on_coordinate_picked(self, x: int, y: int) -> None:
        self._fixed_target_confirmed = True
        self._restoring_settings = True
        try:
            self.auto_click_x_spin.setValue(x)
            self.auto_click_y_spin.setValue(y)
        finally:
            self._restoring_settings = False
        anchor = self._coordinate_picker.last_selection_anchor
        self._coordinate_anchor = (
            anchor if anchor is not None and (anchor.x, anchor.y) == (x, y) else None
        )
        self.status_message_label.setText(
            f"已记录屏幕相对位置：({x}, {y})"
            if self._coordinate_anchor is not None
            else f"已选择固定位置：({x}, {y})"
        )
        self.auto_click_location_hint.setText(
            f"已取点 ({x}, {y})；切到全屏画面后可保持相对位置。"
            if self._coordinate_anchor is not None
            else f"当前目标为 ({x}, {y})；可再次在屏幕上选取。"
        )
        self._update_auto_click_location_controls()
        if not (self._closing or self._close_prompt_active):
            self.raise_()
            self.activateWindow()

    def _on_coordinate_pick_cancelled(self) -> None:
        self.status_message_label.setText("已取消屏幕取点，原坐标保持不变")
        self._update_controls()
        if not (self._closing or self._close_prompt_active):
            self.raise_()
            self.activateWindow()

    def _on_coordinate_pick_failed(self, message: str) -> None:
        self._update_controls()
        self._show_error("无法选择固定位置", message)

    @staticmethod
    def _set_dynamic_property(widget: QWidget, name: str, value: object) -> None:
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _confirm_screen_mismatch(self, message: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("显示器配置已变化")
        box.setText(message)
        continue_button = box.addButton(
            "仍然继续", QMessageBox.ButtonRole.DestructiveRole
        )
        cancel_button = box.addButton("取消回放", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_button)
        box.exec()
        return box.clickedButton() is continue_button

    # ---------------------------------------------------------- action edit
    def _set_action_enabled(self, index: int, enabled: bool) -> None:
        if not 0 <= index < len(self._actions):
            return
        if self._actions[index].enabled == enabled:
            return
        self._actions[index] = self._actions[index].clone(enabled=enabled)
        self._set_dirty(True)
        self._refresh_actions()

    def _edit_action(self, index: int) -> None:
        if not 0 <= index < len(self._actions):
            return
        editor = ActionEditor(self._actions[index], self)
        if editor.exec() != ActionEditor.DialogCode.Accepted:
            return
        edited = editor.edited_action
        if edited is None:
            return
        self._actions[index] = edited
        recalculate_offsets(self._actions)
        self._set_dirty(True)
        self._refresh_actions()

    def _delete_action(self, index: int) -> None:
        if not 0 <= index < len(self._actions):
            return
        delete_action(self._actions, index)
        self._set_dirty(True)
        self._refresh_actions()

    def _move_action(self, index: int, delta: int) -> None:
        target = index + delta
        if not 0 <= index < len(self._actions) or not 0 <= target < len(self._actions):
            return
        reorder_actions(self._actions, index, target)
        self._set_dirty(True)
        self._refresh_actions()
        self.action_table.selectRow(target)

    def _clear_actions(self) -> None:
        if not self._actions:
            return
        answer = QMessageBox.question(
            self,
            "清空全部动作？",
            "此操作会从当前列表删除全部动作。未保存的更改将丢失。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._actions.clear()
        self._set_dirty(True)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        self._action_inventory_cache = None
        self.action_table.set_actions(self._actions)
        self.action_count_badge.setText(f"{len(self._actions)} 个动作")
        action_summary = self._playback_action_summary()
        self._update_ignore_mouse_moves_hint(action_summary)
        snapshot = self._state_manager.snapshot()
        if not self._recorder.is_recording and not self._player.is_running:
            self.action_label.setText(f"动作：0/{len(self._actions)}")
        self._update_controls(snapshot, action_summary=action_summary)

    # ---------------------------------------------------------- state/control
    def _apply_state(self, snapshot: StateSnapshot) -> None:
        if self._macro_runner.is_running:
            state_text = "方案暂停" if self._macro_runner.is_paused else "自定义方案"
        elif self._autoclicker.is_running:
            state_text = "连点暂停" if self._autoclicker.is_paused else "鼠标连点"
        else:
            state_text = snapshot.state.label
        self.state_label.setText(f"状态：{state_text}")
        visual_state = {
            AppState.COUNTDOWN: "countdown",
            AppState.RECORDING: "running",
            AppState.PLAYING: "running",
            AppState.CLICKING: "running",
            AppState.PAUSED: "paused",
            AppState.ERROR: "error",
        }.get(snapshot.state, "idle")
        if self._macro_runner.is_running:
            visual_state = "paused" if self._macro_runner.is_paused else "running"
        elif self._autoclicker.is_running:
            visual_state = "paused" if self._autoclicker.is_paused else "running"
        self._set_dynamic_property(self.state_label, "state", visual_state)
        total_loops = "∞" if snapshot.total_loops is None else str(snapshot.total_loops)
        if self._macro_runner.is_running:
            self.loop_label.setText(f"指令：{self._macro_actions_executed}")
            self.action_label.setVisible(False)
        elif self._autoclicker.is_running:
            self.loop_label.setText(f"点击：{self._auto_click_completed}/{total_loops}")
            self.action_label.setVisible(False)
        else:
            self.loop_label.setText(f"循环：{snapshot.current_loop}/{total_loops}")
            self.action_label.setText(
                f"动作：{snapshot.current_action}/{snapshot.total_actions}"
            )
            self.action_label.setVisible(True)
        self.status_message_label.setText(snapshot.message)
        self.progress_bar.setValue(round(snapshot.progress * 1000))
        self.progress_bar.setVisible(
            snapshot.state
            in {
                AppState.COUNTDOWN,
                AppState.RECORDING,
                AppState.PLAYING,
                AppState.CLICKING,
                AppState.PAUSED,
            }
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        )
        self._update_auto_click_status()
        self._update_controls(snapshot)

    def _update_controls(
        self,
        _snapshot: StateSnapshot | None = None,
        *,
        action_summary: tuple[int, int, bool] | None = None,
    ) -> None:
        recording = self._recorder.is_recording
        recording_countdown = self._record_countdown_timer.isActive()
        playing = self._player.is_running
        auto_clicking = self._autoclicker.is_running
        macro_running = self._macro_runner.is_running
        macro_stopping = macro_running and self._macro_phase in {
            "stopping",
            "finishing",
        }
        custom_selected = self._custom_mode_selected()
        auto_click_stopping = auto_clicking and self._auto_click_phase == "stopping"
        picking_coordinate = self._coordinate_picker.is_active
        busy = (
            recording
            or recording_countdown
            or playing
            or auto_clicking
            or macro_running
            or picking_coordinate
        )
        idle = not busy

        self.new_action.setEnabled(idle)
        self.open_action.setEnabled(idle)
        self.save_action.setEnabled(idle and bool(self._actions))
        self.save_as_action.setEnabled(idle and bool(self._actions))
        self.record_action.setEnabled(
            not playing
            and not auto_clicking
            and not macro_running
            and not picking_coordinate
        )
        self.record_action.setText(
            "停止录制 (F8)" if recording or recording_countdown else "录制 (F8)"
        )
        if action_summary is None:
            action_summary = self._playback_action_summary()
        self.stop_action.setEnabled(busy)
        if macro_running:
            self.play_action.setEnabled(not macro_stopping)
            self.play_action.setText(
                "继续方案 (F9)"
                if self._macro_runner.is_paused
                else "暂停方案 (F9)"
            )
            self.play_action.setStatusTip("暂停或继续当前自定义方案")
        elif idle and custom_selected:
            self.play_action.setEnabled(True)
            self.play_action.setText("运行方案 (F9)")
            self.play_action.setStatusTip("检查并运行当前自定义方案")
        else:
            self.play_action.setEnabled(idle and action_summary[2])
            self.play_action.setText("播放 (F9)")
            self.play_action.setStatusTip("开始普通脚本回放")
        self.pause_action.setEnabled(playing)
        self.pause_action.setText(
            "继续 (F9)" if self._player.is_paused else "暂停 (F9)"
        )
        fixed_location = (
            self.auto_click_location_combo.currentData()
            == ClickLocationMode.FIXED.value
        )
        fixed_target_ready = not fixed_location or self._fixed_target_confirmed
        self.auto_click_action.setEnabled(
            (idle and fixed_target_ready) or (auto_clicking and not auto_click_stopping)
        )
        self.auto_click_action.setText(
            "停止连点 (F10)" if auto_clicking else "连点 (F10)"
        )
        self.emergency_action.setEnabled(busy)
        self.action_table.setEnabled(idle)
        self.clear_button.setEnabled(idle and bool(self._actions))
        for widget in (
            self.loop_count_spin,
            self.infinite_checkbox,
            self.loop_interval_spin,
            self.speed_spin,
            self.playback_countdown_spin,
            self.record_countdown_spin,
            self.sample_interval_spin,
        ):
            widget.setEnabled(idle)
        self.ignore_mouse_moves_checkbox.setEnabled(idle)
        self.skip_move_click_interval_spin.setEnabled(
            idle and self.ignore_mouse_moves_checkbox.isChecked()
        )
        if idle and self.infinite_checkbox.isChecked():
            self.loop_count_spin.setEnabled(False)

        auto_click_fields = (
            self.auto_click_button_combo,
            self.auto_click_interval_spin,
            self.auto_click_infinite_checkbox,
            self.auto_click_countdown_spin,
            self.auto_click_location_combo,
        )
        for widget in auto_click_fields:
            widget.setEnabled(idle)
        self.auto_click_count_spin.setEnabled(
            idle and not self.auto_click_infinite_checkbox.isChecked()
        )
        for widget in (
            self.auto_click_x_spin,
            self.auto_click_y_spin,
            self.capture_cursor_button,
        ):
            widget.setEnabled(idle and fixed_location)
        self.auto_click_adapt_checkbox.setEnabled(
            idle and fixed_location and self._coordinate_anchor is not None
        )
        self.auto_click_start_button.setEnabled(idle and fixed_target_ready)
        self.auto_click_pause_button.setEnabled(
            auto_clicking and not auto_click_stopping
        )
        self.auto_click_pause_button.setText(
            "继续" if self._autoclicker.is_paused else "暂停"
        )
        self.auto_click_stop_button.setEnabled(
            auto_clicking and not auto_click_stopping
        )
        self.macro_editor.set_running(
            macro_running,
            paused=self._macro_runner.is_paused,
        )
        self.macro_editor.setEnabled(idle or macro_running)
        if macro_running:
            self.macro_mode_badge.setText(
                "已暂停" if self._macro_runner.is_paused else "运行中"
            )

    def _playable_actions(self) -> list[Action]:
        """Return actions included by the current non-destructive playback filter."""

        ignore_moves = self.ignore_mouse_moves_checkbox.isChecked()
        return [
            action
            for action in self._actions
            if action.enabled
            and not (ignore_moves and action.type is ActionType.MOUSE_MOVE)
        ]

    def _playback_action_summary(self) -> tuple[int, int, bool]:
        """Return cached action inventory with filter-dependent playability."""

        inventory = self._action_inventory_cache
        if inventory is None:
            total_moves = 0
            enabled_moves = 0
            enabled_non_moves = 0
            for action in self._actions:
                is_move = action.type is ActionType.MOUSE_MOVE
                if is_move:
                    total_moves += 1
                    if action.enabled:
                        enabled_moves += 1
                elif action.enabled:
                    enabled_non_moves += 1
            inventory = (total_moves, enabled_moves, enabled_non_moves)
            self._action_inventory_cache = inventory
        total_moves, enabled_moves, enabled_non_moves = inventory
        ignore_moves = self.ignore_mouse_moves_checkbox.isChecked()
        has_playable_action = bool(
            enabled_non_moves or (enabled_moves and not ignore_moves)
        )
        return total_moves, enabled_moves, has_playable_action

    def _on_ignore_mouse_moves_toggled(self, checked: bool) -> None:
        action_summary = self._playback_action_summary()
        self._update_ignore_mouse_moves_hint(action_summary)
        enabled_moves = action_summary[1]
        if enabled_moves:
            self.status_message_label.setText(
                f"回放将跳过 {enabled_moves} 个鼠标移动动作；取消勾选即可恢复"
                if checked
                else "已恢复鼠标移动轨迹回放"
            )
        self._update_controls(action_summary=action_summary)

    def _update_ignore_mouse_moves_hint(
        self, action_summary: tuple[int, int, bool] | None = None
    ) -> None:
        if action_summary is None:
            action_summary = self._playback_action_summary()
        total_moves, enabled_moves, _has_playable_action = action_summary
        prefix = (
            "本次跳过" if self.ignore_mouse_moves_checkbox.isChecked() else "已启用"
        )
        self.ignore_mouse_moves_hint.setText(
            f"移动轨迹：{prefix} {enabled_moves} / 共 {total_moves}"
        )

    def _stop_current_operation(self) -> None:
        if self._coordinate_picker.is_active:
            self._coordinate_picker.cancel()
        if self._record_countdown_timer.isActive() or self._recorder.is_recording:
            self._stop_recording()
        if self._player.is_running:
            self._player.stop()
        if self._autoclicker.is_running:
            self._stop_auto_click()
        if self._macro_runner.is_running:
            self._stop_macro()
        self._apply_state(self._state_manager.snapshot())

    def _emergency_stop(self) -> None:
        stopped = False
        if self._coordinate_picker.is_active:
            self._coordinate_picker.cancel()
            stopped = True
        if self._record_countdown_timer.isActive() or self._recorder.is_recording:
            self._stop_recording(emergency=True)
            stopped = True
        if self._player.is_running:
            self._player.emergency_stop()
            stopped = True
        if self._autoclicker.is_running:
            self._autoclicker.emergency_stop()
            stopped = True
        if self._macro_runner.is_running:
            self._macro_phase = "stopping"
            self._macro_runner.emergency_stop()
            self._input_worker_completion_timer.start()
            stopped = True
        if stopped:
            self.status_message_label.setText("已触发紧急停止")
        self._apply_state(self._state_manager.snapshot())

    # -------------------------------------------------------- lifecycle misc
    def _start_hotkeys(self) -> None:
        if self._closing:
            return
        try:
            self._hotkeys.start()
        except HotkeyError as exc:
            self._on_hotkey_error(str(exc))

    def _on_hotkey_error(self, message: str) -> None:
        if self._closing:
            return
        self._show_error(
            "全局快捷键不可用",
            f"{message}\n\n仍可使用窗口中的按钮。请检查桌面会话和权限级别。",
        )

    def _show_first_run_notice(self) -> None:
        if not self._settings.value("show_first_run_notice", True, type=bool):
            return
        box = QMessageBox(self)
        box.setWindowTitle("欢迎使用 WinActionRecorder")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "使用 F8 开始/停止录制；F9 会按当前页面控制普通回放或自定义方案；"
            "F10 开始/停止连点，Esc 紧急停止。\n\n"
            "录制可能包含密码、聊天内容等敏感输入，请勿在录制期间输入敏感信息。"
            "所有数据仅保存在本地。"
        )
        checkbox = QCheckBox("以后不再显示")
        box.setCheckBox(checkbox)
        box.exec()
        if checkbox.isChecked():
            self._settings.setValue("show_first_run_notice", False)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        name = self._script_path.name if self._script_path else "未命名"
        marker = " *" if dirty else ""
        self.setWindowTitle(f"{name}{marker} - WinActionRecorder")

    def _confirm_discard_changes(self) -> bool:
        if not self._dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("保存更改？")
        box.setText("当前动作脚本有未保存的更改。")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return self._save_script()
        return result == QMessageBox.StandardButton.Discard

    def _restore_settings(self) -> None:
        self._restoring_settings = True
        geometry = self._settings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.sample_interval_spin.setValue(
            self._settings.value("sample_interval_ms", 20, type=int)
        )
        self.loop_count_spin.setValue(self._settings.value("loop_count", 1, type=int))
        self.infinite_checkbox.setChecked(
            self._settings.value("infinite_loop", False, type=bool)
        )
        self.loop_interval_spin.setValue(
            self._settings.value("loop_interval_seconds", 0.0, type=float)
        )
        self.speed_spin.setValue(
            self._settings.value("playback_speed", 1.0, type=float)
        )
        self.record_countdown_spin.setValue(
            self._settings.value("record_countdown", 3, type=int)
        )
        self.playback_countdown_spin.setValue(
            self._settings.value("playback_countdown", 3, type=int)
        )
        self.ignore_mouse_moves_checkbox.setChecked(
            self._settings.value("ignore_mouse_moves", False, type=bool)
        )
        self.skip_move_click_interval_spin.setValue(
            self._settings.value("skip_move_click_interval_ms", 200, type=int)
        )
        button_index = self.auto_click_button_combo.findData(
            self._settings.value("auto_click_button", "left", type=str)
        )
        self.auto_click_button_combo.setCurrentIndex(max(0, button_index))
        self.auto_click_interval_spin.setValue(
            self._settings.value("auto_click_interval_ms", 100, type=int)
        )
        self.auto_click_count_spin.setValue(
            self._settings.value("auto_click_count", 10, type=int)
        )
        self.auto_click_infinite_checkbox.setChecked(
            self._settings.value("auto_click_infinite", False, type=bool)
        )
        location_index = self.auto_click_location_combo.findData(
            self._settings.value(
                "auto_click_location_mode",
                ClickLocationMode.FOLLOW_CURSOR.value,
                type=str,
            )
        )
        self.auto_click_location_combo.setCurrentIndex(max(0, location_index))
        self.auto_click_x_spin.setValue(
            self._settings.value("auto_click_x", 0, type=int)
        )
        self.auto_click_y_spin.setValue(
            self._settings.value("auto_click_y", 0, type=int)
        )
        self.auto_click_countdown_spin.setValue(
            self._settings.value("auto_click_countdown", 3, type=int)
        )
        self.auto_click_adapt_checkbox.setChecked(
            self._settings.value(
                "auto_click_adapt_to_cursor_clip",
                True,
                type=bool,
            )
        )
        self._fixed_target_confirmed = self._settings.value(
            "auto_click_fixed_target_confirmed",
            False,
            type=bool,
        )
        self._coordinate_anchor = None
        if self._settings.value("auto_click_anchor_valid", False, type=bool):
            try:
                anchor_monitor = self._settings.value(
                    "auto_click_anchor_monitor_id", "", type=str
                ).strip()
                self._coordinate_anchor = CoordinateAnchor(
                    x=self._settings.value("auto_click_anchor_x", 0, type=int),
                    y=self._settings.value("auto_click_anchor_y", 0, type=int),
                    source_rect=VirtualDesktop(
                        self._settings.value(
                            "auto_click_anchor_rect_x", 0, type=int
                        ),
                        self._settings.value(
                            "auto_click_anchor_rect_y", 0, type=int
                        ),
                        self._settings.value(
                            "auto_click_anchor_rect_width", 1, type=int
                        ),
                        self._settings.value(
                            "auto_click_anchor_rect_height", 1, type=int
                        ),
                    ),
                    monitor_id=anchor_monitor or None,
                )
            except (ScreenConfigError, TypeError, ValueError):
                logger.warning("已忽略损坏的固定坐标锚点设置")
                self._coordinate_anchor = None
        target_schema = self._settings.value(
            "auto_click_target_schema_version", 1, type=int
        )
        if (
            target_schema < 2
            and self.auto_click_location_combo.currentData()
            == ClickLocationMode.FIXED.value
            and self._coordinate_anchor is None
        ):
            # Pre-0.4 settings have no source-monitor anchor and may be the
            # exact stale target that led to the old switch-screen failure.
            # Require one explicit edit or a fresh full-screen pick once.
            self._fixed_target_confirmed = False
        self._restoring_settings = False
        self._auto_click_target = (
            None
            if self.auto_click_infinite_checkbox.isChecked()
            else self.auto_click_count_spin.value()
        )
        self._update_auto_click_location_controls()
        self._update_auto_click_status()

        selected_macro_id = self._settings.value(
            "macro_active_scheme_id", "", type=str
        )
        if selected_macro_id in self._macro_schemes:
            self._set_macro_combo_selection(selected_macro_id)
            self._load_macro_scheme(selected_macro_id)
        draft_source = self._settings.value("macro_draft_source", "", type=str)
        draft_dirty = self._settings.value("macro_draft_dirty", False, type=bool)
        if draft_dirty and draft_source.strip():
            if not selected_macro_id:
                self._macro_active_scheme_id = None
                self._set_macro_combo_selection(None)
            self._macro_loading = True
            try:
                self.macro_editor.set_source(draft_source)
            finally:
                self._macro_loading = False
            self._macro_dirty = True
            self.macro_mode_badge.setText("未保存草稿")
            self._set_macro_status("已恢复未保存的本地草稿", state="idle")

    def _save_settings(self) -> None:
        self._settings.setValue("window_geometry", self.saveGeometry())
        self._settings.setValue("sample_interval_ms", self.sample_interval_spin.value())
        self._settings.setValue("loop_count", self.loop_count_spin.value())
        self._settings.setValue("infinite_loop", self.infinite_checkbox.isChecked())
        self._settings.setValue(
            "loop_interval_seconds", self.loop_interval_spin.value()
        )
        self._settings.setValue("playback_speed", self.speed_spin.value())
        self._settings.setValue("record_countdown", self.record_countdown_spin.value())
        self._settings.setValue(
            "playback_countdown", self.playback_countdown_spin.value()
        )
        self._settings.setValue(
            "ignore_mouse_moves", self.ignore_mouse_moves_checkbox.isChecked()
        )
        self._settings.setValue(
            "skip_move_click_interval_ms",
            self.skip_move_click_interval_spin.value(),
        )
        self._settings.setValue(
            "auto_click_button", self.auto_click_button_combo.currentData()
        )
        self._settings.setValue(
            "auto_click_interval_ms", self.auto_click_interval_spin.value()
        )
        self._settings.setValue("auto_click_count", self.auto_click_count_spin.value())
        self._settings.setValue(
            "auto_click_infinite", self.auto_click_infinite_checkbox.isChecked()
        )
        self._settings.setValue(
            "auto_click_location_mode",
            self.auto_click_location_combo.currentData(),
        )
        self._settings.setValue("auto_click_x", self.auto_click_x_spin.value())
        self._settings.setValue("auto_click_y", self.auto_click_y_spin.value())
        self._settings.setValue(
            "auto_click_fixed_target_confirmed",
            self._fixed_target_confirmed,
        )
        self._settings.setValue("auto_click_target_schema_version", 2)
        self._settings.setValue(
            "auto_click_countdown", self.auto_click_countdown_spin.value()
        )
        self._settings.setValue(
            "auto_click_adapt_to_cursor_clip",
            self.auto_click_adapt_checkbox.isChecked(),
        )
        anchor = self._coordinate_anchor
        self._settings.setValue("auto_click_anchor_valid", anchor is not None)
        if anchor is not None:
            self._settings.setValue("auto_click_anchor_x", anchor.x)
            self._settings.setValue("auto_click_anchor_y", anchor.y)
            self._settings.setValue(
                "auto_click_anchor_rect_x", anchor.source_rect.x
            )
            self._settings.setValue(
                "auto_click_anchor_rect_y", anchor.source_rect.y
            )
            self._settings.setValue(
                "auto_click_anchor_rect_width", anchor.source_rect.width
            )
            self._settings.setValue(
                "auto_click_anchor_rect_height", anchor.source_rect.height
            )
            self._settings.setValue(
                "auto_click_anchor_monitor_id", anchor.monitor_id or ""
            )
        self._settings.setValue(
            "macro_active_scheme_id", self._macro_active_scheme_id or ""
        )
        self._settings.setValue("macro_draft_source", self.macro_editor.source)
        self._settings.setValue("macro_draft_dirty", self._macro_dirty)
        self._settings.sync()

    @staticmethod
    def _show_error(title: str, message: str) -> None:
        logger.error("%s", title)
        QMessageBox.critical(None, title, message)

    def _any_input_worker_running(self) -> bool:
        return (
            self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._final_close:
            self._finish_close(event)
            return
        if self._close_pending or self._close_prompt_active:
            event.ignore()
            return
        # Block queued start shortcuts before cancelling the overlay.  The
        # cancellation signal is synchronous and may process UI callbacks.
        self._close_prompt_active = True
        if self._coordinate_picker.is_active:
            self._coordinate_picker.cancel()
        # Never keep injecting or recording while a modal save prompt is open.
        if (
            self._record_countdown_timer.isActive()
            or self._recorder.is_recording
            or self._player.is_running
            or self._autoclicker.is_running
            or self._macro_runner.is_running
        ):
            self._emergency_stop()
        if not self._confirm_discard_changes() or not self._confirm_macro_changes():
            self._close_prompt_active = False
            event.ignore()
            return
        self._closing = True
        self._close_prompt_active = False
        # Prevent already-active hooks from queuing new F8/F9/F10 work while a
        # worker is finishing its final input release. Entry points also guard
        # ``_closing`` because signals already queued in Qt may still arrive.
        self._hotkeys.stop()
        if self._any_input_worker_running():
            # The worker owns final native input release. Keep the Qt event
            # loop responsive while it completes instead of joining here.
            self._close_pending = True
            self._close_deadline = time.monotonic() + 5.0
            self.setEnabled(False)
            self.status_message_label.setText("正在安全释放输入并关闭…")
            if self._player.is_running:
                self._player.stop()
            if self._autoclicker.is_running:
                self._autoclicker.stop()
            if self._macro_runner.is_running:
                self._macro_runner.stop()
            self._close_poll_timer.start()
            event.ignore()
            return
        self._finish_close(event)

    def _poll_async_close(self) -> None:
        if self._any_input_worker_running():
            now = time.monotonic()
            if now >= self._close_deadline:
                # Never force the process to exit while the worker may still
                # own injected input.  Keep Qt alive, repeat the stop request,
                # and tell the user why shutdown is deliberately waiting.
                self._close_deadline = now + 5.0
                if self._player.is_running:
                    self._player.stop()
                if self._autoclicker.is_running:
                    self._autoclicker.stop()
                if self._macro_runner.is_running:
                    self._macro_runner.stop()
                self.status_message_label.setText(
                    "输入安全清理超过 5 秒；应用将保持打开并持续重试，请勿强制结束进程"
                )
                logger.error("输入安全清理超时；继续等待输入工作线程完成")
                if not self._close_timeout_reported:
                    self._close_timeout_reported = True
                    self._show_close_timeout_warning()
            return
        self._close_poll_timer.stop()
        self._close_pending = False
        self._final_close = True
        self.close()

    def _show_close_timeout_warning(self) -> None:
        """Show a non-blocking warning while the event loop keeps polling."""

        if not self.isVisible():
            return
        # Use an independent top-level dialog because the main window is
        # intentionally disabled throughout pending shutdown.
        warning = QMessageBox()
        warning.setIcon(QMessageBox.Icon.Warning)
        warning.setWindowTitle("安全关闭仍在进行")
        warning.setText("输入释放尚未完成，WinActionRecorder 将保持运行并自动重试。")
        warning.setInformativeText(
            "为避免残留按键或鼠标按下状态，请勿在此时通过任务管理器强制结束进程。"
        )
        warning.setStandardButtons(QMessageBox.StandardButton.Ok)
        warning.setModal(False)
        warning.finished.connect(self._clear_close_warning)
        self._close_warning = warning
        warning.show()

    def _clear_close_warning(self, _result: int = 0) -> None:
        self._close_warning = None

    def _finish_close(self, event: QCloseEvent) -> None:
        # QApplication must stay alive until every input worker has completed
        # its final release_all() cleanup path.
        if self._any_input_worker_running():
            self._final_close = False
            self._close_pending = True
            self._close_deadline = time.monotonic() + 5.0
            self._close_poll_timer.start()
            event.ignore()
            return
        self._record_countdown_timer.stop()
        self._record_status_timer.stop()
        self._close_poll_timer.stop()
        self._input_worker_completion_timer.stop()
        self._coordinate_picker.cancel()
        if self._close_warning is not None:
            self._close_warning.close()
            self._close_warning = None
        try:
            self._recorder.stop()
            self._player.close(timeout=0.0)
            self._autoclicker.close(timeout=0.0)
            self._macro_runner.close(timeout=0.0)
            self._hotkeys.stop()
        finally:
            self._state_unsubscribe()
            self._save_settings()
        event.accept()


__all__ = ["MainWindow", "WorkerBridge"]
