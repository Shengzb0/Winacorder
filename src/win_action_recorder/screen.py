"""显示器配置采集、校验及回放前变化检测。"""

from __future__ import annotations

import math
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class ScreenConfigError(ValueError):
    """显示器配置无效或无法采集。"""


@dataclass(frozen=True, slots=True)
class VirtualDesktop:
    """虚拟桌面的边界矩形，坐标允许为负数。"""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _validate_coordinate(self.x, "虚拟桌面 x")
        _validate_coordinate(self.y, "虚拟桌面 y")
        _validate_positive_size(self.width, "虚拟桌面宽度")
        _validate_positive_size(self.height, "虚拟桌面高度")

    @property
    def right(self) -> int:
        """右边界（不包含）。"""

        return self.x + self.width

    @property
    def bottom(self) -> int:
        """下边界（不包含）。"""

        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def to_dict(self) -> dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> VirtualDesktop:
        data = _require_mapping(raw, "virtual_desktop")
        _validate_fields(
            data,
            required={"x", "y", "width", "height"},
            allowed={"x", "y", "width", "height"},
            label="virtual_desktop",
        )
        return cls(
            x=data["x"],
            y=data["y"],
            width=data["width"],
            height=data["height"],
        )


@dataclass(frozen=True, slots=True)
class CoordinateAnchor:
    """A selected physical point anchored to its source monitor rectangle."""

    x: int
    y: int
    source_rect: VirtualDesktop
    monitor_id: str | None = None

    def __post_init__(self) -> None:
        _validate_coordinate(self.x, "锚点 x")
        _validate_coordinate(self.y, "锚点 y")
        if not isinstance(self.source_rect, VirtualDesktop):
            raise ScreenConfigError("锚点 source_rect 必须是 VirtualDesktop")
        if not self.source_rect.contains(self.x, self.y):
            raise ScreenConfigError("锚点坐标必须位于 source_rect 内")
        if self.monitor_id is not None and (
            not isinstance(self.monitor_id, str) or not self.monitor_id.strip()
        ):
            raise ScreenConfigError("锚点 monitor_id 必须是非空字符串或 null")

    def map_to(self, target_rect: VirtualDesktop) -> tuple[int, int]:
        """Map the anchor to the same relative pixel in another rectangle."""

        return map_coordinate_between_rects(
            self.x,
            self.y,
            self.source_rect,
            target_rect,
        )


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """一个 Qt 屏幕的逻辑坐标配置。"""

    id: str
    x: int
    y: int
    width: int
    height: int
    is_primary: bool
    device_pixel_ratio: float | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ScreenConfigError("显示器 id 必须是非空字符串")
        if len(self.id) > 256:
            raise ScreenConfigError("显示器 id 过长")
        if self.name is not None and not isinstance(self.name, str):
            raise ScreenConfigError("显示器 name 必须是字符串或 null")
        _validate_coordinate(self.x, f"显示器 {self.id} 的 x")
        _validate_coordinate(self.y, f"显示器 {self.id} 的 y")
        _validate_positive_size(self.width, f"显示器 {self.id} 的宽度")
        _validate_positive_size(self.height, f"显示器 {self.id} 的高度")
        if not isinstance(self.is_primary, bool):
            raise ScreenConfigError(f"显示器 {self.id} 的 is_primary 必须是布尔值")
        _validate_dpr(self.device_pixel_ratio, f"显示器 {self.id}")

    @property
    def geometry_signature(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)

    @property
    def right(self) -> int:
        """Right edge in the native desktop coordinate space (exclusive)."""

        return self.x + self.width

    @property
    def bottom(self) -> int:
        """Bottom edge in the native desktop coordinate space (exclusive)."""

        return self.y + self.height

    def contains(self, x: int, y: int) -> bool:
        """Return whether a point lands on this physical display rectangle."""

        return self.x <= x < self.right and self.y <= y < self.bottom

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name if self.name is not None else self.id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "is_primary": self.is_primary,
        }
        if self.device_pixel_ratio is not None:
            result["device_pixel_ratio"] = self.device_pixel_ratio
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, index: int) -> MonitorConfig:
        label = f"第 {index + 1} 个显示器"
        data = _require_mapping(raw, label)
        _validate_fields(
            data,
            required={"id", "x", "y", "width", "height", "is_primary"},
            allowed={
                "id",
                "name",
                "x",
                "y",
                "width",
                "height",
                "is_primary",
                "device_pixel_ratio",
            },
            label=label,
        )
        return cls(
            id=data["id"],
            name=data.get("name"),
            x=data["x"],
            y=data["y"],
            width=data["width"],
            height=data["height"],
            is_primary=data["is_primary"],
            device_pixel_ratio=data.get("device_pixel_ratio"),
        )


