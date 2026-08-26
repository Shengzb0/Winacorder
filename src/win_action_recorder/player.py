"""Non-Qt action playback engine with interruptible timing and safe cleanup."""

from __future__ import annotations

import logging
import math
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from random import Random
from time import monotonic
from typing import Any, Final, Protocol, TypeAlias, runtime_checkable

from .hotkeys import is_control_key
from .screen import (
    ScreenConfigError,
    get_cursor_clip_rect,
    is_coordinate_on_current_screen,
    windows_per_monitor_dpi_context,
)
from .state import AppState, StateManager

logger = logging.getLogger(__name__)

MIN_PLAYBACK_SPEED: Final[float] = 0.1
MAX_PLAYBACK_SPEED: Final[float] = 10.0


class PlaybackError(RuntimeError):
    """Base class for friendly playback failures."""


class PlaybackConfigurationError(ValueError, PlaybackError):
    """Raised before playback when settings or actions are invalid."""


class InputInjectionError(PlaybackError):
    """Raised when the operating system input controller rejects an action."""


class PlaybackOutcome(str, Enum):
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlaybackSettings:
    """Validated playback settings.

    Action delays are divided by ``speed``.  The countdown, loop interval and
    explicit skipped-trajectory click interval are deliberate user waits and
    therefore are not affected by playback speed.
    """

    countdown_seconds: float = 3.0
    loop_count: int = 1
    infinite: bool = False
    loop_interval_ms: float = 0.0
    speed: float = 1.0
    ignore_mouse_moves: bool = False
    skip_move_click_interval_ms: float = 0.0
    loop_interval_random_offsets_ms: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        countdown = _finite_number(self.countdown_seconds, "回放倒计时", minimum=0.0)
        interval = _finite_number(self.loop_interval_ms, "循环间隔", minimum=0.0)
        click_interval = _finite_number(
            self.skip_move_click_interval_ms,
            "跳过移动轨迹时的最小点击间隔",
            minimum=0.0,
        )
        interval_random_offsets = _normalize_random_offsets(
            self.loop_interval_random_offsets_ms,
            "循环间隔随机偏移",
        )
        if sum(interval_random_offsets) > interval:
            raise PlaybackConfigurationError(
                "循环间隔随机偏移总量不能大于循环间隔，否则可能产生负等待"
            )
        speed = validate_speed(self.speed)
        if isinstance(self.loop_count, bool) or not isinstance(self.loop_count, int):
            raise PlaybackConfigurationError("循环次数必须是正整数")
        if self.loop_count < 1:
            raise PlaybackConfigurationError("循环次数必须至少为 1")
        if not isinstance(self.infinite, bool):
            raise PlaybackConfigurationError("无限循环设置必须是布尔值")
        if not isinstance(self.ignore_mouse_moves, bool):
            raise PlaybackConfigurationError("忽略鼠标移动设置必须是布尔值")
        object.__setattr__(self, "countdown_seconds", countdown)
        object.__setattr__(self, "loop_interval_ms", interval)
        object.__setattr__(self, "skip_move_click_interval_ms", click_interval)
        object.__setattr__(
            self,
            "loop_interval_random_offsets_ms",
            interval_random_offsets,
        )
        object.__setattr__(self, "speed", speed)

    @property
    def total_loops(self) -> int | None:
        return None if self.infinite else self.loop_count

    @property
    def repeat_count(self) -> int | None:
        """Compatibility/readability alias; ``None`` means infinite."""

        return self.total_loops


@dataclass(frozen=True, slots=True)
class PlaybackEvent:
    """A thread-safe value object emitted at playback milestones.

    Loop and action positions are one-based, with zero meaning "not started".
    Callbacks execute on the worker/control thread; Qt clients should forward
    the event through a Signal before updating widgets.
    """

    kind: str
    state: AppState
    message: str = ""
    loop_index: int = 0
    total_loops: int | None = 1
    action_index: int = 0
    total_actions: int = 0
    countdown_remaining: int | None = None
    error: str | None = None
    timestamp: float = field(default_factory=monotonic)


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    outcome: PlaybackOutcome
    loops_completed: int
    actions_executed: int
    error: str | None = None
    cleanup_errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome is PlaybackOutcome.COMPLETED


@runtime_checkable
class InputController(Protocol):
    """Small injectable controller surface used by :class:`Player`."""

    def move(self, x: float, y: float) -> None: ...

    def mouse_press(self, button: str) -> None: ...

    def mouse_release(self, button: str) -> None: ...

    def scroll(self, dx: float, dy: float) -> None: ...

    def key_press(self, key: str) -> None: ...

    def key_release(self, key: str) -> None: ...


class _CursorMoveBackend(Protocol):
    def move_and_read(self, target: tuple[int, int]) -> tuple[int, int]: ...


def _validate_target_against_clip(
    target: tuple[int, int], clip: tuple[int, int, int, int]
) -> None:
    """Reject a point Windows would clamp because another app confined it."""

    x, y = target
    left, top, right, bottom = clip
    if left <= x < right and top <= y < bottom:
        return
    raise InputInjectionError(
        f"目标坐标 {target} 不在当前鼠标可活动范围 "
        f"({left}, {top})–({right - 1}, {bottom - 1}) 内。"
        "这通常是全屏程序在切屏后锁定了鼠标，并不表示你修改了分辨率；"
        "为防止点错已停止，请在目标程序最终显示模式下重新取点，"
        "或解除鼠标锁定/改用无边框窗口后重试"
    )


