"""Full-screen coordinate picker for fixed-position mouse actions.

Qt window geometry is intentionally used only to cover each display.  On
Windows the selected value comes from ``GetCursorPos`` so it remains in the
same physical-pixel coordinate system that pynput consumes on mixed-DPI
desktops.
"""

from __future__ import annotations

import operator
import sys
from collections.abc import Callable, Iterable

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QScreen,
)
from PySide6.QtWidgets import QWidget

from ..screen import (
    CoordinateAnchor,
    ScreenConfig,
    ScreenConfigError,
    VirtualDesktop,
    capture_screen_config,
    compare_screen_configs,
    normalize_screen_config,
    validate_coordinate_for_screen,
    windows_per_monitor_dpi_context,
)


class CoordinatePickerError(RuntimeError):
    """The coordinate picker could not start or read a safe coordinate."""


PositionProvider = Callable[[], object]
ScreensProvider = Callable[[], Iterable[QScreen]]
ScreenConfigProvider = Callable[[], object]


def read_cursor_position() -> tuple[int, int]:
    """Return the cursor position in the coordinate system used for playback.

    Windows must use the native API here: Qt can expose device-independent
    global coordinates on mixed-DPI desktops while pynput uses native physical
    pixels.  Other platforms use Qt's global cursor position as a safe
    fallback.
    """

    if sys.platform == "win32":
        return _read_windows_cursor_position()
    point = QCursor.pos()
    return int(point.x()), int(point.y())


def _read_windows_cursor_position() -> tuple[int, int]:
    try:
        import ctypes
        from ctypes import wintypes

        class Point(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        with windows_per_monitor_dpi_context():
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_cursor_pos = user32.GetCursorPos
            get_cursor_pos.argtypes = [ctypes.POINTER(Point)]
            get_cursor_pos.restype = wintypes.BOOL
            point = Point()
            if not get_cursor_pos(ctypes.byref(point)):
                error_code = ctypes.get_last_error()
                raise CoordinatePickerError(
                    f"Windows 无法读取鼠标位置（错误代码 {error_code}）"
                )
            return int(point.x), int(point.y)
    except CoordinatePickerError:
        raise
    except ScreenConfigError as exc:
        raise CoordinatePickerError(f"Windows DPI 坐标模式不安全：{exc}") from exc
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise CoordinatePickerError(f"Windows 无法读取鼠标位置：{exc}") from exc


def _default_screens() -> list[QScreen]:
    app = QGuiApplication.instance()
    if app is None:
        return []
    try:
        return list(app.screens())
    except (AttributeError, RuntimeError, TypeError):
        return []


def _default_screen_config() -> object:
    app = QGuiApplication.instance()
    if app is None:
        raise CoordinatePickerError("Qt 图形应用尚未启动")
    return capture_screen_config(app)


class _CoordinateOverlay(QWidget):
    """One translucent overlay; the controller creates one per QScreen."""

    left_clicked = Signal()
    cancel_requested = Signal()
    closed_unexpectedly = Signal(object)

    _PROMPT = "TARGET SELECT // 点击目标位置"
    _ACCESSIBLE_PROMPT = "点击你想连续点击的位置"
    _HELP = "左键确认  ·  右键或 Esc 取消"

    def __init__(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        super().__init__(None, flags)
        self.setObjectName("coordinatePickerOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setWindowTitle(self._PROMPT)
        self.setAccessibleName(self._ACCESSIBLE_PROMPT)
        self.setAccessibleDescription(self._HELP)
        self._pressed_button = Qt.MouseButton.NoButton

    def present(self, screen: QScreen) -> None:
        """Place this native window over exactly one Qt screen."""

        geometry = screen.geometry()
        self.setGeometry(geometry)

        # Creating the QWindow first lets Qt associate it with the desired
        # monitor before it becomes visible.  Geometry remains in Qt's logical
        # space; the eventual picked value does not.
        self.winId()
        window = self.windowHandle()
        if window is not None:
            window.setScreen(screen)
        self.setGeometry(geometry)
        self.show()
        self.raise_()
        self.activateWindow()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.RightButton,
        ):
            event.accept()
            self._pressed_button = event.button()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        button = event.button()
        if button == self._pressed_button and button in (
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.RightButton,
        ):
            event.accept()
            self._pressed_button = Qt.MouseButton.NoButton
            if button == Qt.MouseButton.LeftButton:
                self.left_clicked.emit()
            else:
                self.cancel_requested.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.cancel_requested.emit()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed_unexpectedly.emit(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(19, 28, 46, 178))

        width = max(300.0, min(560.0, float(self.width()) - 48.0))
        height = 132.0
        panel = QRectF(
            float(int((self.width() - width) / 2.0)),
            float(int(max(24.0, (self.height() - height) / 2.0))),
            width,
            height,
        )
        shadow = panel.translated(9.0, 9.0)
        painter.fillRect(shadow, QColor("#0C1426"))
        painter.setPen(QPen(QColor("#17233B"), 4.0))
        painter.setBrush(QColor("#F5F2E8"))
        painter.drawRect(panel)
        painter.fillRect(
            QRectF(panel.left() + 2.0, panel.top() + 2.0, width - 4.0, 10.0),
            QColor("#36C5D8"),
        )

        title_rect = QRectF(panel.left() + 24.0, panel.top() + 27.0, width - 48.0, 38.0)
        painter.setPen(QColor("#17233B"))
        painter.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.Bold))
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignCenter,
            self._PROMPT,
        )

        help_rect = QRectF(panel.left() + 24.0, panel.top() + 78.0, width - 48.0, 30.0)
        painter.setPen(QColor("#556377"))
        painter.setFont(QFont("Cascadia Mono", 10, QFont.Weight.DemiBold))
        painter.drawText(
            help_rect,
            Qt.AlignmentFlag.AlignCenter,
            self._HELP,
        )
        painter.end()


