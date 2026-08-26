"""Non-blocking mouse and keyboard action recorder.

``pynput`` owns the listener threads.  This module never imports Qt and never
touches GUI widgets; callbacks therefore need to be bridged to the GUI thread
by the application layer.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Self
from uuid import uuid4

from .hotkeys import _ListenerShutdownCoordinator, is_control_key, normalize_key
from .models import Action, ActionType


class RecorderError(RuntimeError):
    """A recorder failure suitable for presentation to a Chinese user."""


@dataclass(frozen=True, slots=True)
class RecorderSettings:
    """Mouse sampling and listener shutdown settings."""

    move_sample_interval_ms: int = 20
    move_distance_threshold: float = 2.0
    listener_join_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        interval = self.move_sample_interval_ms
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or interval <= 0
        ):
            raise ValueError("鼠标移动采样间隔必须是正整数毫秒")
        distance = self.move_distance_threshold
        if isinstance(distance, bool):
            raise ValueError("鼠标移动距离阈值必须是非负数")  # noqa: TRY004
        try:
            distance_value = float(distance)
        except (TypeError, ValueError) as exc:
            raise ValueError("鼠标移动距离阈值必须是非负数") from exc
        if not math.isfinite(distance_value) or distance_value < 0:
            raise ValueError("鼠标移动距离阈值必须是非负数")
        object.__setattr__(self, "move_distance_threshold", distance_value)
        timeout = self.listener_join_timeout_s
        if isinstance(timeout, bool):
            raise ValueError("监听线程退出等待时间必须是非负数")  # noqa: TRY004
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("监听线程退出等待时间必须是非负数") from exc
        if not math.isfinite(timeout_value) or timeout_value < 0:
            raise ValueError("监听线程退出等待时间必须是非负数")
        object.__setattr__(self, "listener_join_timeout_s", timeout_value)


ListenerFactory = Callable[..., Any]
ActionCallback = Callable[[Action], None]
ErrorCallback = Callable[[RecorderError], None]


def normalize_button(button: object) -> str:
    """Return pynput's stable lower-case mouse button name."""

    if isinstance(button, str):
        text = button
    else:
        name = getattr(button, "name", None)
        if isinstance(name, str) and name:
            return name.lower()
        text = str(button)
    lowered = text.lower()
    if lowered.startswith("button."):
        return lowered[7:]
    return lowered