class _WindowsCursorMoveBackend:
    """Typed Win32 cursor calls executed in an explicit PMv2 thread context."""

    def move_and_read(self, target: tuple[int, int]) -> tuple[int, int]:
        try:
            import ctypes
            from ctypes import wintypes

            with windows_per_monitor_dpi_context():
                if not is_coordinate_on_current_screen(*target):
                    raise InputInjectionError(
                        f"目标坐标 {target} 已不属于当前任一显示器。"
                        "显示器布局或全屏显示模式可能在倒计时/切屏后发生变化；"
                        "为防止点错已停止，请重新选取位置后再运行"
                    )

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                clip = get_cursor_clip_rect()
                if clip is None:
                    raise InputInjectionError(
                        "当前平台无法核对鼠标活动范围，为防止点错已停止"
                    )
                _validate_target_against_clip(
                    target,
                    (clip.x, clip.y, clip.right, clip.bottom),
                )

                set_cursor_pos = user32.SetCursorPos
                set_cursor_pos.argtypes = [ctypes.c_int, ctypes.c_int]
                set_cursor_pos.restype = wintypes.BOOL
                if not set_cursor_pos(*target):
                    error_code = ctypes.get_last_error()
                    raise InputInjectionError(
                        f"Windows 拒绝将鼠标移动到坐标 {target}"
                        f"（错误代码 {error_code}）。请确认目标程序没有锁定鼠标，"
                        "并重新取点后再运行"
                    )

                get_cursor_pos = user32.GetCursorPos
                get_cursor_pos.argtypes = [ctypes.POINTER(wintypes.POINT)]
                get_cursor_pos.restype = wintypes.BOOL
                actual = wintypes.POINT()
                if not get_cursor_pos(ctypes.byref(actual)):
                    error_code = ctypes.get_last_error()
                    raise InputInjectionError(
                        "Windows 无法核对移动后的鼠标位置"
                        f"（错误代码 {error_code}），为防止点错已停止"
                    )
                return int(actual.x), int(actual.y)
        except InputInjectionError:
            raise
        except ScreenConfigError as exc:
            raise InputInjectionError(
                f"无法确认目标坐标所在的当前显示器：{exc}；请重新取点后再运行"
            ) from exc
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise InputInjectionError(f"Windows 鼠标坐标注入不可用：{exc}") from exc


class PynputInputController:
    """Default Windows input adapter.

    ``pynput`` and its native controllers are imported only when this class is
    instantiated.  Merely importing the player (including in tests) never
    injects input or requires a desktop session.
    """

    _KEY_ALIASES: Final[dict[str, str]] = {
        "return": "enter",
        "escape": "esc",
        "control": "ctrl",
        "control_l": "ctrl_l",
        "control_r": "ctrl_r",
        "windows": "cmd",
        "win": "cmd",
        "win_l": "cmd_l",
        "win_r": "cmd_r",
        "pageup": "page_up",
        "pagedown": "page_down",
    }

    def __init__(self) -> None:
        try:
            from pynput import keyboard, mouse
        except Exception as exc:  # pragma: no cover - depends on host desktop
            raise InputInjectionError(
                "无法初始化系统输入控制器，请检查 pynput 安装和桌面权限"
            ) from exc
        try:
            self._keyboard_module = keyboard
            self._mouse_module = mouse
            self._keyboard = keyboard.Controller()
            self._mouse = mouse.Controller()
            self._cursor_move_backend: _CursorMoveBackend | None = (
                _WindowsCursorMoveBackend() if sys.platform == "win32" else None
            )
        except Exception as exc:  # pragma: no cover - depends on host desktop
            raise InputInjectionError(
                "无法创建系统输入控制器，请检查当前桌面和权限级别"
            ) from exc

    def move(self, x: float, y: float) -> None:
        target = (int(x), int(y))
        try:
            backend = getattr(self, "_cursor_move_backend", None)
            if backend is not None:
                actual = backend.move_and_read(target)
            else:
                self._mouse.position = target
                actual_raw = self._mouse.position
                actual = (int(actual_raw[0]), int(actual_raw[1]))
        except InputInjectionError:
            raise
        except Exception as exc:  # pragma: no cover - native desktop boundary
            raise InputInjectionError(
                f"无法将鼠标移动到坐标 {target}，请检查显示器连接与权限"
            ) from exc
        if actual != target:
            # pynput's Windows backend does not surface SetCursorPos failure.
            # Verify before any caller proceeds to a button press so a stale
            # secondary-monitor coordinate cannot click at the old position.
            raise InputInjectionError(
                f"鼠标未到达目标坐标 {target}（实际位置 {actual}），"
                "可能是全屏程序锁定鼠标、切屏期间鼠标被移动，或显示器/DPI 状态变化；"
                "为防止点错已停止，请在目标界面稳定后重新取点再运行"
            )

    def mouse_press(self, button: str) -> None:
        self._mouse.press(self._mouse_button(button))

    def mouse_release(self, button: str) -> None:
        self._mouse.release(self._mouse_button(button))

    def scroll(self, dx: float, dy: float) -> None:
        self._mouse.scroll(int(dx), int(dy))

    def key_press(self, key: str) -> None:
        self._keyboard.press(self._keyboard_key(key))

    def key_release(self, key: str) -> None:
        self._keyboard.release(self._keyboard_key(key))

    def _mouse_button(self, value: str) -> Any:
        name = value.strip().lower()
        if name.startswith("button."):
            name = name.split(".", 1)[1]
        try:
            return getattr(self._mouse_module.Button, name)
        except AttributeError as exc:
            raise InputInjectionError(f"无法识别鼠标按钮：{value!r}") from exc

    def _keyboard_key(self, value: str) -> Any:
        if not isinstance(value, str) or not value:
            raise InputInjectionError("按键名称不能为空")
        if len(value) == 1:
            return value
        lowered = value.strip().lower()
        if lowered.startswith("key."):
            lowered = lowered.split(".", 1)[1]
        if lowered.startswith("vk:"):
            try:
                vk = int(lowered[3:])
            except ValueError as exc:
                raise InputInjectionError(f"无法识别按键：{value!r}") from exc
            if not 0 <= vk <= 0xFFFF:
                raise InputInjectionError(f"虚拟键码超出范围：{vk}")
            return self._keyboard_module.KeyCode.from_vk(vk)
        name = self._KEY_ALIASES.get(lowered, lowered)
        try:
            return getattr(self._keyboard_module.Key, name)
        except AttributeError as exc:
            raise InputInjectionError(f"无法识别按键：{value!r}") from exc


PlaybackCallback: TypeAlias = Callable[[PlaybackEvent], None]
ControllerFactory: TypeAlias = Callable[[], InputController]
GuardFactory: TypeAlias = Callable[[], AbstractContextManager[Any]]
RandomSource: TypeAlias = Callable[[float, float], float]