class CoordinatePicker(QObject):
    """Manage full-screen overlays and emit one selected physical coordinate."""

    coordinate_picked = Signal(int, int)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        position_provider: PositionProvider | None = None,
        screens_provider: ScreensProvider | None = None,
        screen_config_provider: ScreenConfigProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self._position_provider = position_provider or read_cursor_position
        self._screens_provider = screens_provider or _default_screens
        # Custom position providers are used by tests and integrations whose
        # coordinate space may not be the native desktop.  Production uses the
        # default provider and therefore always enables topology validation;
        # integrations can opt in with an explicit matching config provider.
        self._screen_config_provider = (
            screen_config_provider
            if screen_config_provider is not None
            else (_default_screen_config if position_provider is None else None)
        )
        self._screen_snapshot: dict[str, object] | None = None
        self._last_selection_anchor: CoordinateAnchor | None = None
        self._overlays: list[_CoordinateOverlay] = []
        self._active = False
        self._cleaning_up = False
        self._event_source: QObject | None = None

    @property
    def is_active(self) -> bool:
        """Whether overlays are currently waiting for a selection."""

        return self._active

    @property
    def last_selection_anchor(self) -> CoordinateAnchor | None:
        """Metadata for the last successful native selection.

        A later cancellation or failed attempt deliberately retains this value
        so callers never lose an already confirmed scheme.  It remains
        ``None`` when an integration supplies a custom position provider
        without a matching screen-config provider.
        """

        return self._last_selection_anchor

    def start(self) -> bool:
        """Show one overlay on every screen; return whether picking started."""

        if self._active:
            return False

        try:
            screens = list(self._screens_provider())
        except Exception as exc:  # noqa: BLE001 - provider is an integration boundary
            self._fail(f"无法读取显示器列表：{exc}")
            return False
        if not screens:
            self._fail("未检测到可用显示器，无法选择坐标。")
            return False

        try:
            self._screen_snapshot = self._capture_screen_config()
        except Exception as exc:  # noqa: BLE001 - platform/provider boundary
            self._fail(f"无法记录取点前的显示器状态：{exc}")
            return False

        try:
            for screen in screens:
                overlay = self._create_overlay()
                self._overlays.append(overlay)

            self._active = True
            self._install_event_filter()
            for overlay, screen in zip(tuple(self._overlays), screens, strict=True):
                overlay.present(screen)
        except Exception as exc:  # noqa: BLE001 - Qt/platform failures must clean up
            self._active = False
            self._screen_snapshot = None
            self._remove_event_filter()
            self._cleanup_overlays()
            self._fail(f"无法打开坐标选择界面：{exc}")
            return False
        return self._active

    def cancel(self) -> bool:
        """Cancel an active selection and close every overlay."""

        if not self._active:
            return False
        self._active = False
        self._screen_snapshot = None
        self._remove_event_filter()
        self._cleanup_overlays()
        self.cancelled.emit()
        return True

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        del watched
        if (
            self._active
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            self.cancel()
            return True
        if (
            self._active
            and event.type() == QEvent.Type.MouseButtonRelease
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.RightButton
        ):
            self.cancel()
            return True
        return False

    def _create_overlay(self) -> _CoordinateOverlay:
        overlay = _CoordinateOverlay()
        overlay.left_clicked.connect(self._select_position)
        overlay.cancel_requested.connect(self.cancel)
        overlay.closed_unexpectedly.connect(self._overlay_closed)
        token = id(overlay)
        overlay.destroyed.connect(
            lambda _object=None, token=token: self._overlay_destroyed(token)
        )
        return overlay

    def _select_position(self) -> None:
        if not self._active:
            return
        try:
            x, y = _normalize_position(self._position_provider())
        except Exception as exc:  # noqa: BLE001 - injected/native provider boundary
            self._active = False
            self._screen_snapshot = None
            self._remove_event_filter()
            self._cleanup_overlays()
            self._fail(f"无法读取所选坐标：{exc}")
            return
        selection_anchor: CoordinateAnchor | None = None
        if self._screen_snapshot is not None:
            try:
                current = self._capture_screen_config()
                if current is None:
                    raise CoordinatePickerError("无法读取当前显示器状态")
                comparison = compare_screen_configs(self._screen_snapshot, current)
                if comparison.changed:
                    raise CoordinatePickerError(
                        "取点期间显示器布局、缩放或全屏显示模式发生了变化。"
                        "本次坐标已丢弃，请保持目标界面稳定后重新取点"
                    )
                validate_coordinate_for_screen(x, y, current)
                structured = ScreenConfig.from_dict(current)
                monitor = next(item for item in structured.monitors if item.contains(x, y))
                selection_anchor = CoordinateAnchor(
                    x=x,
                    y=y,
                    source_rect=VirtualDesktop(
                        monitor.x,
                        monitor.y,
                        monitor.width,
                        monitor.height,
                    ),
                    monitor_id=monitor.id,
                )
            except Exception as exc:  # noqa: BLE001 - provider/native boundary
                self._active = False
                self._screen_snapshot = None
                self._remove_event_filter()
                self._cleanup_overlays()
                self._fail(f"所选坐标当前不可安全使用：{exc}")
                return

        self._active = False
        self._screen_snapshot = None
        self._remove_event_filter()
        self._cleanup_overlays()
        self._last_selection_anchor = selection_anchor
        self.coordinate_picked.emit(x, y)

    def _capture_screen_config(self) -> dict[str, object] | None:
        provider = self._screen_config_provider
        if provider is None:
            return None
        if (
            provider is _default_screen_config
            and self._position_provider is not read_cursor_position
        ):
            # A replaced/custom position provider may intentionally use a
            # synthetic coordinate space (the UI tests do this to guarantee
            # that no native cursor is touched).  Native topology validation
            # is only meaningful when both default providers are paired.
            return None
        value = provider()
        return normalize_screen_config(value)  # type: ignore[arg-type,return-value]

    def _overlay_closed(self, overlay: object) -> None:
        if self._cleaning_up or not self._active:
            return
        self._overlays = [item for item in self._overlays if item is not overlay]
        try:
            if isinstance(overlay, QWidget):
                overlay.deleteLater()
        except RuntimeError:
            pass
        self.cancel()

    def _overlay_destroyed(self, token: int) -> None:
        previous_count = len(self._overlays)
        self._overlays = [item for item in self._overlays if id(item) != token]
        if self._active and len(self._overlays) != previous_count:
            self.cancel()

    def _install_event_filter(self) -> None:
        app = QGuiApplication.instance()
        if app is None:
            raise CoordinatePickerError("Qt 图形应用尚未启动")
        app.installEventFilter(self)
        self._event_source = app

    def _remove_event_filter(self) -> None:
        source, self._event_source = self._event_source, None
        if source is None:
            return
        try:
            source.removeEventFilter(self)
        except RuntimeError:
            pass

    def _cleanup_overlays(self) -> None:
        overlays, self._overlays = self._overlays, []
        self._cleaning_up = True
        try:
            for overlay in overlays:
                try:
                    overlay.hide()
                    overlay.close()
                    overlay.deleteLater()
                except RuntimeError:
                    # The native window may already have been destroyed by the
                    # desktop/session while the picker was active.
                    continue
        finally:
            self._cleaning_up = False

    def _fail(self, message: str) -> None:
        self.failed.emit(message)


def _normalize_position(position: object) -> tuple[int, int]:
    if hasattr(position, "x") and hasattr(position, "y"):
        x_member = position.x  # type: ignore[attr-defined]
        y_member = position.y  # type: ignore[attr-defined]
        x = x_member() if callable(x_member) else x_member
        y = y_member() if callable(y_member) else y_member
    else:
        try:
            x, y = position  # type: ignore[misc]
        except (TypeError, ValueError) as exc:
            raise CoordinatePickerError("坐标提供器必须返回两个整数") from exc

    if isinstance(x, bool) or isinstance(y, bool):
        raise CoordinatePickerError("坐标必须是整数")
    try:
        normalized_x = operator.index(x)
        normalized_y = operator.index(y)
    except TypeError as exc:
        raise CoordinatePickerError("坐标必须是整数") from exc
    if not -(2**31) <= normalized_x <= 2**31 - 1:
        raise CoordinatePickerError("坐标 X 超出支持范围")
    if not -(2**31) <= normalized_y <= 2**31 - 1:
        raise CoordinatePickerError("坐标 Y 超出支持范围")
    return normalized_x, normalized_y


__all__ = [
    "CoordinatePicker",
    "CoordinatePickerError",
    "read_cursor_position",
]