@dataclass(frozen=True, slots=True)
class ScreenConfig:
    """可序列化的完整显示器配置。"""

    virtual_desktop: VirtualDesktop
    monitors: tuple[MonitorConfig, ...]
    primary_monitor: str
    device_pixel_ratio: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.virtual_desktop, VirtualDesktop):
            raise ScreenConfigError("virtual_desktop 配置无效")
        if not isinstance(self.monitors, tuple) or not self.monitors:
            raise ScreenConfigError("screen_config 至少需要一个显示器")
        if any(not isinstance(item, MonitorConfig) for item in self.monitors):
            raise ScreenConfigError("monitors 中包含无效的显示器配置")
        identifiers = [monitor.id for monitor in self.monitors]
        if len(set(identifiers)) != len(identifiers):
            raise ScreenConfigError("显示器 id 不能重复")
        if not isinstance(self.primary_monitor, str) or not self.primary_monitor:
            raise ScreenConfigError("primary_monitor 必须是非空字符串")
        if self.primary_monitor not in identifiers:
            raise ScreenConfigError("primary_monitor 未指向 monitors 中的显示器")
        primaries = [monitor for monitor in self.monitors if monitor.is_primary]
        if len(primaries) != 1:
            raise ScreenConfigError("monitors 中必须且只能有一个主显示器")
        if primaries[0].id != self.primary_monitor:
            raise ScreenConfigError("primary_monitor 与 is_primary 标识不一致")
        _validate_dpr(self.device_pixel_ratio, "主显示器")
        primary_dpr = primaries[0].device_pixel_ratio
        if (
            self.device_pixel_ratio is not None
            and primary_dpr is not None
            and not math.isclose(
                float(self.device_pixel_ratio),
                float(primary_dpr),
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            raise ScreenConfigError(
                "顶层 device_pixel_ratio 与主显示器的缩放比例不一致"
            )
        expected_virtual = _union_geometry(self.monitors)
        if self.virtual_desktop != expected_virtual:
            raise ScreenConfigError(
                "virtual_desktop 与各显示器位置、尺寸计算出的范围不一致"
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "virtual_desktop": self.virtual_desktop.to_dict(),
            "monitors": [monitor.to_dict() for monitor in self.monitors],
            "primary_monitor": self.primary_monitor,
        }
        if self.device_pixel_ratio is not None:
            result["device_pixel_ratio"] = self.device_pixel_ratio
        return result

    def contains_coordinate(self, x: int, y: int) -> bool:
        """Return whether a point belongs to any real monitor rectangle.

        ``virtual_desktop`` is only the bounding rectangle of all monitors.
        When displays are staggered it contains empty gaps where Windows
        cannot place the cursor, so it is not a safe injection predicate.
        """

        return any(monitor.contains(x, y) for monitor in self.monitors)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ScreenConfig:
        data = _require_mapping(raw, "screen_config")
        _validate_fields(
            data,
            required={"virtual_desktop", "monitors", "primary_monitor"},
            allowed={
                "virtual_desktop",
                "monitors",
                "primary_monitor",
                "device_pixel_ratio",
            },
            label="screen_config",
        )
        raw_monitors = data["monitors"]
        if not isinstance(raw_monitors, list):
            raise ScreenConfigError("screen_config.monitors 必须是数组")
        monitors = tuple(
            MonitorConfig.from_dict(item, index=index)
            for index, item in enumerate(raw_monitors)
        )
        return cls(
            virtual_desktop=VirtualDesktop.from_dict(data["virtual_desktop"]),
            monitors=monitors,
            primary_monitor=data["primary_monitor"],
            device_pixel_ratio=data.get("device_pixel_ratio"),
        )


@dataclass(frozen=True, slots=True)
class ScreenComparison:
    """显示器配置比较结果；布尔值表示“配置一致”。"""

    matches: bool
    differences: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matches

    @property
    def changed(self) -> bool:
        return not self.matches

    @property
    def is_compatible(self) -> bool:
        return self.matches

    @property
    def warning_message(self) -> str:
        if self.matches:
            return "当前显示器配置与录制时一致。"
        details = "；".join(self.differences)
        return (
            "检测到显示器配置已变化，动作坐标可能错位。"
            f"{details}。默认将取消回放；请仅在确认坐标安全后选择“仍然继续”。"
        )


def enable_per_monitor_dpi_awareness() -> bool:
    """Request one physical-pixel coordinate system before Qt/pynput start.

    Qt 6 normally enables per-monitor DPI awareness itself.  Calling the
    native API explicitly before ``QApplication`` also makes pynput's listener
    and controller agree on physical coordinates.  ``False`` is harmless when
    Windows has already fixed the process awareness from its manifest.
    """

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if setter is not None:
            setter.argtypes = [ctypes.c_void_p]
            setter.restype = ctypes.c_bool
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
            if setter(ctypes.c_void_p(-4)):
                awareness = _windows_thread_dpi_awareness(user32)
                return awareness in (None, 2)
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        legacy_setter = getattr(shcore, "SetProcessDpiAwareness", None)
        if legacy_setter is not None:
            legacy_setter.argtypes = [ctypes.c_int]
            legacy_setter.restype = ctypes.c_long
            # PROCESS_PER_MONITOR_DPI_AWARE == 2.  E_ACCESSDENIED only means
            # that awareness was selected earlier; it does *not* prove that
            # the selected mode is per-monitor aware, so verify the effective
            # thread mode instead of treating that result as automatically
            # safe.
            result = legacy_setter(2)
            awareness = _windows_thread_dpi_awareness(user32)
            if awareness is not None:
                return awareness == 2
            return result == 0
    except (AttributeError, OSError, ValueError):
        return False
    return False


def _windows_thread_dpi_awareness(user32: Any | None = None) -> int | None:
    """Return Win32 ``PROCESS_DPI_AWARENESS`` for the current thread."""

    if sys.platform != "win32":
        return None
    try:
        import ctypes

        native = user32 or ctypes.WinDLL("user32", use_last_error=True)
        get_context = getattr(native, "GetThreadDpiAwarenessContext", None)
        get_awareness = getattr(native, "GetAwarenessFromDpiAwarenessContext", None)
        if get_context is None or get_awareness is None:
            return None
        get_context.argtypes = []
        get_context.restype = ctypes.c_void_p
        get_awareness.argtypes = [ctypes.c_void_p]
        get_awareness.restype = ctypes.c_int
        return int(get_awareness(get_context()))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


@contextmanager
def windows_per_monitor_dpi_context() -> Iterator[None]:
    """Temporarily force Win32 calls onto one physical-pixel coordinate space.

    Playback runs on a worker thread.  A process-level DPI request made on the
    Qt thread is not sufficient evidence that every later worker still has a
    per-monitor-aware context (a host or native component may override it).
    Applying PMv2 at each native coordinate boundary prevents ``GetCursorPos``,
    monitor geometry and ``SetCursorPos`` from silently using different
    virtualised coordinate systems.
    """

    if sys.platform != "win32":
        yield
        return

    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if setter is None:
            if _windows_thread_dpi_awareness(user32) != 2:
                raise ScreenConfigError(
                    "当前 Windows 线程不是每显示器 DPI 感知模式，"
                    "无法安全换算鼠标物理坐标"
                )
            yield
            return

        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        previous = setter(ctypes.c_void_p(-4))
        if not previous:
            error_code = ctypes.get_last_error()
            if _windows_thread_dpi_awareness(user32) != 2:
                raise ScreenConfigError(
                    "无法切换到每显示器 DPI 感知模式"
                    f"（Windows 错误代码 {error_code}）"
                )
            yield
            return
        try:
            yield
        finally:
            setter(ctypes.c_void_p(previous))
    except ScreenConfigError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ScreenConfigError(f"无法确认 Windows DPI 坐标模式：{exc}") from exc


def collect_screen_config(gui_app: Any | None = None) -> ScreenConfig:
    """从现有 ``QGuiApplication`` 采集显示器配置。

    ``gui_app`` 参数允许测试传入 fake，也避免本模块为了采集配置而私自创建
    Qt 应用或阻塞主线程。
    """

    app = gui_app
    if app is None:
        try:
            from PySide6.QtGui import QGuiApplication
        except ImportError as exc:  # pragma: no cover - 正常安装固定依赖
            raise ScreenConfigError("无法获取显示器配置：PySide6 未安装") from exc
        app = QGuiApplication.instance()
    if app is None:
        raise ScreenConfigError("无法获取显示器配置：Qt 图形应用尚未启动")

    if sys.platform == "win32" and _is_real_qt_application(app):
        with windows_per_monitor_dpi_context():
            return _collect_windows_native_screen_config(app)

    try:
        raw_screens = app.screens()
        screens = list(raw_screens)
        primary_screen = app.primaryScreen()
    except Exception as exc:
        raise ScreenConfigError(f"无法读取系统显示器配置：{exc}") from exc
    if not screens:
        raise ScreenConfigError("系统未报告任何可用显示器")

    identifiers: set[str] = set()
    screen_ids: dict[int, str] = {}
    for index, screen in enumerate(screens):
        base = _screen_name(screen) or f"display-{index + 1}"
        identifier = base
        suffix = 2
        while identifier in identifiers:
            identifier = f"{base}#{suffix}"
            suffix += 1
        identifiers.add(identifier)
        screen_ids[id(screen)] = identifier

    if primary_screen is None or id(primary_screen) not in screen_ids:
        # 某些 fake/平台封装会返回不同的 Python wrapper；尝试使用名称匹配。
        primary_name = _screen_name(primary_screen) if primary_screen else ""
        matches = [
            screen
            for screen in screens
            if primary_name and _screen_name(screen) == primary_name
        ]
        if len(matches) == 1:
            primary_screen = matches[0]
        else:
            raise ScreenConfigError("无法确定系统主显示器")

    monitors: list[MonitorConfig] = []
    primary_id = screen_ids[id(primary_screen)]
    for screen in screens:
        identifier = screen_ids[id(screen)]
        try:
            geometry = screen.geometry()
            x = _numeric_member(geometry, "x")
            y = _numeric_member(geometry, "y")
            width = _numeric_member(geometry, "width")
            height = _numeric_member(geometry, "height")
            dpr = _optional_float_member(screen, "devicePixelRatio")
        except Exception as exc:
            raise ScreenConfigError(
                f"无法读取显示器 {identifier} 的位置或尺寸：{exc}"
            ) from exc
        monitors.append(
            MonitorConfig(
                id=identifier,
                name=_screen_name(screen) or identifier,
                x=x,
                y=y,
                width=width,
                height=height,
                is_primary=identifier == primary_id,
                device_pixel_ratio=dpr,
            )
        )

    monitor_tuple = tuple(monitors)
    primary = next(item for item in monitor_tuple if item.is_primary)
    return ScreenConfig(
        virtual_desktop=_union_geometry(monitor_tuple),
        monitors=monitor_tuple,
        primary_monitor=primary.id,
        device_pixel_ratio=primary.device_pixel_ratio,
    )


def _is_real_qt_application(app: Any) -> bool:
    """Keep fake screen providers deterministic while selecting Win32 in app."""

    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:
        return False
    return app is QGuiApplication.instance()


def _collect_windows_native_screen_config(gui_app: Any) -> ScreenConfig:
    """Collect physical-pixel monitor rectangles through Win32.

    ``pynput`` supplies and consumes native Windows coordinates.  QScreen
    geometry is device-independent on mixed-DPI desktops, so it must not be
    used as the validation coordinate space for recorded actions.
    """

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        monitor_handle = wintypes.HANDLE
        enum_proc_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            monitor_handle,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = [monitor_handle, ctypes.POINTER(MONITORINFOEXW)]
        get_monitor_info.restype = wintypes.BOOL
        enum_monitors = user32.EnumDisplayMonitors
        enum_monitors.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            enum_proc_type,
            wintypes.LPARAM,
        ]
        enum_monitors.restype = wintypes.BOOL

        monitors: list[MonitorConfig] = []
        callback_errors: list[str] = []

        @enum_proc_type
        def receive_monitor(
            handle: Any,
            _device_context: Any,
            _rect: Any,
            _data: int,
        ) -> bool:
            try:
                info = MONITORINFOEXW()
                info.cbSize = ctypes.sizeof(MONITORINFOEXW)
                if not get_monitor_info(handle, ctypes.byref(info)):
                    callback_errors.append("GetMonitorInfoW 调用失败")
                    return False
                rect = info.rcMonitor
                identifier = str(info.szDevice).rstrip("\x00")
                if not identifier:
                    identifier = f"display-{len(monitors) + 1}"
                dpr = _windows_monitor_dpr(handle, identifier, gui_app)
                monitors.append(
                    MonitorConfig(
                        id=identifier,
                        name=identifier,
                        x=int(rect.left),
                        y=int(rect.top),
                        width=int(rect.right - rect.left),
                        height=int(rect.bottom - rect.top),
                        is_primary=bool(info.dwFlags & 1),
                        device_pixel_ratio=dpr,
                    )
                )
                return True
            except Exception as exc:  # noqa: BLE001 - ctypes callback boundary
                callback_errors.append(
                    f"枚举显示器失败（{type(exc).__name__}）"
                )
                return False

        enumeration_succeeded = enum_monitors(None, None, receive_monitor, 0)
        if callback_errors:
            raise ScreenConfigError(callback_errors[0])
        if not enumeration_succeeded:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "EnumDisplayMonitors 调用失败")
        if not monitors:
            raise ScreenConfigError("系统未报告任何可用显示器")
        if sum(item.is_primary for item in monitors) != 1:
            raise ScreenConfigError("无法从 Windows 确定唯一主显示器")

        monitor_tuple = tuple(monitors)
        virtual_desktop = _union_geometry(monitor_tuple)
        # Cross-check native virtual-screen metrics instead of mixing in Qt's
        # device-independent geometry.
        native_virtual = VirtualDesktop(
            x=int(user32.GetSystemMetrics(76)),
            y=int(user32.GetSystemMetrics(77)),
            width=int(user32.GetSystemMetrics(78)),
            height=int(user32.GetSystemMetrics(79)),
        )
        if virtual_desktop != native_virtual:
            raise ScreenConfigError(
                "Windows 虚拟桌面范围与显示器枚举结果不一致，无法安全使用绝对坐标"
            )
        primary = next(item for item in monitor_tuple if item.is_primary)
        return ScreenConfig(
            virtual_desktop=virtual_desktop,
            monitors=monitor_tuple,
            primary_monitor=primary.id,
            device_pixel_ratio=primary.device_pixel_ratio,
        )
    except ScreenConfigError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ScreenConfigError(f"无法读取 Windows 原生显示器配置：{exc}") from exc