@dataclass(frozen=True, slots=True)
class _PreparedAction:
    source_index: int
    type: str
    delay_ms: float
    delay_random_offsets_ms: tuple[float, ...]
    params: Mapping[str, Any]


class _ExecutionStatus(Enum):
    EXECUTED = 1
    PAUSED = 2
    STOPPED = 3


class Player:
    """Play actions on a worker thread and release all held input safely."""

    def __init__(
        self,
        controller: InputController | None = None,
        *,
        controller_factory: ControllerFactory | None = None,
        state_manager: StateManager | None = None,
        on_event: PlaybackCallback | None = None,
        injection_guard: GuardFactory | AbstractContextManager[Any] | Any | None = None,
        random_source: RandomSource | None = None,
        thread_name: str = "WinActionRecorder-Player",
    ) -> None:
        if controller is not None and controller_factory is not None:
            raise ValueError("controller 和 controller_factory 不能同时提供")
        self._controller = controller
        self._controller_factory = controller_factory or PynputInputController
        self.state_manager = state_manager or StateManager()
        # The default is resolved lazily to keep this module usable on hosts
        # without pynput/native listeners while still suppressing self-injected
        # F8/F9/F10/Esc events in the real application.
        self._injection_guard = (
            _default_injection_guard if injection_guard is None else injection_guard
        )
        if random_source is not None and not callable(random_source):
            raise TypeError("random_source 必须是可调用对象或 None")
        self._random_source = Random().uniform if random_source is None else random_source
        self._thread_name = thread_name

        self._condition = threading.Condition(threading.RLock())
        self._io_lock = threading.RLock()
        self._callbacks: list[PlaybackCallback] = []
        if on_event is not None:
            self._callbacks.append(on_event)

        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._paused = False
        self._pause_cleanup_pending = False
        self._closed = False
        self._phase = AppState.IDLE
        self._resume_state = AppState.PLAYING
        self._last_result: PlaybackResult | None = None
        self._control_error: str | None = None

        # Updated only while _io_lock is held.  The cross-device order lets us
        # release inputs in exact reverse press order.
        self._held_keys: set[str] = set()
        self._held_buttons: set[str] = set()
        self._press_order: list[tuple[str, str]] = []

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        with self._condition:
            return self._paused and not self._stop_requested

    @property
    def last_result(self) -> PlaybackResult | None:
        with self._condition:
            return self._last_result

    def subscribe(self, callback: PlaybackCallback) -> Callable[[], None]:
        """Subscribe to events; return an idempotent unsubscribe function."""

        if not callable(callback):
            raise TypeError("回放事件订阅者必须可调用")
        with self._condition:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

        def unsubscribe() -> None:
            with self._condition:
                try:
                    self._callbacks.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def start(
        self,
        actions: Sequence[Any],
        settings: PlaybackSettings | None = None,
        *,
        daemon: bool = True,
    ) -> threading.Thread:
        """Validate and start playback in a new worker thread."""

        config = settings or PlaybackSettings()
        if not isinstance(config, PlaybackSettings):
            raise TypeError("settings 必须是 PlaybackSettings")
        prepared = _prepare_actions(
            actions, ignore_mouse_moves=config.ignore_mouse_moves
        )
        if not prepared:
            raise PlaybackConfigurationError("没有可回放的已启用动作")

        phase = AppState.COUNTDOWN if config.countdown_seconds > 0 else AppState.PLAYING
        start_gate = threading.Event()
        abort_start = threading.Event()

        with self._condition:
            if self._closed:
                raise RuntimeError("回放器已经关闭")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("回放已经在运行")
            self._stop_requested = False
            self._paused = False
            self._pause_cleanup_pending = False
            self._control_error = None
            self._last_result = None
            self._phase = phase
            self._resume_state = self._phase
            thread = threading.Thread(
                target=self._run_after_start_gate,
                args=(start_gate, abort_start, prepared, config),
                name=self._thread_name,
                daemon=daemon,
            )
            self._thread = thread
            try:
                # The worker waits on start_gate, so it is reliably alive when
                # the state notification reaches GUI subscribers but cannot
                # race ahead and publish progress first.
                thread.start()
            except Exception as exc:
                self._thread = None
                self._phase = AppState.ERROR
                message = f"无法启动回放线程：{exc}"
                self.state_manager.transition(
                    AppState.ERROR, message=message, force=True
                )
                raise PlaybackError(message) from exc

        try:
            self.state_manager.transition(
                phase,
                message="准备回放" if phase is AppState.COUNTDOWN else "正在回放",
                current_loop=0,
                total_loops=config.total_loops,
                current_action=0,
                total_actions=len(prepared),
                progress=0.0,
                expected_states=(
                    AppState.IDLE,
                    AppState.STOPPED,
                    AppState.ERROR,
                ),
            )
        except Exception:
            # A shared StateManager may reject the transition (for example,
            # recording began concurrently).  Let the gated worker exit without
            # touching input and restore the Player to a non-running state.
            abort_start.set()
            start_gate.set()
            thread.join(1.0)
            with self._condition:
                if self._thread is thread:
                    self._thread = None
                self._stop_requested = False
                self._paused = False
                self._pause_cleanup_pending = False
                self._phase = self.state_manager.state
                self._resume_state = AppState.PLAYING
            raise
        start_gate.set()
        return thread

    def play_blocking(
        self,
        actions: Sequence[Any],
        settings: PlaybackSettings | None = None,
    ) -> PlaybackResult:
        """Start playback and wait for its worker to finish."""

        self.start(actions, settings, daemon=False)
        result = self.join()
        if result is None:  # pragma: no cover - no timeout was supplied
            raise RuntimeError("回放线程未能结束")
        return result

    def join(self, timeout: float | None = None) -> PlaybackResult | None:
        """Wait for playback; return ``None`` if a timeout expires."""

        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("等待超时必须是非负有限数值或 None")
        with self._condition:
            thread = self._thread
        if thread is None:
            return self.last_result
        if thread is threading.current_thread():
            raise RuntimeError("回放线程不能等待自身结束")
        thread.join(None if timeout is None else float(timeout))
        if thread.is_alive():
            return None
        return self.last_result

    wait = join

    def pause(self) -> bool:
        """Pause before the next action without blocking the caller on I/O."""

        with self._condition:
            if (
                self._thread is None
                or not self._thread.is_alive()
                or self._last_result is not None
                or self._stop_requested
                or self._paused
            ):
                return False
            self._paused = True
            self._pause_cleanup_pending = True
            self._resume_state = self._phase
            self.state_manager.transition(AppState.PAUSED, message="回放已暂停")
            self._condition.notify_all()
        self._emit("paused", "回放已暂停")
        return True

    def resume(self) -> bool:
        """Resume from the next action that has not yet executed."""

        with self._condition:
            if (
                self._thread is None
                or not self._thread.is_alive()
                or self._stop_requested
                or not self._paused
            ):
                return False
            target = self._resume_state
            self._paused = False
            self._phase = target
            self.state_manager.transition(
                target,
                message=("继续倒计时" if target is AppState.COUNTDOWN else "继续回放"),
            )
            self._condition.notify_all()

        message = "继续倒计时" if target is AppState.COUNTDOWN else "继续回放"
        self._emit("resumed", message)
        return True

    def toggle_pause(self) -> bool:
        """Pause if playing, otherwise resume; return whether state changed."""

        return self.resume() if self.is_paused else self.pause()

    def stop(self, *, wait: bool = False, timeout: float | None = None) -> bool:
        """Request an interruptible stop without synchronously touching I/O."""

        with self._condition:
            running = self._thread is not None and self._thread.is_alive()
            self._stop_requested = True
            self._paused = False
            if running:
                # STOPPED is published only after release_all() finishes in
                # the worker.  Until then the active state acts as the shared
                # StateManager lease and rejects a second input worker.
                self.state_manager.update_progress(message="正在停止回放")
            self._condition.notify_all()
        if running:
            self._emit("stop_requested", "正在停止回放")
        if wait and running:
            self.join(timeout)
        return running

    def emergency_stop(
        self, *, wait: bool = False, timeout: float | None = None
    ) -> bool:
        """Esc hotkey entry point.  It has the same cleanup guarantees as stop."""

        running = self.stop(wait=wait, timeout=timeout)
        if running:
            self._emit("emergency_stop", "已触发紧急停止")
        return running

    # Convenient explicit name for wiring the Esc hotkey.
    stop_from_escape = emergency_stop

    def close(self, timeout: float | None = 5.0) -> PlaybackResult | None:
        """Stop playback for window shutdown and prevent future starts."""

        with self._condition:
            self._closed = True
        self.stop(wait=True, timeout=timeout)
        return self.last_result

    def release_all(self) -> tuple[str, ...]:
        """Release every key/button pressed by this player.

        Release is best-effort and reverse ordered.  Failures are returned as
        sanitized messages while the remaining inputs are still attempted.
        """

        errors: list[str] = []
        with self._io_lock:
            controller = self._controller
            if controller is None:
                self._held_keys.clear()
                self._held_buttons.clear()
                self._press_order.clear()
                return ()

            failed: list[tuple[str, str]] = []
            for kind, value in reversed(tuple(self._press_order)):
                try:
                    self._release_one(controller, kind, value)
                except Exception as exc:  # noqa: BLE001 - continue cleanup
                    label = "按键" if kind == "key" else "鼠标按钮"
                    errors.append(f"释放{label} {value!r} 失败：{exc}")
                    failed.append((kind, value))
                else:
                    if kind == "key":
                        self._held_keys.discard(value)
                    else:
                        self._held_buttons.discard(value)
            # Preserve failed releases so a later stop/close can retry them.
            self._press_order[:] = reversed(failed)
        if errors:
            # Never persist key values or full action details in local logs.
            logger.error("回放输入清理不完整，共 %d 项释放失败", len(errors))
        return tuple(errors)

    def _run_after_start_gate(
        self,
        start_gate: threading.Event,
        abort_start: threading.Event,
        actions: tuple[_PreparedAction, ...],
        settings: PlaybackSettings,
    ) -> None:
        """Keep a started worker dormant until initial state is published."""

        start_gate.wait()
        if abort_start.is_set():
            return
        self._run(actions, settings)

    def _run(
        self, actions: tuple[_PreparedAction, ...], settings: PlaybackSettings
    ) -> None:
        loops_completed = 0
        actions_executed = 0
        outcome = PlaybackOutcome.COMPLETED
        error: str | None = None
        cleanup_errors: tuple[str, ...] = ()
        minimum_click_interval = (
            settings.skip_move_click_interval_ms / 1000.0
            if settings.ignore_mouse_moves
            else 0.0
        )
        # ``None`` means that no completed mouse-button release is waiting for
        # its next press.  A numeric value is the amount of *planned active
        # wait* still required before that press.  Injection time and pauses do
        # not count; action delays (after speed scaling) and loop intervals do.
        pending_click_interval: float | None = None
        # A multi-button chord/drag is one continuous pointer gesture.  Start
        # the inter-click gap only after the final successfully pressed button
        # is released, not after every partial release inside that gesture.
        gesture_buttons_down: set[str] = set()

        self._emit("started", "回放任务已启动", total_actions=len(actions))
        try:
            if settings.countdown_seconds > 0 and not self._run_countdown(
                settings.countdown_seconds, len(actions), settings.total_loops
            ):
                outcome = self._interrupted_outcome()
            else:
                if (
                    self._set_playing_state(
                        "正在回放", len(actions), settings.total_loops
                    )
                    and self._wait_until_active()
                ):
                    loop_index = 0
                    while settings.infinite or loop_index < settings.loop_count:
                        if self._is_stopped():
                            outcome = self._interrupted_outcome()
                            break
                        loop_index += 1
                        self._update_progress(
                            settings,
                            loop_index,
                            0,
                            len(actions),
                            f"开始第 {loop_index} 轮",
                        )
                        self._emit(
                            "loop_started",
                            f"开始第 {loop_index} 轮",
                            loop_index=loop_index,
                            total_loops=settings.total_loops,
                            total_actions=len(actions),
                        )

                        loop_finished = True
                        for action_index, action in enumerate(actions, start=1):
                            self._update_progress(
                                settings,
                                loop_index,
                                action_index,
                                len(actions),
                                f"等待第 {action_index} 个动作",
                                completed=False,
                            )
                            action_wait = scaled_delay_seconds(
                                sample_randomized_delay_ms(
                                    action.delay_ms,
                                    action.delay_random_offsets_ms,
                                    self._random_source,
                                ),
                                settings.speed,
                            )
                            is_mouse_press = (
                                action.type == "mouse_button"
                                and action.params["pressed"] is True
                            )
                            is_mouse_button = action.type == "mouse_button"
                            button_name = (
                                str(action.params["button"]) if is_mouse_button else ""
                            )
                            starts_new_gesture = (
                                is_mouse_press and not gesture_buttons_down
                            )
                            if (
                                starts_new_gesture
                                and pending_click_interval is not None
                            ):
                                # The action's own delay contributes to the
                                # minimum; only add the missing portion.
                                action_wait = max(action_wait, pending_click_interval)
                            if not self._interruptible_wait(action_wait):
                                loop_finished = False
                                outcome = self._interrupted_outcome()
                                break
                            if pending_click_interval is not None:
                                pending_click_interval = max(
                                    0.0,
                                    pending_click_interval - action_wait,
                                )

                            status = self._execute_when_active(action)
                            if status is _ExecutionStatus.STOPPED:
                                loop_finished = False
                                outcome = self._interrupted_outcome()
                                break
                            if minimum_click_interval > 0.0 and is_mouse_button:
                                if is_mouse_press:
                                    if not gesture_buttons_down:
                                        pending_click_interval = None
                                    gesture_buttons_down.add(button_name)
                                else:
                                    was_tracked = button_name in gesture_buttons_down
                                    gesture_buttons_down.discard(button_name)
                                    if was_tracked and not gesture_buttons_down:
                                        # Start measuring only after the final
                                        # release of the gesture has completed.
                                        # The down-to-up hold time is therefore
                                        # never lengthened by this setting.
                                        pending_click_interval = minimum_click_interval
                            actions_executed += 1
                            self._update_progress(
                                settings,
                                loop_index,
                                action_index,
                                len(actions),
                                f"已执行第 {action_index} 个动作",
                            )
                            self._emit(
                                "action_executed",
                                f"已执行第 {action_index} 个动作",
                                loop_index=loop_index,
                                total_loops=settings.total_loops,
                                action_index=action_index,
                                total_actions=len(actions),
                            )

                        if not loop_finished:
                            break
                        loops_completed += 1
                        self._emit(
                            "loop_finished",
                            f"第 {loop_index} 轮已完成",
                            loop_index=loop_index,
                            total_loops=settings.total_loops,
                            action_index=len(actions),
                            total_actions=len(actions),
                        )

                        has_next = settings.infinite or loop_index < settings.loop_count
                        interval_seconds = 0.0
                        if has_next:
                            interval_seconds = (
                                sample_randomized_delay_ms(
                                    settings.loop_interval_ms,
                                    settings.loop_interval_random_offsets_ms,
                                    self._random_source,
                                )
                                / 1000.0
                            )
                        if has_next and interval_seconds > 0:
                            self.state_manager.update_progress(message="等待下一轮回放")
                            self._emit(
                                "loop_interval",
                                "等待下一轮回放",
                                loop_index=loop_index,
                                total_loops=settings.total_loops,
                                total_actions=len(actions),
                            )
                            if not self._interruptible_wait(interval_seconds):
                                outcome = self._interrupted_outcome()
                                break
                            if pending_click_interval is not None:
                                pending_click_interval = max(
                                    0.0,
                                    pending_click_interval - interval_seconds,
                                )
                    else:
                        outcome = PlaybackOutcome.COMPLETED
                else:
                    outcome = self._interrupted_outcome()
        except Exception as exc:  # noqa: BLE001 - worker exception boundary
            outcome = PlaybackOutcome.FAILED
            error = _friendly_exception(exc)
            # The user-facing result is specific, while the local log records
            # only the exception class to avoid leaking typed characters.
            logger.error("回放线程异常（%s）", type(exc).__name__)
        finally:
            cleanup_errors = self.release_all()
            with self._condition:
                control_error = self._control_error
                stopped = self._stop_requested
            if control_error:
                outcome = PlaybackOutcome.FAILED
                error = control_error if error is None else f"{error}；{control_error}"
            if cleanup_errors:
                outcome = PlaybackOutcome.FAILED
                cleanup_message = "；".join(cleanup_errors)
                error = (
                    f"{error}；清理输入失败：{cleanup_message}"
                    if error
                    else f"清理输入失败：{cleanup_message}"
                )
            elif stopped and outcome is PlaybackOutcome.COMPLETED:
                outcome = PlaybackOutcome.STOPPED

            result = PlaybackResult(
                outcome=outcome,
                loops_completed=loops_completed,
                actions_executed=actions_executed,
                error=error,
                cleanup_errors=cleanup_errors,
            )
            with self._condition:
                self._last_result = result
                self._paused = False
                self._pause_cleanup_pending = False
                self._phase = (
                    AppState.ERROR
                    if outcome is PlaybackOutcome.FAILED
                    else AppState.STOPPED
                )
                self._condition.notify_all()

            if outcome is PlaybackOutcome.FAILED:
                message = error or "回放失败"
                self.state_manager.transition(
                    AppState.ERROR, message=message, force=True
                )
                self._emit("error", message, error=message)
            elif outcome is PlaybackOutcome.STOPPED:
                self.state_manager.transition(
                    AppState.STOPPED, message="回放已停止", force=True
                )
            else:
                self.state_manager.transition(
                    AppState.STOPPED,
                    message="回放完成",
                    progress=1.0,
                    force=True,
                )
            self._emit(
                "finished",
                "回放完成"
                if outcome is PlaybackOutcome.COMPLETED
                else (error or "回放已停止"),
                error=error,
            )

    def _run_countdown(
        self,
        seconds: float,
        total_actions: int,
        total_loops: int | None,
    ) -> bool:
        remaining = seconds
        while remaining > 0:
            if not self._wait_until_active():
                return False
            shown = max(1, math.ceil(remaining))
            message = f"{shown} 秒后开始回放"
            with self._condition:
                if self._stop_requested:
                    return False
                if self._paused or self._pause_cleanup_pending:
                    continue
                self._phase = AppState.COUNTDOWN
                self._resume_state = AppState.COUNTDOWN
                self.state_manager.transition(
                    AppState.COUNTDOWN,
                    message=message,
                    current_loop=0,
                    total_loops=total_loops,
                    current_action=0,
                    total_actions=total_actions,
                    progress=0.0,
                )
                # A subscriber may have synchronously requested pause/stop.
                if self._stop_requested:
                    return False
                if self._paused or self._pause_cleanup_pending:
                    continue
            self._emit(
                "countdown",
                message,
                total_loops=total_loops,
                total_actions=total_actions,
                countdown_remaining=shown,
            )
            step = min(1.0, remaining)
            if not self._interruptible_wait(step):
                return False
            remaining -= step
        return True

    def _set_playing_state(
        self, message: str, total_actions: int, total_loops: int | None
    ) -> bool:
        while True:
            with self._condition:
                if self._stop_requested:
                    return False
                self._phase = AppState.PLAYING
                self._resume_state = AppState.PLAYING
                # Countdown may finish concurrently with pause.  Preserve
                # PAUSED and make resume target PLAYING.  If resume arrived
                # before cleanup finished, cleanup must complete before the
                # PLAYING state is finally published.
                if not self._paused and not self._pause_cleanup_pending:
                    self.state_manager.transition(
                        AppState.PLAYING,
                        message=message,
                        current_loop=0,
                        total_loops=total_loops,
                        current_action=0,
                        total_actions=total_actions,
                        progress=0.0,
                    )
                    if self._stop_requested:
                        return False
                    if not self._paused and not self._pause_cleanup_pending:
                        return True
            if not self._wait_until_active():
                return False

    def _interruptible_wait(self, seconds: float) -> bool:
        """Wait for active playback time, excluding time spent paused."""

        if seconds <= 0:
            return self._wait_until_active()
        remaining = seconds
        while remaining > 0:
            if not self._wait_until_active():
                return False
            with self._condition:
                if self._stop_requested:
                    return False
                if self._paused or self._pause_cleanup_pending:
                    continue
                started = monotonic()
                self._condition.wait(timeout=remaining)
                elapsed = max(0.0, monotonic() - started)
                remaining = max(0.0, remaining - elapsed)
        return not self._is_stopped()

    def _wait_until_active(self) -> bool:
        while True:
            with self._condition:
                if self._stop_requested:
                    return False
                cleanup_pending = self._pause_cleanup_pending
                if not cleanup_pending and not self._paused:
                    return True
                if not cleanup_pending:
                    self._condition.wait()
                    continue

            # Only the playback worker reaches this path.  GUI control methods
            # merely set flags, so they never wait for _io_lock or native I/O.
            cleanup_errors = self.release_all()
            with self._condition:
                self._pause_cleanup_pending = False
                self._condition.notify_all()
            if cleanup_errors:
                self._fail_from_control(
                    "暂停时无法释放全部输入：" + "；".join(cleanup_errors)
                )
                return False
            self._emit("pause_cleanup_complete", "暂停输入清理完成")

    def _execute_when_active(self, action: _PreparedAction) -> _ExecutionStatus:
        while True:
            if not self._wait_until_active():
                return _ExecutionStatus.STOPPED
            with self._io_lock:
                with self._condition:
                    if self._stop_requested:
                        return _ExecutionStatus.STOPPED
                    if self._paused or self._pause_cleanup_pending:
                        continue
                controller = self._ensure_controller()
                try:
                    self._inject_action(controller, action)
                except PlaybackError:
                    raise
                except Exception as exc:
                    raise InputInjectionError(
                        f"输入注入失败（第 {action.source_index + 1} 个原始动作）：{exc}"
                    ) from exc
                return _ExecutionStatus.EXECUTED

    def _inject_action(
        self, controller: InputController, action: _PreparedAction
    ) -> None:
        params = action.params
        if action.type == "mouse_move":
            controller.move(params["x"], params["y"])
            return
        if action.type == "mouse_button":
            if "x" in params and "y" in params:
                controller.move(params["x"], params["y"])
            button = str(params["button"]).lower()
            if params["pressed"]:
                # Track pessimistically: a native injection API can report an
                # error after partially applying the press.  A harmless extra
                # release is safer than a stuck button/key.
                self._remember_press("button", button)
                controller.mouse_press(button)
            else:
                controller.mouse_release(button)
                self._forget_press("button", button)
            return
        if action.type == "mouse_scroll":
            if "x" in params and "y" in params:
                controller.move(params["x"], params["y"])
            controller.scroll(params["dx"], params["dy"])
            return
        key = str(params["key"])
        if action.type == "key_down":
            self._remember_press("key", key)
            with self._guard_for_key(key):
                controller.key_press(key)
        elif action.type == "key_up":
            with self._guard_for_key(key):
                controller.key_release(key)
            self._forget_press("key", key)
        else:  # All actions are validated before the thread starts.
            raise PlaybackConfigurationError(f"不支持的动作类型：{action.type}")

    def _remember_press(self, kind: str, value: str) -> None:
        held = self._held_keys if kind == "key" else self._held_buttons
        if value not in held:
            held.add(value)
            self._press_order.append((kind, value))

    def _forget_press(self, kind: str, value: str) -> None:
        held = self._held_keys if kind == "key" else self._held_buttons
        held.discard(value)
        try:
            self._press_order.remove((kind, value))
        except ValueError:
            pass

    def _ensure_controller(self) -> InputController:
        if self._controller is None:
            try:
                self._controller = self._controller_factory()
            except PlaybackError:
                raise
            except Exception as exc:
                raise InputInjectionError(f"无法初始化输入控制器：{exc}") from exc
        return self._controller

    def _guard_context(self) -> AbstractContextManager[Any]:
        guard = self._injection_guard
        if guard is None:
            return nullcontext()
        suppressed = getattr(guard, "suppressed", None)
        context = (
            suppressed()
            if callable(suppressed)
            else (guard() if callable(guard) else guard)
        )
        if not hasattr(context, "__enter__") or not hasattr(context, "__exit__"):
            raise TypeError("injection_guard 必须返回上下文管理器")
        return context

    def _guard_for_key(self, key: str) -> AbstractContextManager[Any]:
        return self._guard_context() if is_control_key(key) else nullcontext()

    def _release_one(self, controller: InputController, kind: str, value: str) -> None:
        def release() -> None:
            if kind == "key":
                controller.key_release(value)
            else:
                controller.mouse_release(value)

        try:
            context = self._guard_for_key(value) if kind == "key" else nullcontext()
            with context:
                release()
        except Exception as first_error:  # noqa: BLE001 - cleanup fallback
            # Cleanup takes priority over hotkey suppression.  This second,
            # unguarded best-effort attempt also covers native APIs that report
            # an error after they have already applied the release.
            try:
                release()
            except Exception as retry_error:
                raise retry_error from first_error

    def _update_progress(
        self,
        settings: PlaybackSettings,
        loop_index: int,
        action_index: int,
        total_actions: int,
        message: str,
        *,
        completed: bool = True,
    ) -> None:
        completed_in_loop = action_index if completed else max(0, action_index - 1)
        if total_actions == 0:
            progress = 0.0
        elif settings.infinite:
            progress = completed_in_loop / total_actions
        else:
            completed_total = (loop_index - 1) * total_actions + completed_in_loop
            progress = completed_total / (settings.loop_count * total_actions)
        self.state_manager.update_progress(
            current_loop=loop_index,
            total_loops=settings.total_loops,
            current_action=action_index,
            total_actions=total_actions,
            progress=max(0.0, min(1.0, progress)),
            message=message,
        )

    def _is_stopped(self) -> bool:
        with self._condition:
            return self._stop_requested

    def _interrupted_outcome(self) -> PlaybackOutcome:
        with self._condition:
            return (
                PlaybackOutcome.FAILED
                if self._control_error
                else PlaybackOutcome.STOPPED
            )

    def _fail_from_control(self, message: str) -> None:
        with self._condition:
            self._control_error = message
            self._stop_requested = True
            self._paused = False
            self._condition.notify_all()
        self.state_manager.transition(AppState.ERROR, message=message, force=True)
        self._emit("error", message, error=message)

    def _emit(
        self,
        kind: str,
        message: str = "",
        *,
        loop_index: int = 0,
        total_loops: int | None | object = ...,
        action_index: int = 0,
        total_actions: int = 0,
        countdown_remaining: int | None = None,
        error: str | None = None,
    ) -> None:
        snapshot = self.state_manager.snapshot()
        loops = snapshot.total_loops if total_loops is ... else total_loops
        event = PlaybackEvent(
            kind=kind,
            state=snapshot.state,
            message=message,
            loop_index=loop_index,
            total_loops=loops,  # type: ignore[arg-type]
            action_index=action_index,
            total_actions=total_actions or snapshot.total_actions,
            countdown_remaining=countdown_remaining,
            error=error,
        )
        with self._condition:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # pragma: no cover - defensive GUI boundary
                logger.exception("回放事件回调执行失败")


