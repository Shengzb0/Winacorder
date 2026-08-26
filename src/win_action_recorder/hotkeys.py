"""Global control hotkeys and injected-input suppression.

The classes in this module deliberately know nothing about Qt.  ``pynput``
invokes the supplied callbacks on its listener thread, so a GUI integration
must bridge them to the main thread (for example by emitting a Qt signal).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import Enum
from typing import Any, Self


class HotkeyError(RuntimeError):
    """A user-facing failure raised by the global hotkey listener."""


class ControlHotkey(str, Enum):
    """Canonical names of the application's global control keys."""

    RECORD_TOGGLE = "f8"
    PLAY_PAUSE = "f9"
    AUTO_CLICK_TOGGLE = "f10"
    EMERGENCY_STOP = "esc"


CONTROL_KEYS = frozenset(item.value for item in ControlHotkey)
_CONTROL_VIRTUAL_KEYS = {27: "esc", 119: "f8", 120: "f9", 121: "f10"}


def normalize_key(key: object) -> str:
    """Return a stable string for a pynput key or a lightweight test fake.

    Printable keys retain their character value.  Special keys use pynput's
    lower-case names (``ctrl_l``, ``f1``, ``esc``).  A raw virtual key without
    a name is encoded as ``vk:<number>`` so it can be replayed losslessly.
    """

    if isinstance(key, str):
        text = key
        lowered = text.lower()
        if lowered.startswith("key."):
            return lowered[4:]
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        return text

    char = getattr(key, "char", None)
    if isinstance(char, str):
        return char

    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return name.lower()

    virtual_key = getattr(key, "vk", None)
    if isinstance(virtual_key, int) and not isinstance(virtual_key, bool):
        if virtual_key in _CONTROL_VIRTUAL_KEYS:
            return _CONTROL_VIRTUAL_KEYS[virtual_key]
        return f"vk:{virtual_key}"

    text = str(key)
    lowered = text.lower()
    if lowered.startswith("key."):
        return lowered[4:]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return lowered


def is_control_key(key: object) -> bool:
    """Whether *key* is one of the application's global control keys.

    Besides native key objects, this accepts serialized ``vk:<number>`` names
    used by saved scripts so playback and recording share one source of truth.
    """

    name = normalize_key(key).strip().lower()
    if name == "escape":
        name = "esc"
    if name in CONTROL_KEYS:
        return True
    if name.startswith("vk:"):
        try:
            return int(name[3:]) in _CONTROL_VIRTUAL_KEYS
        except ValueError:
            return False
    return False