def _windows_monitor_dpr(handle: Any, identifier: str, gui_app: Any) -> float | None:
    """Read effective monitor DPI, with Qt name matching as a fallback."""

    try:
        import ctypes

        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        get_dpi = shcore.GetDpiForMonitor
        get_dpi.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        get_dpi.restype = ctypes.c_long
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        if (
            get_dpi(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0
            and dpi_x.value > 0
        ):
            return round(dpi_x.value / 96.0, 6)
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    wanted = identifier.lower().replace("\\.\\", "")
    try:
        for screen in gui_app.screens():
            name = _screen_name(screen).lower().replace("\\.\\", "")
            if name == wanted:
                return _optional_float_member(screen, "devicePixelRatio")
    except Exception:  # noqa: BLE001 - dynamic Qt screen boundary
        return None
    return None


def capture_screen_config(gui_app: Any | None = None) -> dict[str, Any]:
    """采集并返回可直接写入 JSON 的显示器配置。"""

    return collect_screen_config(gui_app).to_dict()


def get_current_screen_config(gui_app: Any | None = None) -> dict[str, Any]:
    """``capture_screen_config`` 的兼容别名。"""

    return capture_screen_config(gui_app)


def normalize_screen_config(config: ScreenConfig | Mapping[str, Any]) -> dict[str, Any]:
    """严格校验配置并返回规范化的独立字典。"""

    return _coerce_screen_config(config).to_dict()


def validate_screen_config(config: ScreenConfig | Mapping[str, Any]) -> None:
    """严格校验配置；无返回值表示通过。"""

    _coerce_screen_config(config)


def compare_screen_configs(
    recorded: ScreenConfig | Mapping[str, Any],
    current: ScreenConfig | Mapping[str, Any] | None = None,
    *,
    gui_app: Any | None = None,
) -> ScreenComparison:
    """比较录制时和当前配置，返回适合 GUI 决策的结构化结果。"""

    old = _coerce_screen_config(recorded)
    new = collect_screen_config(gui_app) if current is None else _coerce_screen_config(current)
    differences: list[str] = []

    if len(old.monitors) != len(new.monitors):
        differences.append(
            f"显示器数量从 {len(old.monitors)} 个变为 {len(new.monitors)} 个"
        )
    if old.virtual_desktop != new.virtual_desktop:
        differences.append(
            "虚拟桌面范围从 "
            f"{_format_geometry(old.virtual_desktop)} 变为 "
            f"{_format_geometry(new.virtual_desktop)}"
        )

    old_geometries = Counter(item.geometry_signature for item in old.monitors)
    new_geometries = Counter(item.geometry_signature for item in new.monitors)
    if old_geometries != new_geometries:
        differences.append("显示器位置或分辨率发生变化")

    old_primary = next(item for item in old.monitors if item.is_primary)
    new_primary = next(item for item in new.monitors if item.is_primary)
    if old_primary.geometry_signature != new_primary.geometry_signature:
        differences.append("主显示器或主显示器位置、分辨率发生变化")

    # 缩放会影响 Qt 的逻辑坐标，即使物理显示器几何值未变也应警告。
    old_dprs = sorted(_dpr_geometry_signature(item) for item in old.monitors)
    new_dprs = sorted(_dpr_geometry_signature(item) for item in new.monitors)
    if old_dprs != new_dprs:
        differences.append("显示器缩放比例 devicePixelRatio 发生变化")

    # 保持信息简洁，避免同一种差异被重复显示。
    unique_differences = tuple(dict.fromkeys(differences))
    return ScreenComparison(not unique_differences, unique_differences)


def screen_configs_match(
    recorded: ScreenConfig | Mapping[str, Any],
    current: ScreenConfig | Mapping[str, Any],
) -> bool:
    return compare_screen_configs(recorded, current).matches


def has_screen_config_changed(
    recorded: ScreenConfig | Mapping[str, Any],
    current: ScreenConfig | Mapping[str, Any],
) -> bool:
    return compare_screen_configs(recorded, current).changed


def is_coordinate_in_virtual_desktop(
    x: int, y: int, config: ScreenConfig | Mapping[str, Any]
) -> bool:
    _validate_coordinate(x, "x")
    _validate_coordinate(y, "y")
    # Kept as a compatibility name.  A point in the bounding rectangle but in
    # a staggered-monitor gap is not a usable desktop coordinate.
    return _coerce_screen_config(config).contains_coordinate(x, y)


def validate_coordinate_for_screen(
    x: int, y: int, config: ScreenConfig | Mapping[str, Any]
) -> None:
    """坐标不在任一真实显示器区域内时抛出可展示的中文错误。"""

    screen = _coerce_screen_config(config)
    _validate_coordinate(x, "x")
    _validate_coordinate(y, "y")
    if not screen.contains_coordinate(x, y):
        location = (
            "位于多显示器外接矩形的空白区域"
            if screen.virtual_desktop.contains(x, y)
            else "超出当前虚拟桌面范围"
        )
        raise ScreenConfigError(
            f"坐标 ({x}, {y}) {location}，未落在当前任一显示器的有效区域；"
            "请重新选取位置"
        )


def is_coordinate_on_current_screen(x: int, y: int) -> bool:
    """Check a point against the display topology effective *right now*.

    On Windows this uses ``MonitorFromPoint(..., MONITOR_DEFAULTTONULL)`` in a
    PMv2 thread context.  It is deliberately queried at injection time, after
    any countdown/Alt-Tab, so a stale pre-countdown validation cannot cause a
    click at a clamped cursor position.
    """

    _validate_coordinate(x, "x")
    _validate_coordinate(y, "y")
    if sys.platform != "win32":
        return collect_screen_config().contains_coordinate(x, y)
    try:
        import ctypes
        from ctypes import wintypes

        with windows_per_monitor_dpi_context():
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            monitor_from_point = user32.MonitorFromPoint
            monitor_from_point.argtypes = [wintypes.POINT, wintypes.DWORD]
            monitor_from_point.restype = wintypes.HANDLE
            handle = monitor_from_point(wintypes.POINT(x, y), 0)
            return bool(handle)
    except ScreenConfigError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ScreenConfigError(
            f"无法核对坐标 ({x}, {y}) 所在的当前显示器：{exc}"
        ) from exc


def get_cursor_clip_rect() -> VirtualDesktop | None:
    """Return the current Win32 ``ClipCursor`` rectangle in physical pixels.

    ``None`` means the platform has no Win32 cursor-clipping API.  On Windows,
    inability to read the rectangle is a safety error rather than evidence
    that clipping is disabled.  Windows reports the entire virtual desktop
    when no application has narrowed the cursor range.
    """

    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        with windows_per_monitor_dpi_context():
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_clip_cursor = user32.GetClipCursor
            get_clip_cursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
            get_clip_cursor.restype = wintypes.BOOL
            rect = wintypes.RECT()
            if not get_clip_cursor(ctypes.byref(rect)):
                error_code = ctypes.get_last_error()
                raise ScreenConfigError(
                    "Windows 无法读取当前鼠标活动范围"
                    f"（错误代码 {error_code}）"
                )
            return VirtualDesktop(
                int(rect.left),
                int(rect.top),
                int(rect.right - rect.left),
                int(rect.bottom - rect.top),
            )
    except ScreenConfigError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise ScreenConfigError(f"无法读取 Windows 鼠标活动范围：{exc}") from exc


def map_coordinate_between_rects(
    x: int,
    y: int,
    source_rect: VirtualDesktop,
    target_rect: VirtualDesktop,
) -> tuple[int, int]:
    """Map a point by relative inclusive pixel endpoints between rectangles.

    The first/last source pixels map exactly to the first/last target pixels.
    A one-pixel source or destination axis maps safely to the destination's
    only/first pixel and never divides by zero.  Callers must explicitly opt
    into this transformation; ordinary recorded playback remains absolute.
    """

    _validate_coordinate(x, "x")
    _validate_coordinate(y, "y")
    if not isinstance(source_rect, VirtualDesktop):
        raise ScreenConfigError("source_rect 必须是 VirtualDesktop")
    if not isinstance(target_rect, VirtualDesktop):
        raise ScreenConfigError("target_rect 必须是 VirtualDesktop")
    if not source_rect.contains(x, y):
        raise ScreenConfigError(f"源坐标 ({x}, {y}) 不在 source_rect 内")

    def map_axis(
        value: int,
        source_start: int,
        source_size: int,
        target_start: int,
        target_size: int,
    ) -> int:
        if source_size == 1 or target_size == 1:
            return target_start
        fraction = (value - source_start) / (source_size - 1)
        # Pixel endpoints are inclusive.  Round half up rather than relying on
        # Python's banker rounding so UI positions map predictably.
        return target_start + math.floor(fraction * (target_size - 1) + 0.5)

    return (
        map_axis(x, source_rect.x, source_rect.width, target_rect.x, target_rect.width),
        map_axis(y, source_rect.y, source_rect.height, target_rect.y, target_rect.height),
    )


def validate_action_coordinates(
    actions: Sequence[Any], config: ScreenConfig | Mapping[str, Any]
) -> None:
    """检查所有带坐标动作是否位于给定虚拟桌面内。"""

    screen = _coerce_screen_config(config)
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ScreenConfigError("actions 必须是动作数组")
    for index, action in enumerate(actions):
        params = getattr(action, "params", None)
        action_type = getattr(action, "type", None)
        action_value = getattr(action_type, "value", action_type)
        if action_value not in {"mouse_move", "mouse_button", "mouse_scroll"}:
            continue
        if not isinstance(params, Mapping):
            raise ScreenConfigError(f"第 {index + 1} 个鼠标动作参数无效")
        has_x = "x" in params
        has_y = "y" in params
        if has_x != has_y:
            raise ScreenConfigError(f"第 {index + 1} 个鼠标动作坐标不完整")
        # A button/scroll command may intentionally act at the cursor's
        # current position.  Mouse movement, however, is meaningless without
        # an explicit target and must remain strict.
        if not has_x:
            if action_value == "mouse_move":
                raise ScreenConfigError(f"第 {index + 1} 个鼠标移动动作缺少坐标")
            continue
        try:
            validate_coordinate_for_screen(params["x"], params["y"], screen)
        except ScreenConfigError as exc:
            raise ScreenConfigError(f"第 {index + 1} 个动作无效：{exc}") from exc


def _coerce_screen_config(
    config: ScreenConfig | Mapping[str, Any],
) -> ScreenConfig:
    if isinstance(config, ScreenConfig):
        # 冻结 dataclass，且构造时已完整校验。
        return config
    if not isinstance(config, Mapping):
        raise ScreenConfigError("screen_config 必须是对象")
    return ScreenConfig.from_dict(config)


def _union_geometry(monitors: Sequence[MonitorConfig]) -> VirtualDesktop:
    if not monitors:
        raise ScreenConfigError("至少需要一个显示器才能计算虚拟桌面")
    left = min(item.x for item in monitors)
    top = min(item.y for item in monitors)
    right = max(item.x + item.width for item in monitors)
    bottom = max(item.y + item.height for item in monitors)
    return VirtualDesktop(left, top, right - left, bottom - top)


def _screen_name(screen: Any) -> str:
    if screen is None:
        return ""
    try:
        value = _member(screen, "name")
    except Exception:  # noqa: BLE001 - optional dynamic Qt member
        return ""
    return value.strip() if isinstance(value, str) else ""


def _numeric_member(obj: Any, name: str) -> int:
    value = _member(obj, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} 不是整数")
    return value


def _optional_float_member(obj: Any, name: str) -> float | None:
    try:
        value = _member(obj, name)
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 不是数字")
    number = float(value)
    _validate_dpr(number, "显示器")
    return number


def _member(obj: Any, name: str) -> Any:
    value = getattr(obj, name)
    return value() if callable(value) else value


def _dpr_geometry_signature(
    monitor: MonitorConfig,
) -> tuple[int, int, int, int, float]:
    ratio = (
        -1.0
        if monitor.device_pixel_ratio is None
        else round(monitor.device_pixel_ratio, 4)
    )
    return (*monitor.geometry_signature, ratio)


def _format_geometry(rect: VirtualDesktop) -> str:
    return f"({rect.x}, {rect.y}, {rect.width}×{rect.height})"


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScreenConfigError(f"{label} 必须是对象")
    if any(not isinstance(key, str) for key in value):
        raise ScreenConfigError(f"{label} 的字段名必须是字符串")
    return value


def _validate_fields(
    data: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
) -> None:
    keys = set(data)
    missing = sorted(required - keys)
    if missing:
        raise ScreenConfigError(f"{label} 缺少必需字段：{', '.join(missing)}")
    unknown = sorted(keys - allowed)
    if unknown:
        raise ScreenConfigError(f"{label} 包含未知字段：{', '.join(unknown)}")


def _validate_coordinate(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScreenConfigError(f"{label} 必须是整数")
    if not -(2**31) <= value <= 2**31 - 1:
        raise ScreenConfigError(f"{label} 超出支持范围")


def _validate_positive_size(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScreenConfigError(f"{label} 必须是整数")
    if value <= 0:
        raise ScreenConfigError(f"{label} 必须大于 0")
    if value > 2**31 - 1:
        raise ScreenConfigError(f"{label} 超出支持范围")


def _validate_dpr(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScreenConfigError(f"{label} 的 device_pixel_ratio 必须是数字或 null")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ScreenConfigError(f"{label} 的 device_pixel_ratio 必须是有限正数")


__all__ = [
    "CoordinateAnchor",
    "MonitorConfig",
    "ScreenComparison",
    "ScreenConfig",
    "ScreenConfigError",
    "VirtualDesktop",
    "capture_screen_config",
    "collect_screen_config",
    "compare_screen_configs",
    "enable_per_monitor_dpi_awareness",
    "get_current_screen_config",
    "get_cursor_clip_rect",
    "has_screen_config_changed",
    "is_coordinate_in_virtual_desktop",
    "is_coordinate_on_current_screen",
    "map_coordinate_between_rects",
    "normalize_screen_config",
    "screen_configs_match",
    "validate_action_coordinates",
    "validate_coordinate_for_screen",
    "validate_screen_config",
    "windows_per_monitor_dpi_context",
]