def validate_speed(speed: object) -> float:
    """Validate and normalize a speed multiplier in the supported range."""

    value = _finite_number(speed, "播放速度")
    if not MIN_PLAYBACK_SPEED <= value <= MAX_PLAYBACK_SPEED:
        raise PlaybackConfigurationError(
            f"播放速度必须在 {MIN_PLAYBACK_SPEED:g} 到 {MAX_PLAYBACK_SPEED:g} 之间"
        )
    return value


def scaled_delay_seconds(delay_ms: object, speed: object = 1.0) -> float:
    """Convert an action delay in milliseconds into scaled seconds."""

    delay = _finite_number(delay_ms, "动作延迟", minimum=0.0)
    return delay / 1000.0 / validate_speed(speed)


def scaled_delay_ms(delay_ms: object, speed: object = 1.0) -> float:
    """Return an action delay scaled by playback speed, in milliseconds."""

    return scaled_delay_seconds(delay_ms, speed) * 1000.0


def sample_randomized_delay_ms(
    base_ms: object,
    random_offsets_ms: object,
    random_source: RandomSource,
) -> float:
    """Sample independent symmetric offsets around a non-negative base delay."""

    base = _finite_number(base_ms, "基础等待时间", minimum=0.0)
    offsets = _normalize_random_offsets(random_offsets_ms, "随机等待偏移")
    if sum(offsets) > base:
        raise PlaybackConfigurationError(
            "随机等待偏移总量不能大于基础等待，否则可能产生负等待"
        )
    if not callable(random_source):
        raise TypeError("random_source 必须是可调用对象")
    sampled = base
    for offset in offsets:
        value = _finite_number(
            random_source(-offset, offset),
            "随机等待抽样结果",
        )
        if not -offset <= value <= offset:
            raise PlaybackConfigurationError(
                f"随机等待抽样结果必须在 {-offset:g} 到 {offset:g} 毫秒之间"
            )
        sampled += value
    return max(0.0, sampled)