class InputRecorder:
    """Capture input actions in original order on pynput listener threads.

    ``start`` returns immediately after starting both listeners.  The class is
    intentionally state-agnostic about playback: the application controller is
    responsible for enforcing the recording/playback mutex before calling it.
    """

    def __init__(
        self,
        on_action: ActionCallback | None = None,
        *,
        on_error: ErrorCallback | None = None,
        move_sample_interval_ms: int = 20,
        move_distance_threshold: float = 2.0,
        listener_join_timeout_s: float = 1.0,
        mouse_listener_factory: ListenerFactory | None = None,
        keyboard_listener_factory: ListenerFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], object] = uuid4,
    ) -> None:
        self._settings = RecorderSettings(
            move_sample_interval_ms=move_sample_interval_ms,
            move_distance_threshold=move_distance_threshold,
            listener_join_timeout_s=listener_join_timeout_s,
        )
        self._on_action = on_action
        self._on_error = on_error
        self._mouse_listener_factory = (
            mouse_listener_factory or self._default_mouse_listener_factory
        )
        self._keyboard_listener_factory = (
            keyboard_listener_factory or self._default_keyboard_listener_factory
        )
        self._clock = clock
        self._id_factory = id_factory

        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._recording = False
        self._generation = 0
        self._start_time = 0.0
        self._last_offset_ms = 0
        self._last_move_time: float | None = None
        self._last_move_position: tuple[int, int] | None = None
        self._actions: list[Action] = []
        self._action_event_order: dict[str, tuple[float, int]] = {}
        self._event_sequence = 0
        self._mouse_listener: Any | None = None
        self._keyboard_listener: Any | None = None
        self._monitor_threads: list[threading.Thread] = []
        self._last_error: RecorderError | None = None
        self._shutdown = _ListenerShutdownCoordinator(
            timeout_s=self._settings.listener_join_timeout_s,
            component_name="recorder",
            on_error=self._report_shutdown_error,
        )

    @staticmethod
    def _default_mouse_listener_factory(**callbacks: Callable[..., None]) -> Any:
        from pynput import mouse

        return mouse.Listener(**callbacks)

    @staticmethod
    def _default_keyboard_listener_factory(
        **callbacks: Callable[..., None],
    ) -> Any:
        from pynput import keyboard

        return keyboard.Listener(**callbacks)

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def recording(self) -> bool:
        """Compatibility alias for :attr:`is_recording`."""

        return self.is_recording

    @property
    def actions(self) -> list[Action]:
        """Return a snapshot so callers cannot mutate recorder state."""

        with self._lock:
            return list(self._actions)

    @property
    def settings(self) -> RecorderSettings:
        with self._lock:
            return self._settings

    @property
    def last_error(self) -> RecorderError | None:
        with self._lock:
            return self._last_error

    def configure_mouse_sampling(
        self,
        *,
        interval_ms: int | None = None,
        distance_threshold: float | None = None,
    ) -> None:
        """Update sampling thresholds, including safely during recording."""

        with self._lock:
            settings = self._settings
            self._settings = replace(
                settings,
                move_sample_interval_ms=(
                    settings.move_sample_interval_ms
                    if interval_ms is None
                    else interval_ms
                ),
                move_distance_threshold=(
                    settings.move_distance_threshold
                    if distance_threshold is None
                    else distance_threshold
                ),
            )

    def start(self) -> bool:
        """Start mouse and keyboard listeners without blocking the caller."""

        with self._lifecycle_lock:
            with self._lock:
                if self._recording:
                    return False
                if self._shutdown.pending:
                    raise RecorderError(
                        "上一轮全局输入监听仍在退出；"
                        "为避免重复系统 Hook，请稍后重试录制"
                    )
                self._generation += 1
                generation = self._generation
                self._actions.clear()
                self._last_error = None
                self._start_time = self._clock()
                self._last_offset_ms = 0
                self._last_move_time = None
                self._last_move_position = None
                self._action_event_order.clear()
                self._event_sequence = 0
                self._recording = True

            mouse_listener: Any | None = None
            keyboard_listener: Any | None = None

            # Capture the native callback entry time before generation checks,
            # conversion, normalization, or lock acquisition.  Mouse and
            # keyboard hooks run on different threads, so this timestamp is the
            # only stable basis for final cross-device ordering.
            def on_move(x: int, y: int) -> None:
                event_time = self._clock()
                self._on_move(generation, event_time, x, y)

            def on_click(
                x: int, y: int, button: object, pressed: bool
            ) -> None:
                event_time = self._clock()
                self._on_click(generation, event_time, x, y, button, pressed)

            def on_scroll(x: int, y: int, dx: int, dy: int) -> None:
                event_time = self._clock()
                self._on_scroll(generation, event_time, x, y, dx, dy)

            def on_key_press(key: object) -> None:
                event_time = self._clock()
                self._on_key_press(generation, event_time, key)

            def on_key_release(key: object) -> None:
                event_time = self._clock()
                self._on_key_release(generation, event_time, key)

            try:
                mouse_listener = self._mouse_listener_factory(
                    on_move=on_move,
                    on_click=on_click,
                    on_scroll=on_scroll,
                )
                keyboard_listener = self._keyboard_listener_factory(
                    on_press=on_key_press,
                    on_release=on_key_release,
                )
                for listener in (mouse_listener, keyboard_listener):
                    if not callable(getattr(listener, "start", None)):
                        raise TypeError("监听器缺少 start 方法")

                with self._lock:
                    self._mouse_listener = mouse_listener
                    self._keyboard_listener = keyboard_listener

                mouse_listener.start()
                keyboard_listener.start()
            except Exception as exc:
                with self._lock:
                    self._recording = False
                    self._mouse_listener = None
                    self._keyboard_listener = None
                    self._actions.clear()
                    error = RecorderError(f"全局输入监听启动失败：{exc}")
                    self._last_error = error
                self._shutdown.request(
                    [("鼠标", mouse_listener), ("键盘", keyboard_listener)]
                )
                raise error from exc

            self._start_monitor(mouse_listener, generation, "鼠标")
            self._start_monitor(keyboard_listener, generation, "键盘")
            return True

    def stop(self) -> list[Action]:
        """Stop listeners safely and return the recorded-action snapshot."""

        with self._lifecycle_lock:
            with self._lock:
                self._recording = False
                self._generation += 1
                mouse_listener = self._mouse_listener
                keyboard_listener = self._keyboard_listener
                self._mouse_listener = None
                self._keyboard_listener = None
                self._monitor_threads.clear()
                self._finalize_action_order_locked()

            self._shutdown.request(
                [("鼠标", mouse_listener), ("键盘", keyboard_listener)]
            )
            return self.actions

    close = stop

    @property
    def shutdown_pending(self) -> bool:
        return self._shutdown.pending

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        return self._shutdown.wait(timeout)

    def clear(self) -> None:
        """Clear captured actions while idle."""

        with self._lock:
            if self._recording:
                raise RecorderError("录制进行中，不能清空动作")
            self._actions.clear()
            self._action_event_order.clear()
            self._event_sequence = 0
            self._last_offset_ms = 0

    def raise_if_failed(self) -> None:
        """Propagate a listener-thread failure on the caller's thread."""

        error = self.last_error
        if error is not None:
            raise error

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _on_move(
        self, generation: int, event_time: float, x: int, y: int
    ) -> None:
        try:
            x_value, y_value = int(x), int(y)
            with self._lock:
                if not self._is_active_generation_locked(generation):
                    return
                if (
                    self._last_move_time is not None
                    and self._last_move_position is not None
                ):
                    elapsed_ms = (event_time - self._last_move_time) * 1000.0
                    distance = math.hypot(
                        x_value - self._last_move_position[0],
                        y_value - self._last_move_position[1],
                    )
                    if (
                        distance < self._settings.move_distance_threshold
                        and elapsed_ms < self._settings.move_sample_interval_ms
                    ):
                        return
                self._last_move_time = event_time
                self._last_move_position = (x_value, y_value)
                self._record_action_locked(
                    ActionType.MOUSE_MOVE,
                    {"x": x_value, "y": y_value},
                    event_time,
                )
        except Exception as exc:  # noqa: BLE001 - protect listener thread
            self._report_event_error(generation, "记录鼠标移动失败", exc)

    def _on_click(
        self,
        generation: int,
        event_time: float,
        x: int,
        y: int,
        button: object,
        pressed: bool,
    ) -> None:
        if not self._is_active_generation(generation):
            return
        try:
            button_name = normalize_button(button)
            if button_name not in {"left", "right", "middle"}:
                return
            self._record_action(
                generation,
                ActionType.MOUSE_BUTTON,
                {
                    "x": int(x),
                    "y": int(y),
                    "button": button_name,
                    "pressed": bool(pressed),
                },
                event_time,
            )
        except Exception as exc:  # noqa: BLE001 - protect listener thread
            self._report_event_error(generation, "记录鼠标按键失败", exc)

    def _on_scroll(
        self,
        generation: int,
        event_time: float,
        x: int,
        y: int,
        dx: int,
        dy: int,
    ) -> None:
        if not self._is_active_generation(generation):
            return
        try:
            self._record_action(
                generation,
                ActionType.MOUSE_SCROLL,
                {"x": int(x), "y": int(y), "dx": int(dx), "dy": int(dy)},
                event_time,
            )
        except Exception as exc:  # noqa: BLE001 - protect listener thread
            self._report_event_error(generation, "记录鼠标滚轮失败", exc)

    def _on_key_press(
        self, generation: int, event_time: float, key: object
    ) -> None:
        self._record_key(
            generation,
            event_time,
            key,
            ActionType.KEY_DOWN,
            "记录键盘按下失败",
        )

    def _on_key_release(
        self, generation: int, event_time: float, key: object
    ) -> None:
        self._record_key(
            generation,
            event_time,
            key,
            ActionType.KEY_UP,
            "记录键盘释放失败",
        )

    def _record_key(
        self,
        generation: int,
        event_time: float,
        key: object,
        action_type: ActionType,
        error_prefix: str,
    ) -> None:
        if not self._is_active_generation(generation):
            return
        try:
            if is_control_key(key):
                return
            key_name = normalize_key(key)
            if not key_name:
                raise ValueError("无法识别按键")
            self._record_action(
                generation, action_type, {"key": key_name}, event_time
            )
        except Exception:  # noqa: BLE001 - protect listener thread
            # Do not place the actual key or a model exception containing it in
            # an error that the application may write to its local log.
            self._report_event_error(generation, error_prefix)

    def _record_action(
        self,
        generation: int,
        action_type: ActionType,
        params: dict[str, Any],
        event_time: float,
    ) -> None:
        with self._lock:
            if not self._is_active_generation_locked(generation):
                return
            self._record_action_locked(action_type, params, event_time)

    def _is_active_generation(self, generation: int) -> bool:
        with self._lock:
            return self._is_active_generation_locked(generation)

    def _is_active_generation_locked(self, generation: int) -> bool:
        return self._recording and generation == self._generation

    def _record_action_locked(
        self,
        action_type: ActionType,
        params: dict[str, Any],
        event_time: float,
    ) -> None:
        """Append and emit one action; caller must hold ``self._lock``."""

        raw_offset = max(0, round((event_time - self._start_time) * 1000))
        offset_ms = max(self._last_offset_ms, raw_offset)
        delay_ms = offset_ms - self._last_offset_ms
        action = Action(
            id=str(self._id_factory()),
            type=action_type,
            offset_ms=offset_ms,
            delay_ms=delay_ms,
            enabled=True,
            params=params,
        )
        self._actions.append(action)
        self._event_sequence += 1
        self._action_event_order[action.id] = (event_time, self._event_sequence)
        self._last_offset_ms = offset_ms
        if self._on_action is not None:
            try:
                # Emitted while serialized by the re-entrant lock so mouse and
                # keyboard listener callbacks cannot reach the UI out of order.
                self._on_action(action)
            except Exception:  # noqa: BLE001 - isolate external callback
                # A callback may include the Action in its exception text.
                error = RecorderError("传递录制动作时发生错误")
                self._last_error = error
                self._notify_error(error)

    def _start_monitor(self, listener: Any, generation: int, label: str) -> None:
        if not callable(getattr(listener, "join", None)):
            return
        monitor = threading.Thread(
            target=self._monitor_listener,
            args=(listener, generation, label),
            name=f"WinActionRecorder-{label}-listener-monitor",
            daemon=True,
        )
        with self._lock:
            if not self._recording or generation != self._generation:
                return
            self._monitor_threads.append(monitor)
        monitor.start()

    def _monitor_listener(self, listener: Any, generation: int, label: str) -> None:
        try:
            listener.join()
        except Exception as exc:  # noqa: BLE001 - pynput propagates via join
            self._handle_listener_failure(listener, generation, label, exc)
        else:
            self._handle_listener_failure(
                listener,
                generation,
                label,
                RuntimeError("监听线程意外退出"),
            )

    def _handle_listener_failure(
        self, listener: Any, generation: int, label: str, exc: Exception
    ) -> None:
        with self._lock:
            if not self._recording or generation != self._generation:
                return
            self._recording = False
            self._finalize_action_order_locked()
            listeners = (self._mouse_listener, self._keyboard_listener)
            self._mouse_listener = None
            self._keyboard_listener = None
            error = RecorderError(f"{label}全局监听运行失败：{exc}")
            self._last_error = error
        self._shutdown.request(
            [("鼠标", listeners[0]), ("键盘", listeners[1])],
            before_workers=lambda: self._notify_error(error),
        )

    def _finalize_action_order_locked(self) -> None:
        """Sort cross-device callbacks by entry time and rebuild exact delays."""

        self._actions.sort(
            key=lambda action: self._action_event_order.get(
                action.id, (float("inf"), 2**63 - 1)
            )
        )
        previous_offset = 0
        for action in self._actions:
            event_time, _sequence = self._action_event_order.get(
                action.id, (self._start_time, 0)
            )
            raw_offset = max(0, round((event_time - self._start_time) * 1000))
            action.offset_ms = max(previous_offset, raw_offset)
            action.delay_ms = action.offset_ms - previous_offset
            previous_offset = action.offset_ms
        self._last_offset_ms = previous_offset

    def _report_event_error(
        self,
        generation: int,
        prefix: str,
        exc: Exception | None = None,
    ) -> None:
        with self._lock:
            if not self._is_active_generation_locked(generation):
                return
            error = RecorderError(prefix if exc is None else f"{prefix}：{exc}")
            self._last_error = error
        self._notify_error(error)

    def _notify_error(self, error: RecorderError) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(error)
        except Exception:  # noqa: BLE001 - error callback must be isolated
            # Error handling must not terminate a pynput listener thread.
            return

    def _report_shutdown_error(self, message: str) -> None:
        error = RecorderError(message)
        with self._lock:
            # Preserve the first/root listener failure.  Shutdown may report a
            # secondary join error milliseconds later; overwriting here made
            # ``raise_if_failed`` depend on thread scheduling and hid the
            # failure that actually stopped recording.
            if self._last_error is None:
                self._last_error = error
        self._notify_error(error)


# Concise aliases for application code and older integrations.
ActionRecorder = InputRecorder
Recorder = InputRecorder
RecorderConfig = RecorderSettings


__all__ = [
    "ActionRecorder",
    "InputRecorder",
    "Recorder",
    "RecorderConfig",
    "RecorderError",
    "RecorderSettings",
    "normalize_button",
]
