"""Thread-safe application state shared by workers and the GUI.

The objects in this module deliberately have no Qt dependency.  A GUI may
subscribe to :class:`StateManager` and forward snapshots through a Qt signal;
subscribers are invoked on the thread that changed the state.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import Enum
from time import monotonic
from typing import Final, TypeAlias

logger = logging.getLogger(__name__)


class AppState(str, Enum):
    """High-level states shown by the application."""

    IDLE = "idle"
    COUNTDOWN = "countdown"
    RECORDING = "recording"
    PLAYING = "playing"
    CLICKING = "clicking"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"

    @property
    def label(self) -> str:
        """Return a concise Chinese label suitable for the status bar."""

        return {
            AppState.IDLE: "空闲",
            AppState.COUNTDOWN: "倒计时",
            AppState.RECORDING: "录制",
            AppState.PLAYING: "播放",
            AppState.CLICKING: "连点",
            AppState.PAUSED: "暂停",
            AppState.STOPPED: "停止",
            AppState.ERROR: "错误",
        }[self]


class StateTransitionError(RuntimeError):
    """Raised when a caller requests an invalid state transition."""


_ALLOWED_TRANSITIONS: Final[dict[AppState, frozenset[AppState]]] = {
    AppState.IDLE: frozenset(
        {
            AppState.COUNTDOWN,
            AppState.RECORDING,
            AppState.PLAYING,
            AppState.CLICKING,
            AppState.STOPPED,
            AppState.ERROR,
        }
    ),
    AppState.COUNTDOWN: frozenset(
        {
            AppState.RECORDING,
            AppState.PLAYING,
            AppState.CLICKING,
            AppState.PAUSED,
            AppState.STOPPED,
            AppState.IDLE,
            AppState.ERROR,
        }
    ),
    AppState.RECORDING: frozenset(
        {AppState.STOPPED, AppState.IDLE, AppState.ERROR}
    ),
    AppState.PLAYING: frozenset(
        {AppState.PAUSED, AppState.STOPPED, AppState.IDLE, AppState.ERROR}
    ),
    AppState.CLICKING: frozenset(
        {AppState.PAUSED, AppState.STOPPED, AppState.IDLE, AppState.ERROR}
    ),
    AppState.PAUSED: frozenset(
        {
            AppState.COUNTDOWN,
            AppState.PLAYING,
            AppState.CLICKING,
            AppState.STOPPED,
            AppState.IDLE,
            AppState.ERROR,
        }
    ),
    AppState.STOPPED: frozenset(
        {
            AppState.IDLE,
            AppState.COUNTDOWN,
            AppState.RECORDING,
            AppState.PLAYING,
            AppState.CLICKING,
            AppState.ERROR,
        }
    ),
    AppState.ERROR: frozenset(
        {
            AppState.IDLE,
            AppState.COUNTDOWN,
            AppState.RECORDING,
            AppState.PLAYING,
            AppState.CLICKING,
            AppState.STOPPED,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Immutable view of current state.

    ``current_loop`` and ``current_action`` are one-based for display and are
    zero before playback reaches the corresponding item.  ``total_loops`` is
    ``None`` for infinite playback.  ``progress`` is always in ``[0, 1]``.
    """

    state: AppState = AppState.IDLE
    message: str = "就绪"
    current_loop: int = 0
    total_loops: int | None = 1
    current_action: int = 0
    total_actions: int = 0
    progress: float = 0.0
    revision: int = 0


StateCallback: TypeAlias = Callable[[StateSnapshot], None]
_UNSET = object()