# Widely used name in callers/tests; the return unit is seconds for waiting.
calculate_delay = scaled_delay_seconds


def _prepare_actions(
    actions: Sequence[Any], *, ignore_mouse_moves: bool = False
) -> tuple[_PreparedAction, ...]:
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise PlaybackConfigurationError("动作列表必须是序列")
    if not isinstance(ignore_mouse_moves, bool):
        raise PlaybackConfigurationError("忽略鼠标移动设置必须是布尔值")
    prepared: list[_PreparedAction] = []
    for index, action in enumerate(actions):
        enabled = _action_field(action, "enabled", True)
        if not isinstance(enabled, bool):
            raise PlaybackConfigurationError(
                f"第 {index + 1} 个动作的 enabled 必须是布尔值"
            )
        if not enabled:
            continue
        raw_type = _action_field(action, "type")
        action_type = getattr(raw_type, "value", raw_type)
        if not isinstance(action_type, str):
            raise PlaybackConfigurationError(f"第 {index + 1} 个动作类型无效")
        action_type = action_type.strip().lower()
        if action_type not in {
            "mouse_move",
            "mouse_button",
            "mouse_scroll",
            "key_down",
            "key_up",
        }:
            raise PlaybackConfigurationError(
                f"第 {index + 1} 个动作类型不受支持：{action_type!r}"
            )
        # Ignoring a movement is deliberately equivalent to disabling it for
        # this playback plan: its injection and its recorded delay are both
        # omitted.  The source Action is never mutated, so toggling the option
        # off restores the exact per-action enabled states from the script.
        if ignore_mouse_moves and action_type == "mouse_move":
            continue
        delay = _finite_number(
            _action_field(action, "delay_ms", 0.0),
            f"第 {index + 1} 个动作的延迟",
            minimum=0.0,
        )
        delay_random_offsets = _normalize_random_offsets(
            _action_field(action, "delay_random_offsets_ms", ()),
            f"第 {index + 1} 个动作的随机延迟偏移",
        )
        if sum(delay_random_offsets) > delay:
            raise PlaybackConfigurationError(
                f"第 {index + 1} 个动作的随机延迟偏移总量不能大于动作延迟"
            )
        params = _action_field(action, "params")
        if not isinstance(params, Mapping):
            raise PlaybackConfigurationError(
                f"第 {index + 1} 个动作的 params 必须是对象"
            )
        normalized_params = dict(params)
        _validate_action_params(index, action_type, normalized_params)
        prepared.append(
            _PreparedAction(
                index,
                action_type,
                delay,
                delay_random_offsets,
                normalized_params,
            )
        )
    return tuple(prepared)