class InputSuppression:
    """Thread-safe gate used while the application injects input.

    The counter makes contexts nest safely across worker threads.  The short
    trailing window accounts for an OS hook callback arriving just after the
    controller call returns.  Suppression should wrap each injected operation,
    not an entire playback session, so a real Escape press remains responsive.
    """

    def __init__(
        self,
        *,
        default_trailing_ms: float = 50.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            default_duration = self._coerce_non_negative(default_trailing_ms)
        except ValueError as exc:
            raise ValueError("抑制尾随时间不能为负数") from exc
        self._default_trailing_ms = default_duration
        self._clock = clock
        self._lock = threading.Lock()
        self._depth = 0
        self._pending_trailing_ms = 0.0
        self._suppressed_until = 0.0

    @property
    def active(self) -> bool:
        """Return ``True`` while injected input must be ignored."""

        with self._lock:
            return self._depth > 0 or self._clock() < self._suppressed_until

    def is_suppressed(self) -> bool:
        """Method-form alias useful when a callable predicate is required."""

        return self.active

    def acquire(self) -> None:
        """Enter suppression manually; prefer :meth:`suppressed` when possible."""

        with self._lock:
            self._depth += 1

    def release(self, *, trailing_ms: float | None = None) -> None:
        """Leave manual suppression and optionally retain a trailing window."""

        duration = self._coerce_duration(trailing_ms)
        with self._lock:
            if self._depth == 0:
                raise RuntimeError("输入抑制上下文释放次数多于获取次数")
            self._pending_trailing_ms = max(self._pending_trailing_ms, duration)
            self._depth -= 1
            if self._depth == 0 and self._pending_trailing_ms:
                self._suppressed_until = max(
                    self._suppressed_until,
                    self._clock() + self._pending_trailing_ms / 1000.0,
                )
                self._pending_trailing_ms = 0.0

    @contextmanager
    def suppressed(self, *, trailing_ms: float | None = None) -> Iterator[None]:
        """Context manager that suppresses hotkey handling for injected input."""

        self.acquire()
        try:
            yield
        finally:
            self.release(trailing_ms=trailing_ms)

    def suppress(self, *, trailing_ms: float | None = None) -> Iterator[None]:
        """Alias for :meth:`suppressed` for concise player integration."""

        return self.suppressed(trailing_ms=trailing_ms)

    def suppress_for(self, duration_ms: float) -> None:
        """Suppress callbacks until at least *duration_ms* from now."""

        duration = self._coerce_non_negative(duration_ms)
        with self._lock:
            self._suppressed_until = max(
                self._suppressed_until,
                self._clock() + duration / 1000.0,
            )

    def clear(self) -> None:
        """Clear all suppression, primarily for orderly playback cleanup."""

        with self._lock:
            self._depth = 0
            self._pending_trailing_ms = 0.0
            self._suppressed_until = 0.0

    def _coerce_duration(self, value: float | None) -> float:
        if value is None:
            return self._default_trailing_ms
        return self._coerce_non_negative(value)

    @staticmethod
    def _coerce_non_negative(value: float) -> float:
        if isinstance(value, bool):
            raise ValueError("输入抑制时间必须是非负数")  # noqa: TRY004
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("输入抑制时间必须是非负数") from exc
        if not math.isfinite(result) or result < 0:
            raise ValueError("输入抑制时间必须是非负数")
        return result


DEFAULT_INPUT_SUPPRESSION = InputSuppression()


@contextmanager
def suppress_injected_input(*, trailing_ms: float | None = None) -> Iterator[None]:
    """Use the process-wide suppression gate around a controller operation."""

    with DEFAULT_INPUT_SUPPRESSION.suppressed(trailing_ms=trailing_ms):
        yield


ListenerFactory = Callable[..., Any]
HotkeyCallback = Callable[[], None]
ErrorCallback = Callable[[HotkeyError], None]


class _ListenerShutdownCoordinator:
    """Stop native listeners off-thread and confirm that every hook exited.

    ``pynput.Listener.stop`` is normally quick, but it is still native-facing
    code and may raise or stall.  The coordinator keeps the caller non-blocking,
    reports both stop failures and exit timeouts, and retains strong references
    to listeners until their threads are confirmed dead.  A component must not
    start a replacement listener while :attr:`pending` is true.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        component_name: str,
        on_error: Callable[[str], None],
    ) -> None:
        self._timeout_s = timeout_s
        self._component_name = component_name
        self._on_error = on_error
        self._lock = threading.RLock()
        self._pending: dict[int, tuple[str, Any]] = {}
        self._complete = threading.Event()
        self._complete.set()
        self._serial = 0
        self._timeout_reported_serial = -1

    @property
    def pending(self) -> bool:
        self._prune_finished()
        with self._lock:
            return bool(self._pending)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait explicitly for tests/orderly shutdown; normal stop never waits."""

        return self._complete.wait(timeout)

    def request(
        self,
        listeners: list[tuple[str, Any | None]],
        *,
        before_workers: Callable[[], None] | None = None,
    ) -> None:
        """Request stop and start background exit confirmation."""

        registered: list[tuple[int, str, Any]] = []
        with self._lock:
            for label, listener in listeners:
                if listener is None:
                    continue
                token = id(listener)
                if token in self._pending:
                    continue
                self._pending[token] = (label, listener)
                registered.append((token, label, listener))
            if not registered:
                return
            self._serial += 1
            serial = self._serial
            self._complete.clear()

        # The listeners are already reserved as pending, so a re-entrant
        # ``start`` from an error callback cannot install duplicate hooks.
        if before_workers is not None:
            before_workers()

        for token, label, listener in registered:
            threading.Thread(
                target=self._stop_and_watch_one,
                args=(token, label, listener),
                name=f"WinActionRecorder-{self._component_name}-{label}-shutdown",
                daemon=True,
            ).start()
        threading.Thread(
            target=self._watch_timeout,
            args=(serial,),
            name=f"WinActionRecorder-{self._component_name}-shutdown-watchdog",
            daemon=True,
        ).start()

    def _stop_and_watch_one(self, token: int, label: str, listener: Any) -> None:
        stop = getattr(listener, "stop", None)
        if not callable(stop):
            self._report(f"{label}监听器缺少 stop 方法，无法确认系统 Hook 已释放")
            return
        try:
            stop()
        except Exception as exc:  # noqa: BLE001 - native listener boundary
            self._report(f"请求停止{label}监听失败：{exc}")

        join = getattr(listener, "join", None)
        is_alive = getattr(listener, "is_alive", None)
        if not callable(join):
            if callable(is_alive):
                self._poll_without_join(token, listener, is_alive)
            elif self._listener_reports_stopped(listener):
                self._mark_finished(token, listener)
            else:
                self._report(f"{label}监听器缺少 join 方法，无法确认系统 Hook 已退出")
            return

        if not callable(is_alive):
            try:
                # This blocks only the dedicated daemon watcher, never Qt.
                join()
            except Exception as exc:  # noqa: BLE001 - native listener boundary
                self._report(f"确认{label}监听退出失败：{exc}")
                return
            self._mark_finished(token, listener)
            return

        join_error_reported = False
        while True:
            try:
                join(0.1)
            except Exception as exc:  # noqa: BLE001 - native listener boundary
                if not join_error_reported:
                    self._report(f"确认{label}监听退出失败：{exc}")
                    join_error_reported = True
                # Avoid a hot loop if a broken join keeps raising while alive.
                self._complete.wait(0.1)
            try:
                alive = bool(is_alive())
            except Exception as exc:  # noqa: BLE001 - native listener boundary
                self._report(f"无法确认{label}监听线程状态：{exc}")
                return
            if not alive:
                self._mark_finished(token, listener)
                return

    def _poll_without_join(
        self, token: int, listener: Any, is_alive: Callable[[], object]
    ) -> None:
        while True:
            try:
                if not bool(is_alive()):
                    self._mark_finished(token, listener)
                    return
            except Exception as exc:  # noqa: BLE001 - native listener boundary
                self._report(f"无法确认监听线程状态：{exc}")
                return
            self._complete.wait(0.1)

    def _watch_timeout(self, serial: int) -> None:
        if self._complete.wait(self._timeout_s):
            return
        with self._lock:
            if (
                serial != self._serial
                or not self._pending
                or self._timeout_reported_serial == serial
            ):
                return
            self._timeout_reported_serial = serial
            labels = "、".join(
                dict.fromkeys(label for label, _listener in self._pending.values())
            )
        self._report(
            f"{labels}监听未在 {self._timeout_s:g} 秒内退出；"
            "为避免累积系统 Hook，暂时禁止启动新会话"
        )

    def _prune_finished(self) -> None:
        with self._lock:
            candidates = tuple(self._pending.items())
        for token, (_label, listener) in candidates:
            is_alive = getattr(listener, "is_alive", None)
            if not callable(is_alive):
                continue
            try:
                alive = bool(is_alive())
            except Exception:  # noqa: BLE001, S112 - native listener probe
                continue
            if not alive:
                self._mark_finished(token, listener)

    @staticmethod
    def _listener_reports_stopped(listener: Any) -> bool:
        stopped = getattr(listener, "stopped", None)
        return isinstance(stopped, bool) and stopped

    def _mark_finished(self, token: int, listener: Any) -> None:
        with self._lock:
            current = self._pending.get(token)
            if current is None or current[1] is not listener:
                return
            del self._pending[token]
            if not self._pending:
                self._complete.set()

    def _report(self, message: str) -> None:
        try:
            self._on_error(message)
        except Exception:  # noqa: BLE001 - error reporting must stay isolated
            return


class GlobalHotkeyManager:
    """Listen for F8/F9/F10/Escape without blocking the calling thread."""

    def __init__(
        self,
        on_record_toggle: HotkeyCallback | None = None,
        on_play_pause: HotkeyCallback | None = None,
        on_emergency_stop: HotkeyCallback | None = None,
        *,
        on_auto_click_toggle: HotkeyCallback | None = None,
        on_error: ErrorCallback | None = None,
        suppression: InputSuppression | None = None,
        listener_factory: ListenerFactory | None = None,
        listener_join_timeout_s: float = 1.0,
    ) -> None:
        if isinstance(listener_join_timeout_s, bool):
            raise ValueError(  # noqa: TRY004 - one validation error contract
                "监听线程退出等待时间必须是非负有限数值"
            )
        try:
            join_timeout = float(listener_join_timeout_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("监听线程退出等待时间必须是非负有限数值") from exc
        if not math.isfinite(join_timeout) or join_timeout < 0:
            raise ValueError("监听线程退出等待时间必须是非负有限数值")
        self._callbacks: dict[str, HotkeyCallback | None] = {
            ControlHotkey.RECORD_TOGGLE.value: on_record_toggle,
            ControlHotkey.PLAY_PAUSE.value: on_play_pause,
            ControlHotkey.AUTO_CLICK_TOGGLE.value: on_auto_click_toggle,
            ControlHotkey.EMERGENCY_STOP.value: on_emergency_stop,
        }
        self._on_error = on_error
        self._suppression = suppression or DEFAULT_INPUT_SUPPRESSION
        self._listener_factory = listener_factory or self._default_listener_factory
        self._join_timeout_s = join_timeout

        self._lock = threading.RLock()
        self._listener: Any | None = None
        self._monitor_thread: threading.Thread | None = None
        self._running = False
        self._generation = 0
        self._pressed_controls: set[str] = set()
        self._last_error: HotkeyError | None = None
        self._shutdown = _ListenerShutdownCoordinator(
            timeout_s=self._join_timeout_s,
            component_name="hotkeys",
            on_error=self._report_shutdown_error,
        )

    @staticmethod
    def _default_listener_factory(**callbacks: Callable[..., None]) -> Any:
        from pynput import keyboard

        return keyboard.Listener(**callbacks)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def last_error(self) -> HotkeyError | None:
        with self._lock:
            return self._last_error

    @property
    def suppression(self) -> InputSuppression:
        return self._suppression

    def set_callbacks(
        self,
        *,
        on_record_toggle: HotkeyCallback | None = None,
        on_play_pause: HotkeyCallback | None = None,
        on_auto_click_toggle: HotkeyCallback | None = None,
        on_emergency_stop: HotkeyCallback | None = None,
    ) -> None:
        """Replace callbacks atomically while retaining the active listener."""

        with self._lock:
            self._callbacks = {
                ControlHotkey.RECORD_TOGGLE.value: on_record_toggle,
                ControlHotkey.PLAY_PAUSE.value: on_play_pause,
                ControlHotkey.AUTO_CLICK_TOGGLE.value: on_auto_click_toggle,
                ControlHotkey.EMERGENCY_STOP.value: on_emergency_stop,
            }

    def start(self) -> bool:
        """Start the background listener; raise a Chinese error on failure."""

        listener: Any | None = None
        with self._lock:
            if self._running:
                return False
            if self._shutdown.pending:
                raise HotkeyError(
                    "上一轮全局快捷键监听仍在退出；为避免重复系统 Hook，请稍后重试"
                )
            self._generation += 1
            generation = self._generation
            self._last_error = None
            self._pressed_controls.clear()
            try:
                listener = self._listener_factory(
                    on_press=lambda key: self._on_press(generation, key),
                    on_release=lambda key: self._on_release(generation, key),
                )
                if not callable(getattr(listener, "start", None)):
                    raise TypeError("监听器缺少 start 方法")
                self._listener = listener
                self._running = True
                listener.start()
            except Exception as exc:
                self._listener = None
                self._running = False
                error = HotkeyError(f"全局快捷键监听启动失败：{exc}")
                self._last_error = error
                self._shutdown.request([("快捷键", listener)])
                raise error from exc

            self._start_monitor(listener, generation)
            return True

    def stop(self) -> bool:
        """Request listener shutdown without blocking; repeated calls are harmless."""

        with self._lock:
            was_running = self._running or self._listener is not None
            self._running = False
            self._generation += 1
            self._pressed_controls.clear()
            listener = self._listener
            self._listener = None
            self._monitor_thread = None

        self._shutdown.request([("快捷键", listener)])
        return was_running

    @property
    def shutdown_pending(self) -> bool:
        return self._shutdown.pending

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        return self._shutdown.wait(timeout)

    close = stop

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

    def _on_press(self, generation: int, key: object) -> None:
        name = normalize_key(key).lower()
        if name not in CONTROL_KEYS:
            return
        with self._lock:
            if not self._running or generation != self._generation:
                return
            repeated = name in self._pressed_controls
            self._pressed_controls.add(name)
            callback = self._callbacks.get(name)
        if repeated or self._suppression.active or callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - isolate external callback
            self._report_callback_error(
                HotkeyError(f"处理全局快捷键时发生错误：{exc}")
            )

    def _on_release(self, generation: int, key: object) -> None:
        name = normalize_key(key).lower()
        if name not in CONTROL_KEYS:
            return
        with self._lock:
            if generation != self._generation:
                return
            self._pressed_controls.discard(name)

    def _start_monitor(self, listener: Any, generation: int) -> None:
        if not callable(getattr(listener, "join", None)):
            return
        monitor = threading.Thread(
            target=self._monitor_listener,
            args=(listener, generation),
            name="WinActionRecorder-hotkey-monitor",
            daemon=True,
        )
        with self._lock:
            if self._listener is not listener or not self._running:
                return
            self._monitor_thread = monitor
        monitor.start()

    def _monitor_listener(self, listener: Any, generation: int) -> None:
        try:
            listener.join()
        except Exception as exc:  # noqa: BLE001 - pynput propagates via join
            self._handle_listener_failure(listener, generation, exc)
        else:
            self._handle_listener_failure(
                listener, generation, RuntimeError("监听线程意外退出")
            )

    def _handle_listener_failure(
        self, listener: Any, generation: int, exc: Exception
    ) -> None:
        with self._lock:
            if (
                not self._running
                or generation != self._generation
                or listener is not self._listener
            ):
                return
            self._running = False
            self._listener = None
            self._pressed_controls.clear()
            error = HotkeyError(f"全局快捷键监听运行失败：{exc}")
            self._last_error = error
        self._shutdown.request(
            [("快捷键", listener)],
            before_workers=lambda: self._notify_error(error),
        )

    def _report_shutdown_error(self, message: str) -> None:
        error = HotkeyError(message)
        with self._lock:
            # A listener/start/callback failure is the causal error. Cleanup
            # may subsequently discover that the same broken listener also
            # cannot be joined; report that detail without replacing the
            # exception that callers need in order to diagnose the failure.
            if self._last_error is None:
                self._last_error = error
        self._notify_error(error)

    def _report_callback_error(self, error: HotkeyError) -> None:
        with self._lock:
            self._last_error = error
        self._notify_error(error)

    def _notify_error(self, error: HotkeyError) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(error)
        except Exception:  # noqa: BLE001 - error callback must be isolated
            # Error reporting must never terminate a pynput listener thread.
            return

# Short alias retained for integrations that prefer the concise class name.
HotkeyManager = GlobalHotkeyManager


__all__ = [
    "CONTROL_KEYS",
    "DEFAULT_INPUT_SUPPRESSION",
    "ControlHotkey",
    "GlobalHotkeyManager",
    "HotkeyError",
    "HotkeyManager",
    "InputSuppression",
    "is_control_key",
    "normalize_key",
    "suppress_injected_input",
]
