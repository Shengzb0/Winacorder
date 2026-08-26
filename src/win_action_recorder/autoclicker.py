"""Threaded mouse auto-clicking with interruptible timing and safe cleanup.

The module deliberately has no Qt dependency.  Native input is created lazily
inside the worker, callbacks run on the worker/control thread, and callers may
inject a fake controller for tests without touching the real desktop.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic
from typing import Final, TypeAlias

from .player import InputController, PynputInputController
from .screen import CoordinateAnchor, VirtualDesktop, get_cursor_clip_rect
from .state import AppState, StateManager

logger = logging.getLogger(__name__)

MIN_CLICK_INTERVAL_MS: Final[float] = 10.0
MAX_CLICK_INTERVAL_MS: Final[float] = 60_000.0
MAX_CLICK_COUNT: Final[int] = 1_000_000
MAX_COUNTDOWN_SECONDS: Final[float] = 60.0
CLICK_HOLD_SECONDS: Final[float] = 0.02
_INT32_MIN: Final[int] = -(2**31)
_INT32_MAX: Final[int] = 2**31 - 1
_MOUSE_BUTTONS: Final[frozenset[str]] = frozenset(
    {"left", "right", "middle"}
)


class AutoClickError(RuntimeError):
    """Base class for user-facing auto-click failures."""


class AutoClickConfigurationError(ValueError, AutoClickError):
    """Raised before a worker starts when its settings are invalid."""


class AutoClickInjectionError(AutoClickError):
    """Raised when the native input controller rejects an operation."""


class ClickLocationMode(str, Enum):
    """How the target point for each click is chosen."""

    FOLLOW_CURSOR = "follow_cursor"
    FIXED = "fixed"


class AutoClickOutcome(str, Enum):
    """Terminal auto-click outcomes."""

    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AutoClickSettings:
    """Validated settings for one auto-click session.

    ``interval_ms`` is active time from one successful mouse-button release to
    the next press.  The first click is immediate after any countdown and the
    final click has no trailing interval.  In follow-cursor mode no pointer
    movement is injected; fixed mode moves to ``(x, y)`` before every press.
    """

    button: str = "left"
    interval_ms: float = 100.0
    click_count: int = 10
    infinite: bool = False
    location_mode: ClickLocationMode = ClickLocationMode.FOLLOW_CURSOR
    x: int | None = None
    y: int | None = None
    countdown_seconds: float = 3.0
    coordinate_anchor: CoordinateAnchor | None = None
    adapt_to_cursor_clip: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.button, str):
            raise AutoClickConfigurationError("鼠标按钮必须是文本名称")
        button = self.button.strip().lower()
        if button not in _MOUSE_BUTTONS:
            raise AutoClickConfigurationError("鼠标按钮只能是左键、右键或中键")

        interval = _finite_number(
            self.interval_ms,
            "连点间隔",
            minimum=MIN_CLICK_INTERVAL_MS,
            maximum=MAX_CLICK_INTERVAL_MS,
        )
        if (
            isinstance(self.click_count, bool)
            or not isinstance(self.click_count, int)
            or not 1 <= self.click_count <= MAX_CLICK_COUNT
        ):
            raise AutoClickConfigurationError(
                f"连点次数必须是 1 到 {MAX_CLICK_COUNT} 之间的整数"
            )
        if not isinstance(self.infinite, bool):
            raise AutoClickConfigurationError("无限连点设置必须是布尔值")

        try:
            location_mode = (
                self.location_mode
                if isinstance(self.location_mode, ClickLocationMode)
                else ClickLocationMode(self.location_mode)
            )
        except (TypeError, ValueError) as exc:
            raise AutoClickConfigurationError("未知的连点位置模式") from exc

        if (self.x is None) != (self.y is None):
            raise AutoClickConfigurationError("固定坐标 x 和 y 必须同时提供")
        x = _optional_int32(self.x, "固定坐标 x")
        y = _optional_int32(self.y, "固定坐标 y")
        if location_mode is ClickLocationMode.FIXED and (x is None or y is None):
            raise AutoClickConfigurationError("固定位置模式必须提供 x 和 y 坐标")
        if self.coordinate_anchor is not None:
            if not isinstance(self.coordinate_anchor, CoordinateAnchor):
                raise AutoClickConfigurationError("固定坐标锚点无效")
            if (self.coordinate_anchor.x, self.coordinate_anchor.y) != (x, y):
                raise AutoClickConfigurationError("固定坐标与取点锚点不一致")
        if not isinstance(self.adapt_to_cursor_clip, bool):
            raise AutoClickConfigurationError("切屏坐标自适应设置必须是布尔值")

        countdown = _finite_number(
            self.countdown_seconds,
            "连点倒计时",
            minimum=0.0,
            maximum=MAX_COUNTDOWN_SECONDS,
        )

        object.__setattr__(self, "button", button)
        object.__setattr__(self, "interval_ms", interval)
        object.__setattr__(self, "location_mode", location_mode)
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "countdown_seconds", countdown)

    @property
    def target_clicks(self) -> int | None:
        """Return ``None`` for an infinite session, otherwise its target."""

        return None if self.infinite else self.click_count


@dataclass(frozen=True, slots=True)
class AutoClickEvent:
    """Thread-safe event emitted at auto-click milestones."""

    kind: str
    state: AppState
    message: str = ""
    clicks_completed: int = 0
    target_clicks: int | None = 10
    countdown_remaining: int | None = None
    error: str | None = None
    timestamp: float = field(default_factory=monotonic)


@dataclass(frozen=True, slots=True)
class AutoClickResult:
    """Final session result."""

    outcome: AutoClickOutcome
    clicks_completed: int
    error: str | None = None
    cleanup_errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome is AutoClickOutcome.COMPLETED


AutoClickCallback: TypeAlias = Callable[[AutoClickEvent], None]
ControllerFactory: TypeAlias = Callable[[], InputController]
ClipRectProvider: TypeAlias = Callable[[], VirtualDesktop | None]


class _ClickStatus(Enum):
    EXECUTED = 1
    PAUSED = 2
    STOPPED = 3


class AutoClicker:
    """Execute complete mouse clicks on a worker and release safely on exit."""

    def __init__(
        self,
        controller: InputController | None = None,
        *,
        controller_factory: ControllerFactory | None = None,
        state_manager: StateManager | None = None,
        on_event: AutoClickCallback | None = None,
        clip_rect_provider: ClipRectProvider | None = None,
        thread_name: str = "WinActionRecorder-AutoClicker",
    ) -> None:
        if controller is not None and controller_factory is not None:
            raise ValueError("controller 和 controller_factory 不能同时提供")
        self._controller = controller
        self._controller_factory = controller_factory or PynputInputController
        self.state_manager = state_manager or StateManager()
        self._clip_rect_provider = clip_rect_provider or get_cursor_clip_rect
        self._thread_name = thread_name

        self._condition = threading.Condition(threading.RLock())
        self._io_lock = threading.RLock()
        self._callbacks: list[AutoClickCallback] = []
        if on_event is not None:
            self._callbacks.append(on_event)

        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self._paused = False
        self._closed = False
        self._phase = AppState.IDLE
        self._resume_state = AppState.CLICKING
        self._last_result: AutoClickResult | None = None
        self._held_button: str | None = None

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        with self._condition:
            return self._paused and not self._stop_requested

    @property
    def last_result(self) -> AutoClickResult | None:
        with self._condition:
            return self._last_result

    def subscribe(self, callback: AutoClickCallback) -> Callable[[], None]:
        """Subscribe to events and return an idempotent unsubscribe function."""

        if not callable(callback):
            raise TypeError("连点事件订阅者必须可调用")
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
        settings: AutoClickSettings | None = None,
        *,
        daemon: bool = True,
    ) -> threading.Thread:
        """Validate and start an auto-click session on a new worker."""

        config = settings or AutoClickSettings()
        if not isinstance(config, AutoClickSettings):
            raise TypeError("settings 必须是 AutoClickSettings")
        phase = (
            AppState.COUNTDOWN
            if config.countdown_seconds > 0
            else AppState.CLICKING
        )
        start_gate = threading.Event()
        abort_start = threading.Event()

        with self._condition:
            if self._closed:
                raise RuntimeError("连点器已经关闭")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("鼠标连点已经在运行")
            if self._held_button is not None:
                raise AutoClickError("上一次连点仍有鼠标按钮未能释放")
            self._stop_requested = False
            self._paused = False
            self._last_result = None
            self._phase = phase
            self._resume_state = phase
            thread = threading.Thread(
                target=self._run_after_start_gate,
                args=(start_gate, abort_start, config),
                name=self._thread_name,
                daemon=daemon,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:
                self._thread = None
                self._phase = AppState.ERROR
                message = f"无法启动连点线程：{exc}"
                self.state_manager.transition(
                    AppState.ERROR, message=message, force=True
                )
                raise AutoClickError(message) from exc

        try:
            self.state_manager.transition(
                phase,
                message="准备开始连点" if phase is AppState.COUNTDOWN else "正在连点",
                current_loop=0,
                total_loops=config.target_clicks,
                current_action=0,
                total_actions=0,
                progress=0.0,
                expected_states=(
                    AppState.IDLE,
                    AppState.STOPPED,
                    AppState.ERROR,
                ),
            )
        except Exception:
            abort_start.set()
            start_gate.set()
            thread.join(1.0)
            with self._condition:
                if self._thread is thread:
                    self._thread = None
                self._stop_requested = False
                self._paused = False
                self._phase = self.state_manager.state
                self._resume_state = AppState.CLICKING
            raise
        start_gate.set()
        return thread

    def play_blocking(
        self, settings: AutoClickSettings | None = None
    ) -> AutoClickResult:
        """Start a non-daemon worker and wait until it finishes."""

        self.start(settings, daemon=False)
        result = self.join()
        if result is None:  # pragma: no cover - no timeout was supplied
            raise RuntimeError("连点线程未能结束")
        return result

    def join(self, timeout: float | None = None) -> AutoClickResult | None:
        """Wait for the worker, returning ``None`` if a timeout expires."""

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
            raise RuntimeError("连点线程不能等待自身结束")
        thread.join(None if timeout is None else float(timeout))
        if thread.is_alive():
            return None
        return self.last_result

    wait = join

    def pause(self) -> bool:
        """Pause before the next complete click or countdown step."""

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
            self._resume_state = self._phase
            self.state_manager.transition(AppState.PAUSED, message="连点已暂停")
            self._condition.notify_all()
        self._emit("paused", "连点已暂停")
        return True

    def resume(self) -> bool:
        """Resume the active countdown or the gap before the next click."""

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
                message=(
                    "继续连点倒计时"
                    if target is AppState.COUNTDOWN
                    else "继续连点"
                ),
            )
            self._condition.notify_all()
        self._emit(
            "resumed",
            "继续连点倒计时" if target is AppState.COUNTDOWN else "继续连点",
        )
        return True

    def toggle_pause(self) -> bool:
        """Pause or resume the current session."""

        return self.resume() if self.is_paused else self.pause()

    def stop(self, *, wait: bool = False, timeout: float | None = None) -> bool:
        """Request an interruptible stop without doing native I/O on the caller."""

        with self._condition:
            running = self._thread is not None and self._thread.is_alive()
            self._stop_requested = True
            self._paused = False
            if running:
                # Keep the active state until release_all() has completed.
                # STOPPED is a reusable start state, so publishing it here
                # would allow a second shared worker to inject concurrently
                # while this worker still owns a pressed mouse button.
                self.state_manager.update_progress(message="正在停止连点")
            self._condition.notify_all()
        if running:
            self._emit("stop_requested", "正在停止连点")
        if wait and running:
            self.join(timeout)
        return running

    def emergency_stop(
        self, *, wait: bool = False, timeout: float | None = None
    ) -> bool:
        """Emergency-stop entry point with the same cleanup guarantees."""

        running = self.stop(wait=wait, timeout=timeout)
        if running:
            self._emit("emergency_stop", "已触发连点紧急停止")
        return running

    stop_from_escape = emergency_stop

    def close(self, timeout: float | None = 5.0) -> AutoClickResult | None:
        """Stop the worker and prevent future sessions."""

        with self._condition:
            self._closed = True
        self.stop(wait=True, timeout=timeout)
        return self.last_result

    def release_all(self) -> tuple[str, ...]:
        """Best-effort release of the button currently held by this worker."""

        with self._io_lock:
            button = self._held_button
            controller = self._controller
            if button is None:
                return ()
            if controller is None:
                self._held_button = None
                return ()
            try:
                controller.mouse_release(button)
            except Exception as first_error:  # noqa: BLE001 - native boundary
                try:
                    controller.mouse_release(button)
                except Exception as retry_error:  # noqa: BLE001 - native boundary
                    logger.error("连点输入清理失败，鼠标按钮仍可能处于按下状态")
                    return (
                        f"释放鼠标按钮 {button!r} 失败：{retry_error}",
                    )
                else:
                    logger.warning(
                        "首次释放鼠标按钮失败，重试后已完成清理（%s）",
                        type(first_error).__name__,
                    )
            self._held_button = None
            return ()

    def _run_after_start_gate(
        self,
        start_gate: threading.Event,
        abort_start: threading.Event,
        settings: AutoClickSettings,
    ) -> None:
        start_gate.wait()
        if abort_start.is_set():
            return
        self._run(settings)

    def _run(self, settings: AutoClickSettings) -> None:
        clicks_completed = 0
        outcome = AutoClickOutcome.COMPLETED
        error: str | None = None
        cleanup_errors: tuple[str, ...] = ()

        self._emit(
            "started",
            "连点任务已启动",
            target_clicks=settings.target_clicks,
        )
        try:
            if settings.countdown_seconds > 0 and not self._run_countdown(
                settings.countdown_seconds, settings.target_clicks
            ):
                outcome = AutoClickOutcome.STOPPED
            elif self._set_clicking_state(settings.target_clicks):
                while settings.infinite or clicks_completed < settings.click_count:
                    if not self._wait_until_active():
                        outcome = AutoClickOutcome.STOPPED
                        break

                    status = self._execute_click(settings)
                    if status is _ClickStatus.PAUSED:
                        continue
                    if status is _ClickStatus.STOPPED:
                        outcome = AutoClickOutcome.STOPPED
                        break

                    clicks_completed += 1
                    self._update_progress(clicks_completed, settings.target_clicks)
                    self._emit(
                        "click_completed",
                        f"已完成 {clicks_completed} 次连点",
                        clicks_completed=clicks_completed,
                        target_clicks=settings.target_clicks,
                    )

                    if self._is_stopped():
                        outcome = AutoClickOutcome.STOPPED
                        break
                    if not settings.infinite and clicks_completed >= settings.click_count:
                        break
                    if not self._interruptible_wait(settings.interval_ms / 1000.0):
                        outcome = AutoClickOutcome.STOPPED
                        break
            else:
                outcome = AutoClickOutcome.STOPPED
        except Exception as exc:  # noqa: BLE001 - worker exception boundary
            outcome = AutoClickOutcome.FAILED
            error = _friendly_exception(exc)
            logger.error("连点线程异常（%s）", type(exc).__name__)
        finally:
            cleanup_errors = self.release_all()
            with self._condition:
                stopped = self._stop_requested
            if cleanup_errors:
                outcome = AutoClickOutcome.FAILED
                cleanup_message = "；".join(cleanup_errors)
                error = (
                    f"{error}；清理输入失败：{cleanup_message}"
                    if error
                    else f"清理输入失败：{cleanup_message}"
                )
            elif stopped and outcome is AutoClickOutcome.COMPLETED:
                outcome = AutoClickOutcome.STOPPED

            result = AutoClickResult(
                outcome=outcome,
                clicks_completed=clicks_completed,
                error=error,
                cleanup_errors=cleanup_errors,
            )
            with self._condition:
                self._last_result = result
                self._paused = False
                self._phase = (
                    AppState.ERROR
                    if outcome is AutoClickOutcome.FAILED
                    else AppState.STOPPED
                )
                self._condition.notify_all()

            if outcome is AutoClickOutcome.FAILED:
                message = error or "连点失败"
                self.state_manager.transition(
                    AppState.ERROR, message=message, force=True
                )
                self._emit(
                    "error",
                    message,
                    clicks_completed=clicks_completed,
                    target_clicks=settings.target_clicks,
                    error=message,
                )
            elif outcome is AutoClickOutcome.STOPPED:
                self.state_manager.transition(
                    AppState.STOPPED,
                    message="连点已停止",
                    current_loop=clicks_completed,
                    total_loops=settings.target_clicks,
                    progress=(
                        0.0
                        if settings.target_clicks is None
                        else clicks_completed / settings.click_count
                    ),
                    force=True,
                )
            else:
                self.state_manager.transition(
                    AppState.STOPPED,
                    message="连点完成",
                    current_loop=clicks_completed,
                    total_loops=settings.target_clicks,
                    progress=1.0,
                    force=True,
                )
            self._emit(
                "finished",
                "连点完成"
                if outcome is AutoClickOutcome.COMPLETED
                else (error or "连点已停止"),
                clicks_completed=clicks_completed,
                target_clicks=settings.target_clicks,
                error=error,
            )

    def _run_countdown(
        self, seconds: float, target_clicks: int | None
    ) -> bool:
        remaining = seconds
        while remaining > 0:
            if not self._wait_until_active():
                return False
            shown = max(1, math.ceil(remaining))
            message = f"连点将在 {shown} 秒后开始；Esc 可取消"
            with self._condition:
                if self._stop_requested:
                    return False
                self._phase = AppState.COUNTDOWN
                self._resume_state = AppState.COUNTDOWN
            self.state_manager.update_progress(message=message)
            self._emit(
                "countdown",
                message,
                target_clicks=target_clicks,
                countdown_remaining=shown,
            )
            step = min(1.0, remaining)
            if not self._interruptible_wait(step):
                return False
            remaining -= step
        return True

    def _set_clicking_state(self, target_clicks: int | None) -> bool:
        while True:
            with self._condition:
                if self._stop_requested:
                    return False
                self._phase = AppState.CLICKING
                self._resume_state = AppState.CLICKING
                if not self._paused:
                    self.state_manager.transition(
                        AppState.CLICKING,
                        message="正在连点",
                        current_loop=0,
                        total_loops=target_clicks,
                        current_action=0,
                        total_actions=0,
                        progress=0.0,
                    )
                    if self._stop_requested:
                        return False
                    if not self._paused:
                        return True
            if not self._wait_until_active():
                return False

    def _execute_click(self, settings: AutoClickSettings) -> _ClickStatus:
        with self._io_lock:
            with self._condition:
                if self._stop_requested:
                    return _ClickStatus.STOPPED
                if self._paused:
                    return _ClickStatus.PAUSED
            controller = self._ensure_controller()
            try:
                if settings.location_mode is ClickLocationMode.FIXED:
                    target_x, target_y = self._resolve_fixed_target(settings)
                    controller.move(target_x, target_y)

                # Controller creation and a fixed-position move are native
                # boundaries and may block.  Honor a stop/pause that arrived
                # while either was in progress before beginning a new press.
                with self._condition:
                    if self._stop_requested:
                        return _ClickStatus.STOPPED
                    if self._paused:
                        return _ClickStatus.PAUSED

                # Track before the native call: an API may raise after it has
                # partially applied the press, and an extra release is safer
                # than leaving the physical button stuck.
                self._held_button = settings.button
                controller.mouse_press(settings.button)
                hold_completed = self._wait_button_hold()
                controller.mouse_release(settings.button)
                self._held_button = None
            except AutoClickError:
                raise
            except Exception as exc:
                raise AutoClickInjectionError(f"鼠标连点注入失败：{exc}") from exc

            if not hold_completed or self._is_stopped():
                return _ClickStatus.STOPPED
            return _ClickStatus.EXECUTED

    def _resolve_fixed_target(self, settings: AutoClickSettings) -> tuple[int, int]:
        """Resolve an explicitly anchored point against a fullscreen cursor clip.

        Windows fullscreen applications may narrow ``ClipCursor`` after the
        countdown, even when the user never changed the display resolution.
        For a point selected through the full-screen picker, opt-in adaptation
        preserves its relative position inside that new active rectangle.
        Ordinary recorded playback remains strictly absolute.
        """

        assert settings.x is not None and settings.y is not None
        target = (settings.x, settings.y)
        anchor = settings.coordinate_anchor
        if not settings.adapt_to_cursor_clip or anchor is None:
            return target
        clip = self._clip_rect_provider()
        if clip is None:
            return target

        source = anchor.source_rect
        clip_contains_source = (
            clip.x <= source.x
            and clip.y <= source.y
            and source.right <= clip.right
            and source.bottom <= clip.bottom
        )
        if clip_contains_source:
            return target
        mapped = anchor.map_to(clip)
        logger.info(
            "固定坐标已按当前鼠标活动范围自适应：%s -> %s",
            target,
            mapped,
        )
        return mapped

    def _wait_button_hold(self) -> bool:
        """Hold for about 20 ms; pause waits until the click is complete."""

        remaining = CLICK_HOLD_SECONDS
        while remaining > 0:
            with self._condition:
                if self._stop_requested:
                    return False
                started = monotonic()
                self._condition.wait(timeout=remaining)
                elapsed = max(0.0, monotonic() - started)
                remaining = max(0.0, remaining - elapsed)
        return not self._is_stopped()

    def _interruptible_wait(self, seconds: float) -> bool:
        """Wait for active session time, excluding time spent paused."""

        if seconds <= 0:
            return self._wait_until_active()
        remaining = seconds
        while remaining > 0:
            if not self._wait_until_active():
                return False
            with self._condition:
                if self._stop_requested:
                    return False
                if self._paused:
                    continue
                started = monotonic()
                self._condition.wait(timeout=remaining)
                elapsed = max(0.0, monotonic() - started)
                remaining = max(0.0, remaining - elapsed)
        return not self._is_stopped()

    def _wait_until_active(self) -> bool:
        with self._condition:
            while self._paused and not self._stop_requested:
                self._condition.wait()
            return not self._stop_requested

    def _ensure_controller(self) -> InputController:
        if self._controller is None:
            try:
                self._controller = self._controller_factory()
            except AutoClickError:
                raise
            except Exception as exc:
                raise AutoClickInjectionError(
                    f"无法初始化鼠标输入控制器：{exc}"
                ) from exc
        return self._controller

    def _update_progress(
        self, clicks_completed: int, target_clicks: int | None
    ) -> None:
        progress = (
            0.0
            if target_clicks is None
            else clicks_completed / target_clicks
        )
        self.state_manager.update_progress(
            current_loop=clicks_completed,
            total_loops=target_clicks,
            current_action=0,
            total_actions=0,
            progress=max(0.0, min(1.0, progress)),
            message=f"已完成 {clicks_completed} 次连点",
        )

    def _is_stopped(self) -> bool:
        with self._condition:
            return self._stop_requested

    def _emit(
        self,
        kind: str,
        message: str = "",
        *,
        clicks_completed: int | object = ...,
        target_clicks: int | None | object = ...,
        countdown_remaining: int | None = None,
        error: str | None = None,
    ) -> None:
        snapshot = self.state_manager.snapshot()
        target = snapshot.total_loops if target_clicks is ... else target_clicks
        completed = (
            snapshot.current_loop
            if clicks_completed is ...
            else clicks_completed
        )
        event = AutoClickEvent(
            kind=kind,
            state=snapshot.state,
            message=message,
            clicks_completed=completed,  # type: ignore[arg-type]
            target_clicks=target,  # type: ignore[arg-type]
            countdown_remaining=countdown_remaining,
            error=error,
        )
        with self._condition:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:  # pragma: no cover - defensive UI boundary
                logger.exception("连点事件回调执行失败")


def _finite_number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise AutoClickConfigurationError(f"{name}必须是有限数值")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise AutoClickConfigurationError(f"{name}必须是有限数值") from exc
    if not math.isfinite(result):
        raise AutoClickConfigurationError(f"{name}必须是有限数值")
    if not minimum <= result <= maximum:
        raise AutoClickConfigurationError(
            f"{name}必须在 {minimum:g} 到 {maximum:g} 之间"
        )
    return result


def _optional_int32(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AutoClickConfigurationError(f"{name}必须是 32 位整数")
    if not _INT32_MIN <= value <= _INT32_MAX:
        raise AutoClickConfigurationError(f"{name}超出 32 位整数范围")
    return value


def _friendly_exception(exc: Exception) -> str:
    if isinstance(exc, AutoClickError):
        return str(exc)
    text = str(exc).strip()
    return f"连点期间发生异常：{text or type(exc).__name__}"


__all__ = [
    "CLICK_HOLD_SECONDS",
    "MAX_CLICK_COUNT",
    "MAX_CLICK_INTERVAL_MS",
    "MAX_COUNTDOWN_SECONDS",
    "MIN_CLICK_INTERVAL_MS",
    "AutoClickCallback",
    "AutoClickConfigurationError",
    "AutoClickError",
    "AutoClickEvent",
    "AutoClickInjectionError",
    "AutoClickOutcome",
    "AutoClickResult",
    "AutoClickSettings",
    "AutoClicker",
    "ClickLocationMode",
    "ClipRectProvider",
    "ControllerFactory",
]