def _normalize_random_offsets(value: object, name: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PlaybackConfigurationError(f"{name}必须是非负数值序列")
    return tuple(
        _finite_number(item, f"{name}第 {index + 1} 项", minimum=0.0)
        for index, item in enumerate(value)
    )


def _action_field(action: Any, name: str, default: Any = ...) -> Any:
    if isinstance(action, Mapping):
        if name in action:
            return action[name]
    elif hasattr(action, name):
        return getattr(action, name)
    if default is not ...:
        return default
    raise PlaybackConfigurationError(f"动作缺少字段：{name}")


def _validate_action_params(
    index: int, action_type: str, params: dict[str, Any]
) -> None:
    prefix = f"第 {index + 1} 个动作"
    if action_type == "mouse_move":
        _required_coordinates(params, prefix)
    elif action_type == "mouse_button":
        button = params.get("button")
        if not isinstance(button, str) or button.strip().lower() not in {
            "left",
            "right",
            "middle",
        }:
            raise PlaybackConfigurationError(f"{prefix}的鼠标按钮无效")
        params["button"] = button.strip().lower()
        if not isinstance(params.get("pressed"), bool):
            raise PlaybackConfigurationError(f"{prefix}的 pressed 必须是布尔值")
        _optional_coordinates(params, prefix)
    elif action_type == "mouse_scroll":
        if "dx" not in params or "dy" not in params:
            raise PlaybackConfigurationError(f"{prefix}缺少滚轮 dx 或 dy")
        params["dx"] = _finite_number(params["dx"], f"{prefix}的 dx")
        params["dy"] = _finite_number(params["dy"], f"{prefix}的 dy")
        _optional_coordinates(params, prefix)
    else:
        key = params.get("key", params.get("char", params.get("value")))
        if not isinstance(key, str) or not key:
            raise PlaybackConfigurationError(f"{prefix}的按键名称不能为空")
        params["key"] = key


def _required_coordinates(params: dict[str, Any], prefix: str) -> None:
    if "x" not in params or "y" not in params:
        raise PlaybackConfigurationError(f"{prefix}缺少坐标 x 或 y")
    params["x"] = _finite_number(params["x"], f"{prefix}的 x")
    params["y"] = _finite_number(params["y"], f"{prefix}的 y")


def _optional_coordinates(params: dict[str, Any], prefix: str) -> None:
    has_x = "x" in params
    has_y = "y" in params
    if has_x != has_y:
        raise PlaybackConfigurationError(f"{prefix}的 x 和 y 必须同时提供")
    if has_x:
        _required_coordinates(params, prefix)


def _finite_number(value: object, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise PlaybackConfigurationError(f"{name}必须是有限数值")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PlaybackConfigurationError(f"{name}必须是有限数值") from exc
    if not math.isfinite(result):
        raise PlaybackConfigurationError(f"{name}必须是有限数值")
    if minimum is not None and result < minimum:
        raise PlaybackConfigurationError(f"{name}不能小于 {minimum:g}")
    return result


def _friendly_exception(exc: Exception) -> str:
    if isinstance(exc, PlaybackError):
        return str(exc)
    text = str(exc).strip()
    return f"回放期间发生异常：{text or type(exc).__name__}"


def _default_injection_guard() -> AbstractContextManager[Any]:
    from .hotkeys import suppress_injected_input

    return suppress_injected_input()


# Readable compatibility aliases for callers.
ActionPlayer = Player
PlaybackPlayer = Player
PlaybackConfig = PlaybackSettings


__all__ = [
    "MAX_PLAYBACK_SPEED",
    "MIN_PLAYBACK_SPEED",
    "ActionPlayer",
    "ControllerFactory",
    "InputController",
    "InputInjectionError",
    "PlaybackCallback",
    "PlaybackConfig",
    "PlaybackConfigurationError",
    "PlaybackError",
    "PlaybackEvent",
    "PlaybackOutcome",
    "PlaybackPlayer",
    "PlaybackResult",
    "PlaybackSettings",
    "Player",
    "PynputInputController",
    "RandomSource",
    "calculate_delay",
    "sample_randomized_delay_ms",
    "scaled_delay_ms",
    "scaled_delay_seconds",
    "validate_speed",
]