class StateManager:
    """Maintain application state and publish immutable snapshots safely."""

    def __init__(self, initial: StateSnapshot | None = None) -> None:
        self._snapshot = initial or StateSnapshot()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._subscribers: list[StateCallback] = []

    @property
    def state(self) -> AppState:
        return self.snapshot().state

    def snapshot(self) -> StateSnapshot:
        """Return the current immutable snapshot."""

        with self._lock:
            return self._snapshot

    def can_transition(self, new_state: AppState | str) -> bool:
        """Return whether ``new_state`` is valid from the current state."""

        target = _coerce_state(new_state)
        with self._lock:
            current = self._snapshot.state
            return target == current or target in _ALLOWED_TRANSITIONS[current]

    def transition(
        self,
        new_state: AppState | str,
        *,
        message: str | None = None,
        current_loop: int | object = _UNSET,
        total_loops: int | None | object = _UNSET,
        current_action: int | object = _UNSET,
        total_actions: int | object = _UNSET,
        progress: float | object = _UNSET,
        force: bool = False,
        expected_states: Iterable[AppState | str] | None = None,
    ) -> StateSnapshot:
        """Move to a new state and notify subscribers.

        Use ``force=True`` only for recovery/reset paths.  Normal invalid
        transitions raise :class:`StateTransitionError` so GUI logic errors do
        not silently leave controls in an inconsistent state.  Start gates may
        supply ``expected_states`` to claim an idle manager atomically; this
        prevents two workers from both accepting the same shared state.
        """

        target = _coerce_state(new_state)
        expected = (
            None
            if expected_states is None
            else frozenset(_coerce_state(state) for state in expected_states)
        )
        with self._lock:
            old = self._snapshot
            if expected is not None and old.state not in expected:
                raise StateTransitionError(
                    f"不能从“{old.state.label}”切换到“{target.label}”"
                )
            if (
                not force
                and target != old.state
                and target not in _ALLOWED_TRANSITIONS[old.state]
            ):
                raise StateTransitionError(
                    f"不能从“{old.state.label}”切换到“{target.label}”"
                )

            values = _validated_values(
                old,
                state=target,
                message=old.message if message is None else str(message),
                current_loop=current_loop,
                total_loops=total_loops,
                current_action=current_action,
                total_actions=total_actions,
                progress=progress,
            )
            self._snapshot = replace(old, revision=old.revision + 1, **values)
            snapshot = self._snapshot
            subscribers = tuple(self._subscribers)
            self._changed.notify_all()

        self._notify(subscribers, snapshot)
        return snapshot

    # ``set_state`` reads naturally in worker code and is kept as the public
    # synonym for callers that do not model transitions explicitly.
    set_state = transition

    def update_progress(
        self,
        *,
        current_loop: int | object = _UNSET,
        total_loops: int | None | object = _UNSET,
        current_action: int | object = _UNSET,
        total_actions: int | object = _UNSET,
        progress: float | object = _UNSET,
        message: str | None = None,
    ) -> StateSnapshot:
        """Update counters without changing the high-level state."""

        with self._lock:
            old = self._snapshot
            values = _validated_values(
                old,
                state=old.state,
                message=old.message if message is None else str(message),
                current_loop=current_loop,
                total_loops=total_loops,
                current_action=current_action,
                total_actions=total_actions,
                progress=progress,
            )
            self._snapshot = replace(old, revision=old.revision + 1, **values)
            snapshot = self._snapshot
            subscribers = tuple(self._subscribers)
            self._changed.notify_all()

        self._notify(subscribers, snapshot)
        return snapshot

    def reset(self, message: str = "就绪") -> StateSnapshot:
        """Reset all state and progress fields to their idle values."""

        return self.transition(
            AppState.IDLE,
            message=message,
            current_loop=0,
            total_loops=1,
            current_action=0,
            total_actions=0,
            progress=0.0,
            force=True,
        )

    def subscribe(
        self, callback: StateCallback, *, emit_current: bool = False
    ) -> Callable[[], None]:
        """Register ``callback`` and return an idempotent unsubscribe function.

        Callbacks run outside the manager lock.  A Qt application should make
        the callback emit a Qt signal instead of touching widgets directly.
        """

        if not callable(callback):
            raise TypeError("状态订阅者必须可调用")
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)
            current = self._snapshot

        if emit_current:
            self._notify((callback,), current)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def wait_for_state(
        self,
        states: AppState | str | Iterable[AppState | str],
        timeout: float | None = None,
    ) -> StateSnapshot | None:
        """Wait until one of ``states`` is reached, returning ``None`` on timeout."""

        if isinstance(states, (AppState, str)):
            wanted = {_coerce_state(states)}
        else:
            wanted = {_coerce_state(item) for item in states}
        if not wanted:
            raise ValueError("至少需要一个等待状态")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("等待超时必须是非负有限数值或 None")

        deadline = None if timeout is None else monotonic() + timeout
        with self._changed:
            while self._snapshot.state not in wanted:
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._changed.wait(remaining)
            return self._snapshot

    @staticmethod
    def _notify(
        subscribers: tuple[StateCallback, ...], snapshot: StateSnapshot
    ) -> None:
        for callback in subscribers:
            try:
                callback(snapshot)
            except Exception:  # pragma: no cover - defensive GUI boundary
                logger.exception("状态订阅回调执行失败")


def _coerce_state(value: AppState | str) -> AppState:
    if isinstance(value, AppState):
        return value
    try:
        return AppState(str(value))
    except ValueError as exc:
        raise ValueError(f"未知应用状态：{value!r}") from exc


def _validated_values(
    old: StateSnapshot,
    *,
    state: AppState,
    message: str,
    current_loop: int | object,
    total_loops: int | None | object,
    current_action: int | object,
    total_actions: int | object,
    progress: float | object,
) -> dict[str, object]:
    loop = old.current_loop if current_loop is _UNSET else _non_negative_int(
        current_loop, "当前循环"
    )
    loops = old.total_loops if total_loops is _UNSET else total_loops
    if loops is not None:
        loops = _non_negative_int(loops, "总循环数")
        if loops == 0:
            raise ValueError("总循环数必须大于 0，或使用 None 表示无限循环")
    action = (
        old.current_action
        if current_action is _UNSET
        else _non_negative_int(current_action, "当前动作")
    )
    actions = (
        old.total_actions
        if total_actions is _UNSET
        else _non_negative_int(total_actions, "总动作数")
    )
    value = old.progress if progress is _UNSET else _finite_float(progress, "进度")
    if not 0.0 <= value <= 1.0:
        raise ValueError("进度必须在 0 到 1 之间")
    if loops is not None and loop > loops:
        raise ValueError("当前循环不能大于总循环数")
    if action > actions:
        raise ValueError("当前动作不能大于总动作数")
    return {
        "state": state,
        "message": message,
        "current_loop": loop,
        "total_loops": loops,
        "current_action": action,
        "total_actions": actions,
        "progress": value,
    }


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}必须是非负整数")
    return value


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name}必须是有限数值")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name}必须是有限数值")
    return number


# Concise aliases for code that treats state as playback-specific.
PlaybackState = AppState
ApplicationState = AppState


__all__ = [
    "AppState",
    "ApplicationState",
    "PlaybackState",
    "StateCallback",
    "StateManager",
    "StateSnapshot",
    "StateTransitionError",
]
