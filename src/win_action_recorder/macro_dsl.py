"""Safe, bounded macro language for keyboard and mouse automation.

The language is deliberately line-oriented.  Its numeric expressions are
parsed by this module (never by ``eval``), and it provides no imports, file or
network access, object traversal, or arbitrary Python execution.  Source is
compiled to an immutable plan accepted by
:class:`~win_action_recorder.player.Player`.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from random import Random
from time import monotonic
from types import MappingProxyType
from typing import Any, Final, TypeAlias

from .player import (
    ControllerFactory,
    InputController,
    PlaybackCallback,
    PlaybackEvent,
    PlaybackOutcome,
    PlaybackResult,
    PlaybackSettings,
    Player,
    RandomSource,
    sample_randomized_delay_ms,
)
from .state import AppState, StateManager

logger = logging.getLogger(__name__)


DEFAULT_NEXT_MS: Final[float] = 100.0
DEFAULT_CLICK_HOLD_MS: Final[float] = 20.0
DEFAULT_CLICK_INTERVAL_MS: Final[float] = 100.0
MIN_CLICK_HOLD_MS: Final[float] = 1.0
MAX_CLICK_HOLD_MS: Final[float] = 60_000.0
DEFAULT_KEY_HOLD_MS: Final[float] = 30.0
MIN_KEY_HOLD_MS: Final[float] = 1.0
MAX_KEY_HOLD_MS: Final[float] = 60_000.0
MAX_HOTKEY_KEYS: Final[int] = 16
_INT32_MIN: Final[int] = -(2**31)
_INT32_MAX: Final[int] = 2**31 - 1
_DURATION_RE = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+))(?P<unit>ms|s)?$",
    re.IGNORECASE,
)
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_VK_RE = re.compile(r"^vk:(\d+)$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        "let",
        "function",
        "func",
        "call",
        "next",
        "wait",
        "random",
        "mouse",
        "key",
        "hotkey",
        "repeat",
        "while",
        "true",
        "break",
    }
)

_SPECIAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "alt",
        "alt_l",
        "alt_r",
        "alt_gr",
        "backspace",
        "caps_lock",
        "cmd",
        "cmd_l",
        "cmd_r",
        "ctrl",
        "ctrl_l",
        "ctrl_r",
        "delete",
        "down",
        "end",
        "enter",
        "esc",
        "home",
        "insert",
        "left",
        "media_next",
        "media_play_pause",
        "media_previous",
        "media_volume_down",
        "media_volume_mute",
        "media_volume_up",
        "menu",
        "num_lock",
        "page_down",
        "page_up",
        "pause",
        "print_screen",
        "right",
        "scroll_lock",
        "shift",
        "shift_l",
        "shift_r",
        "space",
        "tab",
        "up",
        # Friendly aliases already supported by PynputInputController.
        "return",
        "escape",
        "control",
        "control_l",
        "control_r",
        "windows",
        "win",
        "win_l",
        "win_r",
        "pageup",
        "pagedown",
    }
    | {f"f{number}" for number in range(1, 25)}
)


class MacroLanguageError(ValueError):
    """Base error carrying an exact source position suitable for an editor."""

    def __init__(self, message: str, line: int, column: int) -> None:
        self.message = str(message)
        self.line = int(line)
        self.column = int(column)
        super().__init__(f"第 {self.line} 行，第 {self.column} 列：{self.message}")


class MacroSyntaxError(MacroLanguageError):
    """Raised when source text does not match the macro grammar."""


class MacroCompileError(MacroLanguageError):
    """Raised when valid syntax would be unsafe or cannot be compiled."""


class MacroRunError(RuntimeError):
    """Raised when a macro runner cannot be started safely."""


@dataclass(frozen=True, slots=True)
class SourcePosition:
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class MacroLimits:
    """Resource limits used by parsing, compilation, and execution."""

    max_source_chars: int = 100_000
    max_statements: int = 10_000
    max_nesting: int = 16
    max_repeat_count: int = 10_000
    max_expanded_actions: int = 100_000
    max_call_depth: int = 32
    max_wait_ms: float = 86_400_000.0
    max_runtime_seconds: float = 3_600.0
    max_executed_actions: int = 100_000

    def __post_init__(self) -> None:
        integer_names = (
            "max_source_chars",
            "max_statements",
            "max_nesting",
            "max_repeat_count",
            "max_expanded_actions",
            "max_call_depth",
            "max_executed_actions",
        )
        for name in integer_names:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("max_wait_ms", "max_runtime_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} 必须是正有限数值")


@dataclass(frozen=True, slots=True)
class VariableExpression:
    name: str
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class UnaryExpression:
    operator: str
    operand: NumericValue
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class BinaryExpression:
    left: NumericValue
    operator: str
    right: NumericValue
    position: SourcePosition


NumericValue: TypeAlias = (
    int | float | VariableExpression | UnaryExpression | BinaryExpression
)


@dataclass(frozen=True, slots=True)
class NextStatement:
    duration_ms: NumericValue
    position: SourcePosition
    random_offset_ms: NumericValue = 0.0


@dataclass(frozen=True, slots=True)
class MouseStatement:
    operation: str
    position: SourcePosition
    button: str | None = None
    x: NumericValue | None = None
    y: NumericValue | None = None
    last_ms: NumericValue = DEFAULT_CLICK_HOLD_MS
    count: NumericValue = 1
    interval_ms: NumericValue = DEFAULT_CLICK_INTERVAL_MS
    after_ms: NumericValue = 0.0
    dx: NumericValue = 0
    dy: NumericValue = 0


@dataclass(frozen=True, slots=True)
class KeyStatement:
    key: str
    position: SourcePosition
    mode: str = "tap"
    last_ms: NumericValue = DEFAULT_KEY_HOLD_MS


@dataclass(frozen=True, slots=True)
class HotkeyStatement:
    keys: tuple[str, ...]
    position: SourcePosition
    last_ms: NumericValue = DEFAULT_KEY_HOLD_MS


@dataclass(frozen=True, slots=True)
class RepeatStatement:
    count: NumericValue
    body: tuple[Statement, ...]
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class WhileTrueStatement:
    body: tuple[Statement, ...]
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class BreakStatement:
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class LetStatement:
    name: str
    expression: NumericValue
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class FunctionStatement:
    name: str
    parameters: tuple[str, ...]
    body: tuple[Statement, ...]
    position: SourcePosition


@dataclass(frozen=True, slots=True)
class CallStatement:
    name: str
    arguments: tuple[NumericValue, ...]
    position: SourcePosition


Statement: TypeAlias = (
    NextStatement
    | MouseStatement
    | KeyStatement
    | HotkeyStatement
    | RepeatStatement
    | WhileTrueStatement
    | BreakStatement
    | LetStatement
    | FunctionStatement
    | CallStatement
)


@dataclass(frozen=True, slots=True)
class MacroProgram:
    statements: tuple[Statement, ...]
    source: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CompiledAction:
    """Immutable Player-compatible action without persisted-recording metadata."""

    type: str
    delay_ms: float
    params: Mapping[str, object]
    enabled: bool = True
    delay_random_offsets_ms: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True, slots=True)
class CompiledMacro:
    """Bounded plan: a finite prefix and an optional repeating cycle."""

    program: MacroProgram
    prefix_actions: tuple[CompiledAction, ...]
    prefix_tail_wait_ms: float = 0.0
    prefix_tail_random_offsets_ms: tuple[float, ...] = ()
    loop_actions: tuple[CompiledAction, ...] | None = None
    loop_interval_ms: float = 0.0
    loop_interval_random_offsets_ms: tuple[float, ...] = ()
    limits: MacroLimits = field(default_factory=MacroLimits, repr=False)

    @property
    def is_infinite(self) -> bool:
        return self.loop_actions is not None

    @property
    def actions(self) -> tuple[CompiledAction, ...]:
        """Finite actions, or the prefix for an infinite macro."""

        return self.prefix_actions


class _TokenKind(Enum):
    WORD = "word"
    LBRACE = "{"
    RBRACE = "}"
    LBRACKET = "["
    RBRACKET = "]"
    COLON = ":"
    COMMA = ","
    SLASH = "/"
    LPAREN = "("
    RPAREN = ")"
    EQUAL = "="
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    PERCENT = "%"
    NEWLINE = "newline"
    EOF = "eof"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: _TokenKind
    text: str
    line: int
    column: int

    @property
    def position(self) -> SourcePosition:
        return SourcePosition(self.line, self.column)


def parse_macro(
    source: str, *, limits: MacroLimits | None = None
) -> MacroProgram:
    """Parse macro source without executing or evaluating arbitrary code."""

    config = limits or MacroLimits()
    if not isinstance(source, str):
        raise TypeError("宏脚本源码必须是字符串")
    if len(source) > config.max_source_chars:
        raise MacroSyntaxError(
            f"脚本长度不能超过 {config.max_source_chars} 个字符", 1, 1
        )
    tokens = _tokenize(source)
    return _Parser(tokens, source, config).parse()


def compile_macro(
    source_or_program: str | MacroProgram,
    *,
    limits: MacroLimits | None = None,
) -> CompiledMacro:
    """Compile a macro to a bounded, immutable Player execution plan."""

    config = limits or MacroLimits()
    if isinstance(source_or_program, str):
        program = parse_macro(source_or_program, limits=config)
    elif isinstance(source_or_program, MacroProgram):
        # ``MacroProgram`` is public for editor tooling, so do not assume a
        # manually constructed instance has parser-validated statements.
        # Reparse its immutable source and keep one authoritative grammar path.
        program = parse_macro(source_or_program.source, limits=config)
    else:
        raise TypeError("宏脚本必须是源码字符串或 MacroProgram")

    compiler = _Compiler(config)
    compiler.register_functions(program.statements)
    flow = compiler.compile_block(
        program.statements,
        loop_depth=0,
        environment={},
        call_stack=(),
    )
    if flow.kind is _FlowKind.BREAK:
        # The parser normally prevents this; retain a defensive semantic guard.
        pos = flow.position or SourcePosition(1, 1)
        raise MacroCompileError("break 只能写在循环块中", pos.line, pos.column)

    if flow.kind is _FlowKind.INFINITE:
        prefix_actions, prefix_tail, prefix_tail_random = compiler.lower(flow.prefix)
        loop_actions, loop_tail, loop_tail_random = compiler.lower(flow.cycle)
        if not loop_actions and loop_tail <= 0:
            pos = flow.position or SourcePosition(1, 1)
            raise MacroCompileError(
                "while true 不能为空；请加入动作或非零等待，避免忙循环",
                pos.line,
                pos.column,
            )
        return CompiledMacro(
            program=program,
            prefix_actions=prefix_actions,
            prefix_tail_wait_ms=prefix_tail,
            prefix_tail_random_offsets_ms=prefix_tail_random,
            loop_actions=loop_actions,
            loop_interval_ms=loop_tail,
            loop_interval_random_offsets_ms=loop_tail_random,
            limits=config,
        )

    prefix_actions, tail_wait, tail_random = compiler.lower(flow.prefix)
    if not prefix_actions and tail_wait <= 0:
        raise MacroCompileError("脚本中没有可运行的命令", 1, 1)
    return CompiledMacro(
        program=program,
        prefix_actions=prefix_actions,
        prefix_tail_wait_ms=tail_wait,
        prefix_tail_random_offsets_ms=tail_random,
        limits=config,
    )


class _Parser:
    def __init__(
        self, tokens: tuple[_Token, ...], source: str, limits: MacroLimits
    ) -> None:
        self.tokens = tokens
        self.source = source
        self.limits = limits
        self.index = 0
        self.statement_count = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def parse(self) -> MacroProgram:
        statements = self._parse_block(depth=0, loop_depth=0, root=True)
        return MacroProgram(statements=statements, source=self.source)

    def _parse_block(
        self, *, depth: int, loop_depth: int, root: bool = False
    ) -> tuple[Statement, ...]:
        if depth > self.limits.max_nesting:
            token = self.current
            raise MacroSyntaxError(
                f"嵌套层数不能超过 {self.limits.max_nesting}",
                token.line,
                token.column,
            )
        result: list[Statement] = []
        self._skip_newlines()
        while self.current.kind is not _TokenKind.EOF:
            if self.current.kind is _TokenKind.RBRACE:
                if root:
                    self._error(self.current, "这里没有与之匹配的左花括号 {")
                self.index += 1
                return tuple(result)
            result.append(self._parse_statement(depth=depth, loop_depth=loop_depth))
            self.statement_count += 1
            if self.statement_count > self.limits.max_statements:
                token = self.current
                raise MacroSyntaxError(
                    f"命令数量不能超过 {self.limits.max_statements}",
                    token.line,
                    token.column,
                )
            self._skip_newlines()
        if not root:
            token = self.current
            self._error(token, "循环块缺少右花括号 }")
        return tuple(result)

    def _parse_statement(self, *, depth: int, loop_depth: int) -> Statement:
        if self.current.kind is _TokenKind.SLASH:
            self.index += 1
        command = self._expect_word("这里需要一条命令")
        name = command.text.casefold()
        if name == "let":
            name_token = self._expect_word("let 后面需要变量名")
            variable_name = self._parse_identifier(name_token, "变量名")
            self._expect(_TokenKind.EQUAL, "变量名后面需要等号 =")
            if self._at_statement_end():
                self._error(self.current, "等号后面需要数值表达式")
            expression = self._parse_expression("变量值")
            self._expect_statement_end()
            return LetStatement(variable_name, expression, command.position)
        if name in {"function", "func"}:
            if depth != 0:
                self._error(command, "function 只能在脚本最外层声明")
            name_token = self._expect_word("function 后面需要函数名")
            function_name = self._parse_identifier(name_token, "函数名")
            self._expect(_TokenKind.LPAREN, "函数名后面需要左括号 (")
            parameters: list[str] = []
            seen: set[str] = set()
            if self.current.kind is not _TokenKind.RPAREN:
                while True:
                    parameter_token = self._expect_word("这里需要参数名")
                    parameter = self._parse_identifier(parameter_token, "参数名")
                    if parameter in seen:
                        self._error(parameter_token, f"参数 {parameter!r} 不能重复")
                    seen.add(parameter)
                    parameters.append(parameter)
                    if self.current.kind is _TokenKind.RPAREN:
                        break
                    self._expect(_TokenKind.COMMA, "函数参数之间需要逗号 ,")
            self._expect(_TokenKind.RPAREN, "函数参数缺少右括号 )")
            self._expect(_TokenKind.LBRACE, "function 块需要左花括号 {")
            self._expect_line_break_after_open_brace()
            body = self._parse_block(depth=depth + 1, loop_depth=0, root=False)
            self._expect_statement_end()
            return FunctionStatement(
                function_name,
                tuple(parameters),
                body,
                command.position,
            )
        if name == "call":
            name_token = self._expect_word("call 后面需要函数名")
            function_name = self._parse_identifier(name_token, "函数名")
            self._expect(_TokenKind.LPAREN, "函数名后面需要左括号 (")
            arguments: list[NumericValue] = []
            if self.current.kind is not _TokenKind.RPAREN:
                while True:
                    arguments.append(self._parse_expression("函数参数"))
                    if self.current.kind is _TokenKind.RPAREN:
                        break
                    self._expect(_TokenKind.COMMA, "函数参数之间需要逗号 ,")
            self._expect(_TokenKind.RPAREN, "函数调用缺少右括号 )")
            self._expect_statement_end()
            return CallStatement(
                function_name,
                tuple(arguments),
                command.position,
            )
        if name in {"next", "wait"}:
            duration: NumericValue = DEFAULT_NEXT_MS
            random_offset: NumericValue = 0.0
            if not self._at_statement_end():
                duration_token = self.current
                duration = self._parse_expression("等待时间")
                self._validate_literal_duration(
                    duration,
                    duration_token,
                    "等待时间",
                )
            if not self._at_statement_end():
                random_token = self._expect_word(
                    "等待时间后只能写 random 与随机偏移量"
                )
                if random_token.text.casefold() != "random":
                    self._error(
                        random_token,
                        "等待时间后只能写 random，例如 next 0.5s random 0.25s",
                    )
                if self._at_statement_end():
                    self._error(
                        self.current,
                        "random 后需要随机偏移量，例如 random 0.25s",
                    )
                offset_token = self.current
                random_offset = self._parse_expression("随机偏移量")
                self._validate_literal_duration(
                    random_offset,
                    offset_token,
                    "随机偏移量",
                )
                if (
                    _is_number(duration)
                    and _is_number(random_offset)
                    and float(random_offset) > float(duration)
                ):
                    self._error(
                        offset_token,
                        "随机偏移量不能大于基础等待，否则最短等待会小于 0",
                    )
                if (
                    _is_number(duration)
                    and _is_number(random_offset)
                    and float(duration) + float(random_offset)
                    > self.limits.max_wait_ms
                ):
                    self._error(
                        offset_token,
                        "基础等待加随机偏移不能超过 "
                        f"{self.limits.max_wait_ms:g} 毫秒",
                    )
            self._expect_statement_end()
            return NextStatement(duration, command.position, random_offset)
        if name == "mouse":
            return self._parse_mouse(command)
        if name == "key":
            return self._parse_key_statement(command)
        if name == "hotkey":
            return self._parse_hotkey(command)
        if name == "repeat":
            count_token = self.current
            if self._at_statement_end():
                self._error(count_token, "repeat 后面需要重复次数")
            count = self._parse_expression("重复次数")
            self._validate_literal_integer(
                count,
                count_token,
                "重复次数",
                minimum=1,
                maximum=self.limits.max_repeat_count,
                maximum_suffix=" 次",
            )
            self._expect(_TokenKind.LBRACE, "repeat 块需要左花括号 {")
            self._expect_line_break_after_open_brace()
            body = self._parse_block(
                depth=depth + 1, loop_depth=loop_depth + 1, root=False
            )
            self._expect_statement_end()
            return RepeatStatement(count, body, command.position)
        if name == "while":
            condition = self._expect_word("while 后面只能写 true")
            if condition.text.casefold() != "true":
                self._error(condition, "只支持 while true，不支持其他条件")
            self._expect(_TokenKind.LBRACE, "while true 块需要左花括号 {")
            self._expect_line_break_after_open_brace()
            body = self._parse_block(
                depth=depth + 1, loop_depth=loop_depth + 1, root=False
            )
            self._expect_statement_end()
            return WhileTrueStatement(body, command.position)
        if name == "break":
            if loop_depth <= 0:
                self._error(command, "break 只能写在 repeat 或 while true 块中")
            self._expect_statement_end()
            return BreakStatement(command.position)
        self._error(
            command,
            f"未知命令 {command.text!r}；可用 let、function、call、next、mouse、key、hotkey、repeat、while、break",
        )

    def _parse_key_statement(self, command: _Token) -> KeyStatement:
        first_token = self._parse_key_token()
        first_name = first_token.text.casefold()
        if (
            first_name in {"down", "up"}
            and not self._at_statement_end()
            and self.current.kind is not _TokenKind.LBRACKET
        ):
            key_token = self._parse_key_token()
            if self.current.kind is _TokenKind.LBRACKET:
                self._error(
                    self.current,
                    "显式 key down/key up 不支持 last；请把等待写成 next",
                )
            self._expect_statement_end()
            return KeyStatement(
                _normalize_key(key_token),
                command.position,
                mode=first_name,
            )

        last_ms = DEFAULT_KEY_HOLD_MS
        if self.current.kind is _TokenKind.LBRACKET:
            last_ms = self._parse_key_hold_options("按键")
        self._expect_statement_end()
        self._validate_key_hold(last_ms, first_token, "按键保持时间 last")
        return KeyStatement(
            _normalize_key(first_token),
            command.position,
            last_ms=last_ms,
        )

    def _parse_hotkey(self, command: _Token) -> HotkeyStatement:
        keys: list[str] = []
        seen: set[str] = set()
        while (
            not self._at_statement_end()
            and self.current.kind is not _TokenKind.LBRACKET
        ):
            token = self._parse_key_token()
            key = _normalize_key(token)
            if key in seen:
                self._error(token, f"组合键中不能重复按键 {key!r}")
            seen.add(key)
            keys.append(key)
            if len(keys) > MAX_HOTKEY_KEYS:
                self._error(
                    token,
                    f"单个 hotkey 最多包含 {MAX_HOTKEY_KEYS} 个按键",
                )
        if len(keys) < 2:
            self._error(command, "hotkey 至少需要两个按键，例如 hotkey ctrl s")
        last_ms = DEFAULT_KEY_HOLD_MS
        if self.current.kind is _TokenKind.LBRACKET:
            last_ms = self._parse_key_hold_options("组合键")
        self._expect_statement_end()
        self._validate_key_hold(last_ms, command, "组合键保持时间 last")
        return HotkeyStatement(tuple(keys), command.position, last_ms)

    def _parse_key_hold_options(self, label: str) -> NumericValue:
        self._expect(_TokenKind.LBRACKET, f"{label}选项需要左方括号 [")
        if self.current.kind is _TokenKind.RBRACKET:
            self._error(self.current, f"{label}选项不能为空")
        name_token = self._expect_word(f"{label}选项只支持 last 或 hold")
        supplied_name = name_token.text.casefold()
        if supplied_name not in {"last", "hold"}:
            self._error(
                name_token,
                f"未知{label}选项 {name_token.text!r}；只支持 last 或 hold",
            )
        self._expect(
            _TokenKind.COLON,
            f"{label}选项 {supplied_name} 后面需要冒号 :",
        )
        value_token = self.current
        if self.current.kind is _TokenKind.RBRACKET:
            self._error(
                self.current,
                f"{label}选项 {supplied_name} 后面需要时长",
            )
        value = self._parse_expression(f"{label}选项 {supplied_name}")
        self._validate_literal_duration(
            value,
            value_token,
            f"{label}选项 {supplied_name}",
            maximum=min(MAX_KEY_HOLD_MS, self.limits.max_wait_ms),
        )
        self._validate_key_hold(value, value_token, f"{label}选项 {supplied_name}")
        if self.current.kind is _TokenKind.COMMA:
            self._error(self.current, f"{label}当前只有一个选项，不能再写逗号")
        self._expect(_TokenKind.RBRACKET, f"{label}选项缺少右方括号 ]")
        return value

    def _validate_key_hold(
        self,
        value: NumericValue,
        token: _Token,
        label: str,
    ) -> None:
        maximum = min(MAX_KEY_HOLD_MS, self.limits.max_wait_ms)
        if not _is_number(value):
            return
        if float(value) < MIN_KEY_HOLD_MS:
            self._error(token, f"{label}不能小于 {MIN_KEY_HOLD_MS:g} 毫秒")
        if float(value) > maximum:
            self._error(token, f"{label}不能超过 {maximum:g} 毫秒")

    def _parse_key_token(self) -> _Token:
        token = self.current
        single_character_kinds = {
            _TokenKind.LBRACKET,
            _TokenKind.RBRACKET,
            _TokenKind.COLON,
            _TokenKind.COMMA,
            _TokenKind.SLASH,
            _TokenKind.LPAREN,
            _TokenKind.RPAREN,
            _TokenKind.EQUAL,
            _TokenKind.PLUS,
            _TokenKind.MINUS,
            _TokenKind.STAR,
            _TokenKind.PERCENT,
        }
        if token.kind in single_character_kinds:
            self.index += 1
            return _Token(_TokenKind.WORD, token.text, token.line, token.column)
        token = self._expect_word("key 后面需要按键名，例如 key space")
        if token.text.casefold() != "vk" or self.current.kind is not _TokenKind.COLON:
            parts = [token.text]
            while (
                self.current.kind is _TokenKind.MINUS
                and self.index + 1 < len(self.tokens)
                and self.tokens[self.index + 1].kind is _TokenKind.WORD
            ):
                self.index += 1
                parts.append("-")
                parts.append(self.current.text)
                self.index += 1
            if len(parts) == 1:
                return token
            return _Token(
                _TokenKind.WORD,
                "".join(parts),
                token.line,
                token.column,
            )

        self.index += 1
        number = self._expect_word("vk: 后面需要虚拟键码")
        return _Token(
            _TokenKind.WORD,
            f"{token.text}:{number.text}",
            token.line,
            token.column,
        )

    def _parse_mouse(self, command: _Token) -> MouseStatement:
        operation_token = self._expect_word(
            "mouse 后面需要 left、right、middle、move 或 scroll"
        )
        operation = operation_token.text.casefold()
        if operation == "move":
            if self.current.kind is _TokenKind.LBRACKET:
                self._error(self.current, "mouse move 不支持点击选项")
            x_token = self.current
            x = self._parse_integer_slot(
                "mouse move 后面需要 X 坐标", "X 坐标"
            )
            self._validate_literal_integer(
                x, x_token, "X 坐标", minimum=_INT32_MIN, maximum=_INT32_MAX
            )
            y_token = self.current
            y = self._parse_integer_slot(
                "mouse move 后面需要 Y 坐标", "Y 坐标"
            )
            self._validate_literal_integer(
                y, y_token, "Y 坐标", minimum=_INT32_MIN, maximum=_INT32_MAX
            )
            self._expect_statement_end()
            return MouseStatement("move", command.position, x=x, y=y)
        if operation == "scroll":
            if self.current.kind is _TokenKind.LBRACKET:
                self._error(self.current, "mouse scroll 不支持点击选项")
            dx_token = self.current
            dx = self._parse_integer_slot(
                "mouse scroll 后面需要水平滚动量 DX", "水平滚动量 DX"
            )
            self._validate_literal_integer(
                dx,
                dx_token,
                "水平滚动量 DX",
                minimum=_INT32_MIN,
                maximum=_INT32_MAX,
            )
            dy_token = self.current
            dy = self._parse_integer_slot(
                "mouse scroll 后面需要垂直滚动量 DY", "垂直滚动量 DY"
            )
            self._validate_literal_integer(
                dy,
                dy_token,
                "垂直滚动量 DY",
                minimum=_INT32_MIN,
                maximum=_INT32_MAX,
            )
            if _is_number(dx) and _is_number(dy) and dx == 0 and dy == 0:
                self._error(dy_token, "滚轮的 DX 和 DY 不能同时为 0")
            x: NumericValue | None = None
            y: NumericValue | None = None
            if not self._at_statement_end():
                at_token = self._expect_word("滚轮坐标前需要写 at")
                if at_token.text.casefold() != "at":
                    self._error(
                        at_token,
                        "滚轮坐标前需要写 at，例如 mouse scroll 0 -3 at 100 200",
                    )
                x_token = self.current
                x = self._parse_integer_slot("at 后面需要 X 坐标", "X 坐标")
                self._validate_literal_integer(
                    x, x_token, "X 坐标", minimum=_INT32_MIN, maximum=_INT32_MAX
                )
                y_token = self.current
                y = self._parse_integer_slot("at 后面需要 Y 坐标", "Y 坐标")
                self._validate_literal_integer(
                    y, y_token, "Y 坐标", minimum=_INT32_MIN, maximum=_INT32_MAX
                )
            self._expect_statement_end()
            return MouseStatement(
                "scroll",
                command.position,
                x=x,
                y=y,
                dx=dx,
                dy=dy,
            )
        if operation not in {"left", "right", "middle"}:
            self._error(
                operation_token,
                "鼠标命令只支持 left、right、middle、move 或 scroll",
            )
        last_ms = DEFAULT_CLICK_HOLD_MS
        count = 1
        interval_ms = DEFAULT_CLICK_INTERVAL_MS
        after_ms = 0.0
        if self.current.kind is _TokenKind.LBRACKET:
            last_ms, count, interval_ms, after_ms = self._parse_click_options()
        x: NumericValue | None = None
        y: NumericValue | None = None
        if not self._at_statement_end():
            at_token = self._expect_word("鼠标坐标前需要写 at")
            if at_token.text.casefold() != "at":
                self._error(at_token, "鼠标坐标前需要写 at，例如 mouse left at 100 200")
            x_token = self.current
            x = self._parse_integer_slot("at 后面需要 X 坐标", "X 坐标")
            self._validate_literal_integer(
                x, x_token, "X 坐标", minimum=_INT32_MIN, maximum=_INT32_MAX
            )
            y_token = self.current
            y = self._parse_integer_slot("at 后面需要 Y 坐标", "Y 坐标")
            self._validate_literal_integer(
                y, y_token, "Y 坐标", minimum=_INT32_MIN, maximum=_INT32_MAX
            )
        self._expect_statement_end()
        self._validate_effective_click_options(
            last_ms=last_ms,
            count=count,
            interval_ms=interval_ms,
            after_ms=after_ms,
            fallback_token=operation_token,
        )
        return MouseStatement(
            "click",
            command.position,
            button=operation,
            x=x,
            y=y,
            last_ms=last_ms,
            count=count,
            interval_ms=interval_ms,
            after_ms=after_ms,
        )

    def _parse_click_options(
        self,
    ) -> tuple[NumericValue, NumericValue, NumericValue, NumericValue]:
        self._expect(_TokenKind.LBRACKET, "点击选项需要左方括号 [")
        if self.current.kind is _TokenKind.RBRACKET:
            self._error(self.current, "点击选项不能为空")

        values: dict[str, NumericValue] = {
            "last": DEFAULT_CLICK_HOLD_MS,
            "count": 1,
            "interval": DEFAULT_CLICK_INTERVAL_MS,
            "after": 0.0,
        }
        seen: set[str] = set()
        aliases = {"hold": "last"}
        while True:
            if self.current.kind in {
                _TokenKind.NEWLINE,
                _TokenKind.RBRACE,
                _TokenKind.EOF,
            }:
                self._error(self.current, "点击选项缺少右方括号 ]")
            name_token = self._expect_word(
                "这里需要点击选项名：last、count、interval 或 after"
            )
            supplied_name = name_token.text.casefold()
            name = aliases.get(supplied_name, supplied_name)
            if name not in values:
                self._error(
                    name_token,
                    f"未知点击选项 {name_token.text!r}；可用 last、count、interval、after",
                )
            if name in seen:
                self._error(name_token, f"点击选项 {name} 不能重复")
            seen.add(name)

            self._expect(
                _TokenKind.COLON,
                f"点击选项 {supplied_name} 后面需要冒号 :",
            )
            value_token = self.current
            if self.current.kind is _TokenKind.RBRACKET:
                self._error(
                    self.current,
                    f"点击选项 {supplied_name} 后面需要值",
                )
            value = self._parse_expression(
                "点击次数 count"
                if name == "count"
                else f"点击选项 {supplied_name}"
            )
            if name == "count":
                self._validate_literal_integer(
                    value,
                    value_token,
                    "点击次数 count",
                    minimum=1,
                    maximum=self.limits.max_repeat_count,
                )
                values[name] = value
            else:
                maximum = (
                    min(MAX_CLICK_HOLD_MS, self.limits.max_wait_ms)
                    if name == "last"
                    else self.limits.max_wait_ms
                )
                self._validate_literal_duration(
                    value,
                    value_token,
                    f"点击选项 {supplied_name}",
                    maximum=maximum,
                )
                values[name] = value
                if (
                    name == "last"
                    and _is_number(value)
                    and float(value) < MIN_CLICK_HOLD_MS
                ):
                    self._error(
                        value_token,
                        f"点击选项 {supplied_name} 不能小于 "
                        f"{MIN_CLICK_HOLD_MS:g} 毫秒",
                    )

            if self.current.kind is _TokenKind.RBRACKET:
                self.index += 1
                break
            if self.current.kind in {
                _TokenKind.NEWLINE,
                _TokenKind.RBRACE,
                _TokenKind.EOF,
            }:
                self._error(self.current, "点击选项缺少右方括号 ]")
            self._expect(
                _TokenKind.COMMA,
                "点击选项之间需要逗号 ,",
            )
            if self.current.kind is _TokenKind.RBRACKET:
                self._error(self.current, "逗号后面需要另一个点击选项")

        return (
            values["last"],
            values["count"],
            values["interval"],
            values["after"],
        )

    def _validate_effective_click_options(
        self,
        *,
        last_ms: NumericValue,
        count: NumericValue,
        interval_ms: NumericValue,
        after_ms: NumericValue,
        fallback_token: _Token,
    ) -> None:
        """Apply limits to defaults as well as explicitly authored values.

        Explicit values already fail at their own value token in
        ``_parse_click_options``.  These checks close the equivalent default
        value path; the button token is the closest exact source location when
        no option token exists.
        """

        maximum_last = min(MAX_CLICK_HOLD_MS, self.limits.max_wait_ms)
        if _is_number(last_ms) and float(last_ms) < MIN_CLICK_HOLD_MS:
            self._error(
                fallback_token,
                f"点击保持时间 last 不能小于 {MIN_CLICK_HOLD_MS:g} 毫秒",
            )
        if _is_number(last_ms) and float(last_ms) > maximum_last:
            self._error(
                fallback_token,
                f"点击保持时间 last 不能超过 {maximum_last:g} 毫秒",
            )
        if _is_number(count) and (
            float(count) < 1 or float(count) > self.limits.max_repeat_count
        ):
            self._error(
                fallback_token,
                f"点击次数 count 必须在 1 到 {self.limits.max_repeat_count} 之间",
            )
        effective_waits = [("after", after_ms)]
        if _is_number(count) and float(count) > 1:
            effective_waits.append(("interval", interval_ms))
        for label, value in effective_waits:
            if _is_number(value) and (
                float(value) < 0 or float(value) > self.limits.max_wait_ms
            ):
                self._error(
                    fallback_token,
                    f"点击选项 {label} 必须在 0 到 "
                    f"{self.limits.max_wait_ms:g} 毫秒之间",
                )

    def _parse_identifier(self, token: _Token, label: str) -> str:
        name = token.text.casefold()
        if _IDENTIFIER_RE.fullmatch(token.text) is None:
            self._error(
                token,
                f"{label}只能使用字母、数字和下划线，且不能以数字开头",
            )
        if name in _RESERVED_IDENTIFIERS:
            self._error(token, f"{label}不能使用保留字 {token.text!r}")
        return name

    def _parse_expression(self, label: str = "数值表达式") -> NumericValue:
        return self._parse_additive_expression(label)

    def _parse_additive_expression(self, label: str) -> NumericValue:
        expression = self._parse_multiplicative_expression(label)
        while self.current.kind in {_TokenKind.PLUS, _TokenKind.MINUS}:
            operator = self.current
            self.index += 1
            right = self._parse_multiplicative_expression(label)
            expression = BinaryExpression(
                expression,
                operator.text,
                right,
                operator.position,
            )
        return expression

    def _parse_multiplicative_expression(self, label: str) -> NumericValue:
        expression = self._parse_unary_expression(label)
        while self.current.kind in {
            _TokenKind.STAR,
            _TokenKind.SLASH,
            _TokenKind.PERCENT,
        }:
            operator = self.current
            self.index += 1
            right = self._parse_unary_expression(label)
            expression = BinaryExpression(
                expression,
                operator.text,
                right,
                operator.position,
            )
        return expression

    def _parse_unary_expression(self, label: str) -> NumericValue:
        if self.current.kind in {_TokenKind.PLUS, _TokenKind.MINUS}:
            operator = self.current
            self.index += 1
            operand = self._parse_unary_expression(label)
            if _is_number(operand):
                return (
                    float(operand)
                    if operator.kind is _TokenKind.PLUS
                    else -float(operand)
                )
            return UnaryExpression(operator.text, operand, operator.position)
        return self._parse_primary_expression(label)

    def _parse_primary_expression(self, label: str) -> NumericValue:
        token = self.current
        if token.kind is _TokenKind.LPAREN:
            self.index += 1
            expression = self._parse_expression(label)
            self._expect(_TokenKind.RPAREN, f"{label}缺少右括号 )")
            return expression
        if token.kind is not _TokenKind.WORD:
            self._error(token, f"{label}需要数值、变量名或括号表达式")
        self.index += 1
        match = _DURATION_RE.fullmatch(token.text)
        if match is not None:
            try:
                value = float(match.group("number"))
            except ValueError:
                self._error(token, f"{label}超出可处理范围")
            if match.group("unit") and match.group("unit").casefold() == "s":
                value *= 1000.0
            if not math.isfinite(value):
                self._error(token, f"{label}必须是有限值")
            if match.group("unit") is None and _INTEGER_RE.fullmatch(token.text):
                try:
                    return int(token.text)
                except ValueError:
                    self._error(token, f"{label}必须是合理范围内的整数")
            return value
        name = self._parse_identifier(token, "变量名")
        return VariableExpression(name, token.position)

    def _parse_integer_slot(self, message: str, label: str) -> NumericValue:
        if self._at_statement_end():
            self._error(self.current, message)
        if self.current.kind is _TokenKind.LPAREN:
            return self._parse_primary_expression(label)
        return self._parse_unary_expression(label)

    def _validate_literal_duration(
        self,
        value: NumericValue,
        token: _Token,
        label: str,
        *,
        maximum: float | None = None,
    ) -> None:
        if not _is_number(value):
            return
        number = float(value)
        if not math.isfinite(number) or number < 0:
            self._error(token, f"{label}必须是非负有限数值")
        upper = self.limits.max_wait_ms if maximum is None else maximum
        if number > upper:
            self._error(token, f"{label}不能超过 {upper:g} 毫秒")

    def _validate_literal_integer(
        self,
        value: NumericValue,
        token: _Token,
        label: str,
        *,
        minimum: int,
        maximum: int,
        maximum_suffix: str = "",
    ) -> None:
        if not _is_number(value):
            return
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            self._error(token, f"{label}必须是整数")
        if number < minimum:
            qualifier = "至少为" if minimum >= 0 else "不能小于"
            self._error(token, f"{label}{qualifier} {minimum}")
        if number > maximum:
            if minimum == _INT32_MIN and maximum == _INT32_MAX:
                self._error(token, f"{label}超出 32 位坐标范围")
            self._error(token, f"{label}不能超过 {maximum}{maximum_suffix}")

    def _expect_line_break_after_open_brace(self) -> None:
        if self.current.kind is _TokenKind.NEWLINE:
            self._skip_newlines()
            return
        if self.current.kind is _TokenKind.RBRACE:
            return
        self._error(self.current, "左花括号 { 后请换行书写块内命令")

    def _at_statement_end(self) -> bool:
        return self.current.kind in {
            _TokenKind.NEWLINE,
            _TokenKind.RBRACE,
            _TokenKind.EOF,
        }

    def _expect_statement_end(self) -> None:
        if not self._at_statement_end():
            self._error(self.current, f"命令末尾有多余内容 {self.current.text!r}")

    def _skip_newlines(self) -> None:
        while self.current.kind is _TokenKind.NEWLINE:
            self.index += 1

    def _expect_word(self, message: str) -> _Token:
        return self._expect(_TokenKind.WORD, message)

    def _expect(self, kind: _TokenKind, message: str) -> _Token:
        token = self.current
        if token.kind is not kind:
            self._error(token, message)
        self.index += 1
        return token

    @staticmethod
    def _error(token: _Token, message: str) -> None:
        raise MacroSyntaxError(message, token.line, token.column)


class _PrimitiveKind(Enum):
    WAIT = "wait"
    MOUSE = "mouse"
    KEY = "key"


@dataclass(frozen=True, slots=True)
class _Primitive:
    kind: _PrimitiveKind
    statement: NextStatement | MouseStatement | KeyStatement | HotkeyStatement


class _FlowKind(Enum):
    NORMAL = "normal"
    BREAK = "break"
    INFINITE = "infinite"


@dataclass(slots=True)
class _Flow:
    kind: _FlowKind
    prefix: list[_Primitive]
    cycle: list[_Primitive] = field(default_factory=list)
    position: SourcePosition | None = None


class _Compiler:
    def __init__(self, limits: MacroLimits) -> None:
        self.limits = limits
        self.expanded_actions = 0
        self.functions: dict[str, FunctionStatement] = {}

    def register_functions(self, statements: Sequence[Statement]) -> None:
        for statement in statements:
            if not isinstance(statement, FunctionStatement):
                continue
            previous = self.functions.get(statement.name)
            if previous is not None:
                raise MacroCompileError(
                    f"函数 {statement.name!r} 重复声明",
                    statement.position.line,
                    statement.position.column,
                )
            self.functions[statement.name] = statement

    def compile_block(
        self,
        statements: Sequence[Statement],
        *,
        loop_depth: int,
        environment: Mapping[str, float],
        call_stack: tuple[str, ...],
    ) -> _Flow:
        local_environment = dict(environment)
        prefix: list[_Primitive] = []
        for index, statement in enumerate(statements):
            if isinstance(statement, FunctionStatement):
                continue
            if isinstance(statement, LetStatement):
                local_environment[statement.name] = self._evaluate(
                    statement.expression,
                    local_environment,
                    "变量值",
                    statement.position,
                )
                continue
            if isinstance(statement, CallStatement):
                called = self._compile_call(
                    statement,
                    loop_depth=loop_depth,
                    environment=local_environment,
                    call_stack=call_stack,
                )
                self._guard_extension(prefix, called.prefix, statement.position)
                prefix.extend(called.prefix)
                if called.kind is _FlowKind.BREAK:
                    return _Flow(
                        _FlowKind.BREAK,
                        prefix,
                        position=called.position or statement.position,
                    )
                if called.kind is _FlowKind.INFINITE:
                    self._reject_unreachable(statements, index)
                    return _Flow(
                        _FlowKind.INFINITE,
                        prefix,
                        called.cycle,
                        called.position or statement.position,
                    )
                continue
            if isinstance(statement, NextStatement):
                prefix.append(
                    _Primitive(
                        _PrimitiveKind.WAIT,
                        self._resolve_next(statement, local_environment),
                    )
                )
                continue
            if isinstance(statement, MouseStatement):
                prefix.append(
                    _Primitive(
                        _PrimitiveKind.MOUSE,
                        self._resolve_mouse(statement, local_environment),
                    )
                )
                continue
            if isinstance(statement, KeyStatement):
                prefix.append(
                    _Primitive(
                        _PrimitiveKind.KEY,
                        self._resolve_key(statement, local_environment),
                    )
                )
                continue
            if isinstance(statement, HotkeyStatement):
                prefix.append(
                    _Primitive(
                        _PrimitiveKind.KEY,
                        self._resolve_hotkey(statement, local_environment),
                    )
                )
                continue
            if isinstance(statement, BreakStatement):
                if loop_depth <= 0:
                    raise MacroCompileError(
                        "break 只能写在循环块中",
                        statement.position.line,
                        statement.position.column,
                    )
                if index + 1 < len(statements):
                    unreachable = statements[index + 1].position
                    raise MacroCompileError(
                        "break 后面的同一循环块命令永远不会执行",
                        unreachable.line,
                        unreachable.column,
                    )
                return _Flow(_FlowKind.BREAK, prefix, position=statement.position)
            if isinstance(statement, RepeatStatement):
                repeated = self._compile_repeat(
                    statement,
                    loop_depth=loop_depth,
                    environment=local_environment,
                    call_stack=call_stack,
                )
                self._guard_extension(prefix, repeated.prefix, statement.position)
                prefix.extend(repeated.prefix)
                if repeated.kind is _FlowKind.INFINITE:
                    self._reject_unreachable(statements, index)
                    return _Flow(
                        _FlowKind.INFINITE,
                        prefix,
                        repeated.cycle,
                        repeated.position,
                    )
                continue
            if isinstance(statement, WhileTrueStatement):
                body = self.compile_block(
                    statement.body,
                    loop_depth=loop_depth + 1,
                    environment=local_environment,
                    call_stack=call_stack,
                )
                if body.kind is _FlowKind.BREAK:
                    # An unconditional break exits this while after one pass.
                    self._guard_extension(prefix, body.prefix, statement.position)
                    prefix.extend(body.prefix)
                    continue
                if body.kind is _FlowKind.INFINITE:
                    self._guard_extension(prefix, body.prefix, statement.position)
                    prefix.extend(body.prefix)
                    self._reject_unreachable(statements, index)
                    return _Flow(
                        _FlowKind.INFINITE,
                        prefix,
                        body.cycle,
                        body.position or statement.position,
                    )
                self._reject_unreachable(statements, index)
                return _Flow(
                    _FlowKind.INFINITE,
                    prefix,
                    body.prefix,
                    statement.position,
                )
            raise AssertionError(f"未处理的语句：{type(statement).__name__}")
        return _Flow(_FlowKind.NORMAL, prefix)

    def _compile_call(
        self,
        statement: CallStatement,
        *,
        loop_depth: int,
        environment: Mapping[str, float],
        call_stack: tuple[str, ...],
    ) -> _Flow:
        function = self.functions.get(statement.name)
        if function is None:
            raise MacroCompileError(
                f"未声明函数 {statement.name!r}",
                statement.position.line,
                statement.position.column,
            )
        if statement.name in call_stack:
            chain = " → ".join((*call_stack, statement.name))
            raise MacroCompileError(
                f"函数不允许递归调用：{chain}",
                statement.position.line,
                statement.position.column,
            )
        if len(call_stack) >= self.limits.max_call_depth:
            raise MacroCompileError(
                f"函数调用深度不能超过 {self.limits.max_call_depth}",
                statement.position.line,
                statement.position.column,
            )
        if len(statement.arguments) != len(function.parameters):
            raise MacroCompileError(
                f"函数 {statement.name!r} 需要 {len(function.parameters)} 个参数，"
                f"实际收到 {len(statement.arguments)} 个",
                statement.position.line,
                statement.position.column,
            )
        values = tuple(
            self._evaluate(argument, environment, "函数参数", statement.position)
            for argument in statement.arguments
        )
        function_environment = dict(environment)
        function_environment.update(zip(function.parameters, values, strict=True))
        return self.compile_block(
            function.body,
            loop_depth=loop_depth,
            environment=function_environment,
            call_stack=(*call_stack, statement.name),
        )

    def _evaluate(
        self,
        expression: NumericValue,
        environment: Mapping[str, float],
        label: str,
        fallback_position: SourcePosition,
    ) -> float:
        if _is_number(expression):
            try:
                value = float(expression)
            except (OverflowError, ValueError):
                raise MacroCompileError(
                    f"{label}超出可处理范围",
                    fallback_position.line,
                    fallback_position.column,
                ) from None
        elif isinstance(expression, VariableExpression):
            if expression.name not in environment:
                raise MacroCompileError(
                    f"变量 {expression.name!r} 尚未定义",
                    expression.position.line,
                    expression.position.column,
                )
            value = environment[expression.name]
        elif isinstance(expression, UnaryExpression):
            operand = self._evaluate(
                expression.operand,
                environment,
                label,
                expression.position,
            )
            value = operand if expression.operator == "+" else -operand
        elif isinstance(expression, BinaryExpression):
            left = self._evaluate(
                expression.left,
                environment,
                label,
                expression.position,
            )
            right = self._evaluate(
                expression.right,
                environment,
                label,
                expression.position,
            )
            if expression.operator == "+":
                value = left + right
            elif expression.operator == "-":
                value = left - right
            elif expression.operator == "*":
                value = left * right
            elif expression.operator == "/":
                if right == 0:
                    raise MacroCompileError(
                        "数值表达式不能除以 0",
                        expression.position.line,
                        expression.position.column,
                    )
                value = left / right
            else:
                if right == 0:
                    raise MacroCompileError(
                        "数值表达式不能对 0 取余",
                        expression.position.line,
                        expression.position.column,
                    )
                value = left % right
        else:  # pragma: no cover - closed public union, defensive boundary
            raise MacroCompileError(
                f"{label}包含未知表达式",
                fallback_position.line,
                fallback_position.column,
            )
        if not math.isfinite(value):
            raise MacroCompileError(
                f"{label}的计算结果必须是有限数值",
                fallback_position.line,
                fallback_position.column,
            )
        return value

    def _duration(
        self,
        expression: NumericValue,
        environment: Mapping[str, float],
        label: str,
        position: SourcePosition,
        *,
        minimum: float = 0.0,
        maximum: float | None = None,
    ) -> float:
        value = self._evaluate(expression, environment, label, position)
        upper = self.limits.max_wait_ms if maximum is None else maximum
        if value < minimum or value > upper:
            raise MacroCompileError(
                f"{label}必须在 {minimum:g} 到 {upper:g} 毫秒之间",
                position.line,
                position.column,
            )
        return value

    def _integer(
        self,
        expression: NumericValue,
        environment: Mapping[str, float],
        label: str,
        position: SourcePosition,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = self._evaluate(expression, environment, label, position)
        if not value.is_integer():
            raise MacroCompileError(
                f"{label}的计算结果必须是整数",
                position.line,
                position.column,
            )
        result = int(value)
        if not minimum <= result <= maximum:
            raise MacroCompileError(
                f"{label}必须在 {minimum} 到 {maximum} 之间",
                position.line,
                position.column,
            )
        return result

    def _resolve_next(
        self,
        statement: NextStatement,
        environment: Mapping[str, float],
    ) -> NextStatement:
        duration = self._duration(
            statement.duration_ms,
            environment,
            "等待时间",
            statement.position,
        )
        offset = self._duration(
            statement.random_offset_ms,
            environment,
            "随机偏移量",
            statement.position,
        )
        if offset > duration:
            raise MacroCompileError(
                "随机偏移量不能大于基础等待，否则最短等待会小于 0",
                statement.position.line,
                statement.position.column,
            )
        if duration + offset > self.limits.max_wait_ms:
            raise MacroCompileError(
                "基础等待加随机偏移不能超过 "
                f"{self.limits.max_wait_ms:g} 毫秒",
                statement.position.line,
                statement.position.column,
            )
        return NextStatement(duration, statement.position, offset)

    def _resolve_mouse(
        self,
        statement: MouseStatement,
        environment: Mapping[str, float],
    ) -> MouseStatement:
        if statement.operation == "move":
            assert statement.x is not None and statement.y is not None
            return MouseStatement(
                "move",
                statement.position,
                x=self._integer(
                    statement.x,
                    environment,
                    "X 坐标",
                    statement.position,
                    minimum=_INT32_MIN,
                    maximum=_INT32_MAX,
                ),
                y=self._integer(
                    statement.y,
                    environment,
                    "Y 坐标",
                    statement.position,
                    minimum=_INT32_MIN,
                    maximum=_INT32_MAX,
                ),
            )
        if statement.operation == "scroll":
            dx = self._integer(
                statement.dx,
                environment,
                "水平滚动量 DX",
                statement.position,
                minimum=_INT32_MIN,
                maximum=_INT32_MAX,
            )
            dy = self._integer(
                statement.dy,
                environment,
                "垂直滚动量 DY",
                statement.position,
                minimum=_INT32_MIN,
                maximum=_INT32_MAX,
            )
            if dx == 0 and dy == 0:
                raise MacroCompileError(
                    "滚轮的 DX 和 DY 不能同时为 0",
                    statement.position.line,
                    statement.position.column,
                )
            x = (
                None
                if statement.x is None
                else self._integer(
                    statement.x,
                    environment,
                    "X 坐标",
                    statement.position,
                    minimum=_INT32_MIN,
                    maximum=_INT32_MAX,
                )
            )
            y = (
                None
                if statement.y is None
                else self._integer(
                    statement.y,
                    environment,
                    "Y 坐标",
                    statement.position,
                    minimum=_INT32_MIN,
                    maximum=_INT32_MAX,
                )
            )
            return MouseStatement(
                "scroll",
                statement.position,
                x=x,
                y=y,
                dx=dx,
                dy=dy,
            )

        count = self._integer(
            statement.count,
            environment,
            "点击次数 count",
            statement.position,
            minimum=1,
            maximum=self.limits.max_repeat_count,
        )
        last = self._duration(
            statement.last_ms,
            environment,
            "点击保持时间 last",
            statement.position,
            minimum=MIN_CLICK_HOLD_MS,
            maximum=min(MAX_CLICK_HOLD_MS, self.limits.max_wait_ms),
        )
        interval_maximum = (
            max(DEFAULT_CLICK_INTERVAL_MS, self.limits.max_wait_ms)
            if count == 1
            and _is_number(statement.interval_ms)
            and float(statement.interval_ms) == DEFAULT_CLICK_INTERVAL_MS
            else self.limits.max_wait_ms
        )
        interval = self._duration(
            statement.interval_ms,
            environment,
            "点击间隔 interval",
            statement.position,
            maximum=interval_maximum,
        )
        after = self._duration(
            statement.after_ms,
            environment,
            "点击后等待 after",
            statement.position,
        )
        x = (
            None
            if statement.x is None
            else self._integer(
                statement.x,
                environment,
                "X 坐标",
                statement.position,
                minimum=_INT32_MIN,
                maximum=_INT32_MAX,
            )
        )
        y = (
            None
            if statement.y is None
            else self._integer(
                statement.y,
                environment,
                "Y 坐标",
                statement.position,
                minimum=_INT32_MIN,
                maximum=_INT32_MAX,
            )
        )
        return MouseStatement(
            "click",
            statement.position,
            button=statement.button,
            x=x,
            y=y,
            last_ms=last,
            count=count,
            interval_ms=interval,
            after_ms=after,
        )

    def _resolve_key(
        self,
        statement: KeyStatement,
        environment: Mapping[str, float],
    ) -> KeyStatement:
        last = (
            DEFAULT_KEY_HOLD_MS
            if statement.mode != "tap"
            else self._duration(
                statement.last_ms,
                environment,
                "按键保持时间 last",
                statement.position,
                minimum=MIN_KEY_HOLD_MS,
                maximum=min(MAX_KEY_HOLD_MS, self.limits.max_wait_ms),
            )
        )
        return KeyStatement(
            statement.key,
            statement.position,
            mode=statement.mode,
            last_ms=last,
        )

    def _resolve_hotkey(
        self,
        statement: HotkeyStatement,
        environment: Mapping[str, float],
    ) -> HotkeyStatement:
        return HotkeyStatement(
            statement.keys,
            statement.position,
            self._duration(
                statement.last_ms,
                environment,
                "组合键保持时间 last",
                statement.position,
                minimum=MIN_KEY_HOLD_MS,
                maximum=min(MAX_KEY_HOLD_MS, self.limits.max_wait_ms),
            ),
        )

    def _compile_repeat(
        self,
        statement: RepeatStatement,
        *,
        loop_depth: int,
        environment: Mapping[str, float],
        call_stack: tuple[str, ...],
    ) -> _Flow:
        count = self._integer(
            statement.count,
            environment,
            "重复次数",
            statement.position,
            minimum=1,
            maximum=self.limits.max_repeat_count,
        )
        body = self.compile_block(
            statement.body,
            loop_depth=loop_depth + 1,
            environment=environment,
            call_stack=call_stack,
        )
        if body.kind is _FlowKind.BREAK:
            # An unconditional break exits the repeat during its first pass.
            return _Flow(_FlowKind.NORMAL, body.prefix)
        if body.kind is _FlowKind.INFINITE:
            return _Flow(
                _FlowKind.INFINITE,
                body.prefix,
                body.cycle,
                body.position or statement.position,
            )

        self._guard_repeated_expansion(
            body.prefix,
            count,
            statement.position,
        )
        return _Flow(_FlowKind.NORMAL, body.prefix * count)

    def _guard_repeated_expansion(
        self,
        primitives: Sequence[_Primitive],
        count: int,
        position: SourcePosition,
    ) -> None:
        primitive_count = len(primitives) * count
        action_count = self._action_weight(primitives) * count
        if (
            primitive_count > self.limits.max_expanded_actions
            or action_count > self.limits.max_expanded_actions
        ):
            raise MacroCompileError(
                f"展开后的动作不能超过 {self.limits.max_expanded_actions} 个",
                position.line,
                position.column,
            )

    def _guard_extension(
        self,
        existing: Sequence[_Primitive],
        additions: Sequence[_Primitive],
        position: SourcePosition,
    ) -> None:
        primitive_count = len(existing) + len(additions)
        action_count = self._action_weight(existing) + self._action_weight(additions)
        if (
            primitive_count > self.limits.max_expanded_actions
            or action_count > self.limits.max_expanded_actions
        ):
            raise MacroCompileError(
                f"展开后的动作不能超过 {self.limits.max_expanded_actions} 个",
                position.line,
                position.column,
            )

    @staticmethod
    def _action_weight(primitives: Sequence[_Primitive]) -> int:
        result = 0
        for primitive in primitives:
            statement = primitive.statement
            if primitive.kind is _PrimitiveKind.WAIT:
                continue
            if primitive.kind is _PrimitiveKind.MOUSE:
                assert isinstance(statement, MouseStatement)
                result += (
                    1
                    if statement.operation in {"move", "scroll"}
                    else 2 * statement.count
                )
                continue
            if isinstance(statement, HotkeyStatement):
                result += 2 * len(statement.keys)
            elif statement.mode == "tap":
                result += 2
            else:
                result += 1
        return result

    @staticmethod
    def _reject_unreachable(statements: Sequence[Statement], index: int) -> None:
        if index + 1 >= len(statements):
            return
        position = statements[index + 1].position
        raise MacroCompileError(
            "无限循环后面的命令永远不会执行；请移动命令或加入可到达的 break",
            position.line,
            position.column,
        )

    def lower(
        self, primitives: Sequence[_Primitive]
    ) -> tuple[tuple[CompiledAction, ...], float, tuple[float, ...]]:
        actions: list[CompiledAction] = []
        pending_wait = 0.0
        pending_random_offsets: list[float] = []
        for primitive in primitives:
            statement = primitive.statement
            if primitive.kind is _PrimitiveKind.WAIT:
                assert isinstance(statement, NextStatement)
                pending_wait += statement.duration_ms
                if statement.random_offset_ms > 0:
                    pending_random_offsets.append(statement.random_offset_ms)
                if (
                    pending_wait + sum(pending_random_offsets)
                    > self.limits.max_wait_ms
                ):
                    raise MacroCompileError(
                        f"连续等待不能超过 {self.limits.max_wait_ms:g} 毫秒"
                        "（按随机偏移的最大结果计算）",
                        statement.position.line,
                        statement.position.column,
                    )
                continue
            if primitive.kind is _PrimitiveKind.MOUSE:
                assert isinstance(statement, MouseStatement)
                if statement.operation == "move":
                    self._claim_expanded_actions(1, statement.position)
                    actions.append(
                        _action(
                            "mouse_move",
                            pending_wait,
                            {"x": statement.x, "y": statement.y},
                            delay_random_offsets_ms=tuple(pending_random_offsets),
                        )
                    )
                    pending_wait = 0.0
                    pending_random_offsets.clear()
                elif statement.operation == "scroll":
                    self._claim_expanded_actions(1, statement.position)
                    params: dict[str, object] = {
                        "dx": statement.dx,
                        "dy": statement.dy,
                    }
                    if statement.x is not None and statement.y is not None:
                        params.update({"x": statement.x, "y": statement.y})
                    actions.append(
                        _action(
                            "mouse_scroll",
                            pending_wait,
                            params,
                            delay_random_offsets_ms=tuple(
                                pending_random_offsets
                            ),
                        )
                    )
                    pending_wait = 0.0
                    pending_random_offsets.clear()
                else:
                    generated_count = 2 * statement.count
                    self._claim_expanded_actions(
                        generated_count,
                        statement.position,
                    )
                    press_params: dict[str, object] = {
                        "button": statement.button,
                        "pressed": True,
                    }
                    if statement.x is not None and statement.y is not None:
                        press_params.update({"x": statement.x, "y": statement.y})
                    for click_index in range(statement.count):
                        actions.extend(
                            (
                                _action(
                                    "mouse_button",
                                    (
                                        pending_wait
                                        if click_index == 0
                                        else statement.interval_ms
                                    ),
                                    press_params,
                                    delay_random_offsets_ms=(
                                        tuple(pending_random_offsets)
                                        if click_index == 0
                                        else ()
                                    ),
                                ),
                                _action(
                                    "mouse_button",
                                    statement.last_ms,
                                    {
                                        "button": statement.button,
                                        "pressed": False,
                                    },
                                ),
                            )
                        )
                    pending_wait = statement.after_ms
                    pending_random_offsets.clear()
            else:
                assert primitive.kind is _PrimitiveKind.KEY
                if isinstance(statement, HotkeyStatement):
                    self._claim_expanded_actions(
                        2 * len(statement.keys),
                        statement.position,
                    )
                    for index, key in enumerate(statement.keys):
                        actions.append(
                            _action(
                                "key_down",
                                pending_wait if index == 0 else 0.0,
                                {"key": key},
                                delay_random_offsets_ms=(
                                    tuple(pending_random_offsets)
                                    if index == 0
                                    else ()
                                ),
                            )
                        )
                    for index, key in enumerate(reversed(statement.keys)):
                        actions.append(
                            _action(
                                "key_up",
                                statement.last_ms if index == 0 else 0.0,
                                {"key": key},
                            )
                        )
                else:
                    assert isinstance(statement, KeyStatement)
                    action_count = 2 if statement.mode == "tap" else 1
                    self._claim_expanded_actions(
                        action_count,
                        statement.position,
                    )
                    actions.append(
                        _action(
                            "key_down" if statement.mode != "up" else "key_up",
                            pending_wait,
                            {"key": statement.key},
                            delay_random_offsets_ms=tuple(pending_random_offsets),
                        )
                    )
                    if statement.mode == "tap":
                        actions.append(
                            _action(
                                "key_up",
                                statement.last_ms,
                                {"key": statement.key},
                            )
                        )
                pending_wait = 0.0
                pending_random_offsets.clear()
        return tuple(actions), pending_wait, tuple(pending_random_offsets)

    def _claim_expanded_actions(
        self, count: int, position: SourcePosition
    ) -> None:
        # Preflight before extending ``actions`` so a huge click count cannot
        # allocate an oversized plan and only fail afterwards.
        if self.expanded_actions + count > self.limits.max_expanded_actions:
            raise MacroCompileError(
                f"展开后的动作不能超过 {self.limits.max_expanded_actions} 个",
                position.line,
                position.column,
            )
        self.expanded_actions += count


def _action(
    action_type: str,
    delay_ms: float,
    params: Mapping[str, object],
    *,
    delay_random_offsets_ms: tuple[float, ...] = (),
) -> CompiledAction:
    return CompiledAction(
        type=action_type,
        delay_ms=delay_ms,
        params=params,
        delay_random_offsets_ms=delay_random_offsets_ms,
    )


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_key(token: _Token) -> str:
    value = token.text
    if len(value) == 1 and value not in {"{", "}"}:
        return value
    lowered = value.casefold()
    lowered = lowered.removeprefix("key.")
    vk_match = _VK_RE.fullmatch(lowered)
    if vk_match is not None:
        try:
            vk_code = int(vk_match.group(1))
        except ValueError:
            raise MacroSyntaxError(
                "虚拟键码必须是 0 到 65535 之间的整数",
                token.line,
                token.column,
            ) from None
        if 0 <= vk_code <= 0xFFFF:
            return f"vk:{vk_code}"
        raise MacroSyntaxError(
            "虚拟键码必须是 0 到 65535 之间的整数",
            token.line,
            token.column,
        )
    if lowered in _SPECIAL_KEYS:
        return lowered
    raise MacroSyntaxError(
        f"无法识别按键 {value!r}；普通字符只能写一个，特殊键可写 space、enter、f1 等",
        token.line,
        token.column,
    )


def _tokenize(source: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    index = 0
    line = 1
    column = 1
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\r":
            if index + 1 < length and source[index + 1] == "\n":
                index += 1
            tokens.append(_Token(_TokenKind.NEWLINE, "\n", line, column))
            index += 1
            line += 1
            column = 1
            continue
        if char == "\n":
            tokens.append(_Token(_TokenKind.NEWLINE, "\n", line, column))
            index += 1
            line += 1
            column = 1
            continue
        if char in " \t":
            index += 1
            column += 4 if char == "\t" else 1
            continue
        if char == "#" or source.startswith("//", index):
            while index < length and source[index] not in "\r\n":
                index += 1
                column += 1
            continue
        delimiters = {
            "{": _TokenKind.LBRACE,
            "}": _TokenKind.RBRACE,
            "[": _TokenKind.LBRACKET,
            "]": _TokenKind.RBRACKET,
            ":": _TokenKind.COLON,
            ",": _TokenKind.COMMA,
            "(": _TokenKind.LPAREN,
            ")": _TokenKind.RPAREN,
            "=": _TokenKind.EQUAL,
            "+": _TokenKind.PLUS,
            "-": _TokenKind.MINUS,
            "*": _TokenKind.STAR,
            "%": _TokenKind.PERCENT,
        }
        if char in delimiters:
            tokens.append(_Token(delimiters[char], char, line, column))
            index += 1
            column += 1
            continue
        if char == "/":
            tokens.append(_Token(_TokenKind.SLASH, char, line, column))
            index += 1
            column += 1
            continue
        start = index
        start_column = column
        while index < length:
            current = source[index]
            if current.isspace() or current in "{}[]:,()=+-*%#/":
                break
            if source.startswith("//", index):
                break
            index += 1
            column += 1
        if start == index:
            # This only guards future delimiter additions.
            raise MacroSyntaxError(f"无法识别字符 {char!r}", line, column)
        tokens.append(
            _Token(_TokenKind.WORD, source[start:index], line, start_column)
        )
    tokens.append(_Token(_TokenKind.EOF, "", line, column))
    return tuple(tokens)


class MacroRunner:
    """Run a compiled macro on a coordinator thread using Player for input.

    ``while true`` always remains interruptible.  Independent execution caps
    stop a forgotten busy loop even when the caller never presses Esc.
    """

    def __init__(
        self,
        macro: str | MacroProgram | CompiledMacro | None = None,
        *,
        controller: InputController | None = None,
        controller_factory: ControllerFactory | None = None,
        state_manager: StateManager | None = None,
        on_event: PlaybackCallback | None = None,
        injection_guard: (
            Callable[[], AbstractContextManager[Any]]
            | AbstractContextManager[Any]
            | Any
            | None
        ) = None,
        random_source: RandomSource | None = None,
        limits: MacroLimits | None = None,
        thread_name: str = "WinActionRecorder-Macro",
    ) -> None:
        if controller is not None and controller_factory is not None:
            raise ValueError("controller 和 controller_factory 不能同时提供")
        if random_source is not None and not callable(random_source):
            raise TypeError("random_source 必须是可调用对象或 None")
        self._default_macro = macro
        self._limits = limits or MacroLimits()
        self._random_source = Random().uniform if random_source is None else random_source
        # MacroRunner owns the shared application-state lease for the complete
        # program.  The delegated Player deliberately uses a private manager,
        # otherwise its finite-prefix ``finished`` transition would publish
        # STOPPED before a following infinite cycle (or tail wait) begins.
        self.state_manager = state_manager or StateManager()
        self._player_state_manager = StateManager()
        self._callbacks: list[PlaybackCallback] = []
        if on_event is not None:
            self._callbacks.append(on_event)
        self._player = Player(
            controller=controller,
            controller_factory=controller_factory,
            state_manager=self._player_state_manager,
            on_event=self._on_player_event,
            injection_guard=injection_guard,
            random_source=self._random_source,
            thread_name=f"{thread_name}-Player",
        )
        self._thread_name = thread_name
        self._condition = threading.Condition(threading.RLock())
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result: PlaybackResult | None = None
        self._closed = False
        self._paused = False
        self._action_count = 0
        self._limit_error: str | None = None
        self._finishing = False
        self._active_limits = self._limits
        self._active_is_infinite = False
        self._active_total_actions = 0

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        with self._condition:
            return self._paused and not self._stop_event.is_set()

    @property
    def last_result(self) -> PlaybackResult | None:
        with self._condition:
            return self._last_result

    def subscribe(self, callback: PlaybackCallback) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("宏事件订阅者必须可调用")
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
        macro: str | MacroProgram | CompiledMacro | None = None,
        *,
        daemon: bool = True,
    ) -> threading.Thread:
        plan = self._resolve_macro(macro)
        start_gate = threading.Event()
        abort_start = threading.Event()
        with self._condition:
            if self._closed:
                raise MacroRunError("宏运行器已经关闭")
            if self._thread is not None and self._thread.is_alive():
                raise MacroRunError("宏脚本已经在运行")
            self._stop_event.clear()
            self._last_result = None
            self._paused = False
            self._action_count = 0
            self._limit_error = None
            self._finishing = False
            self._active_limits = plan.limits
            self._active_is_infinite = plan.is_infinite
            self._active_total_actions = len(plan.prefix_actions)
            thread = threading.Thread(
                target=self._run_after_start_gate,
                args=(start_gate, abort_start, plan),
                name=self._thread_name,
                daemon=daemon,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:
                self._thread = None
                raise MacroRunError(f"无法启动宏脚本线程：{exc}") from exc
        try:
            self.state_manager.transition(
                AppState.PLAYING,
                message="准备运行宏脚本",
                current_loop=0,
                total_loops=None if plan.is_infinite else 1,
                current_action=0,
                total_actions=(
                    len(plan.prefix_actions) + len(plan.loop_actions or ())
                ),
                progress=0.0,
                expected_states=(AppState.IDLE, AppState.STOPPED, AppState.ERROR),
            )
        except Exception:
            abort_start.set()
            start_gate.set()
            thread.join(1.0)
            with self._condition:
                if self._thread is thread:
                    self._thread = None
                self._paused = False
                self._stop_event.clear()
            raise
        start_gate.set()
        return thread

    def play_blocking(
        self, macro: str | MacroProgram | CompiledMacro | None = None
    ) -> PlaybackResult:
        self.start(macro, daemon=False)
        result = self.join()
        if result is None:  # pragma: no cover - no timeout was supplied
            raise MacroRunError("宏脚本线程未能结束")
        return result

    def join(self, timeout: float | None = None) -> PlaybackResult | None:
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
            raise RuntimeError("宏脚本线程不能等待自身结束")
        thread.join(None if timeout is None else float(timeout))
        return None if thread.is_alive() else self.last_result

    wait = join

    def pause(self) -> bool:
        with self._condition:
            if (
                self._thread is None
                or not self._thread.is_alive()
                or self._stop_event.is_set()
                or self._paused
            ):
                return False
            self._paused = True
            self._condition.notify_all()
            self.state_manager.transition(AppState.PAUSED, message="宏脚本已暂停")
        # Player.pause is intentionally non-blocking; if the runner is in its
        # own wait phase it simply returns False and the outer pause still works.
        self._player.pause()
        return True

    def resume(self) -> bool:
        with self._condition:
            if (
                self._thread is None
                or not self._thread.is_alive()
                or self._stop_event.is_set()
                or not self._paused
            ):
                return False
            self._paused = False
            self._condition.notify_all()
            self.state_manager.transition(AppState.PLAYING, message="继续运行宏脚本")
        self._player.resume()
        return True

    def toggle_pause(self) -> bool:
        return self.resume() if self.is_paused else self.pause()

    def stop(self, *, wait: bool = False, timeout: float | None = None) -> bool:
        running = self.is_running
        self._stop_event.set()
        with self._condition:
            self._paused = False
            self._condition.notify_all()
        if running:
            self.state_manager.update_progress(message="正在停止宏脚本")
        self._player.stop()
        if wait and running:
            self.join(timeout)
        return running

    def emergency_stop(
        self, *, wait: bool = False, timeout: float | None = None
    ) -> bool:
        running = self.is_running
        self._stop_event.set()
        with self._condition:
            self._paused = False
            self._condition.notify_all()
        if running:
            self.state_manager.update_progress(message="正在紧急停止宏脚本")
        self._player.emergency_stop()
        if wait and running:
            self.join(timeout)
        return running

    stop_from_escape = emergency_stop

    def close(self, timeout: float | None = 5.0) -> PlaybackResult | None:
        with self._condition:
            self._closed = True
        self.stop(wait=True, timeout=timeout)
        self._player.close(timeout=timeout)
        return self.last_result

    def _resolve_macro(
        self, macro: str | MacroProgram | CompiledMacro | None
    ) -> CompiledMacro:
        selected = self._default_macro if macro is None else macro
        if selected is None:
            raise MacroRunError("没有提供要运行的宏脚本")
        if isinstance(selected, CompiledMacro):
            return selected
        return compile_macro(selected, limits=self._limits)

    def _run_after_start_gate(
        self,
        start_gate: threading.Event,
        abort_start: threading.Event,
        plan: CompiledMacro,
    ) -> None:
        start_gate.wait()
        if abort_start.is_set():
            return
        self._run(plan)

    def _run(self, plan: CompiledMacro) -> None:
        results: list[PlaybackResult] = []
        self._emit_macro_event("started", "宏脚本任务已启动")
        timer = threading.Timer(
            plan.limits.max_runtime_seconds, self._runtime_limit_reached
        )
        timer.daemon = True
        try:
            timer.start()
            if plan.prefix_actions and self._wait_until_active():
                results.append(
                    self._player.play_blocking(
                        plan.prefix_actions,
                        PlaybackSettings(countdown_seconds=0),
                    )
                )
            if self._phase_failed_or_stopped(results):
                return
            if plan.prefix_tail_wait_ms > 0:
                prefix_tail_wait = sample_randomized_delay_ms(
                    plan.prefix_tail_wait_ms,
                    plan.prefix_tail_random_offsets_ms,
                    self._random_source,
                )
                if not self._wait_ms(prefix_tail_wait):
                    return
            if plan.loop_actions is None:
                return
            if plan.loop_actions:
                if not self._wait_until_active():
                    return
                results.append(
                    self._player.play_blocking(
                        plan.loop_actions,
                        PlaybackSettings(
                            countdown_seconds=0,
                            infinite=True,
                            loop_interval_ms=plan.loop_interval_ms,
                            loop_interval_random_offsets_ms=(
                                plan.loop_interval_random_offsets_ms
                            ),
                        ),
                    )
                )
            else:
                while not self._stop_event.is_set():
                    loop_wait = sample_randomized_delay_ms(
                        plan.loop_interval_ms,
                        plan.loop_interval_random_offsets_ms,
                        self._random_source,
                    )
                    if not self._wait_ms(loop_wait):
                        break
        except Exception as exc:  # noqa: BLE001 - sanitize worker boundary
            results.append(
                PlaybackResult(
                    PlaybackOutcome.FAILED,
                    loops_completed=0,
                    actions_executed=0,
                    error=f"宏脚本运行失败：{exc}",
                )
            )
        finally:
            timer.cancel()
            with self._condition:
                self._finishing = True
            result = self._combine_results(results)
            with self._condition:
                self._last_result = result
                self._paused = False
                self._condition.notify_all()
            state = (
                AppState.ERROR
                if result.outcome is PlaybackOutcome.FAILED
                else AppState.STOPPED
            )
            message = result.error or (
                "宏脚本运行完成"
                if result.outcome is PlaybackOutcome.COMPLETED
                else "宏脚本已停止"
            )
            # This is the only place that releases the outer state lease, and
            # it runs only after Player.play_blocking returned from release_all.
            self.state_manager.transition(state, message=message)
            self._emit_macro_event("finished", message, error=result.error)

    def _wait_ms(self, milliseconds: float) -> bool:
        remaining = milliseconds / 1000.0
        while remaining > 0:
            with self._condition:
                if self._stop_event.is_set():
                    return False
                if self._paused:
                    self._condition.wait()
                    continue
                started = monotonic()
                self._condition.wait(timeout=remaining)
                if self._stop_event.is_set():
                    return False
                remaining = max(0.0, remaining - (monotonic() - started))
        return not self._stop_event.is_set()

    def _wait_until_active(self) -> bool:
        with self._condition:
            while self._paused and not self._stop_event.is_set():
                self._condition.wait()
            return not self._stop_event.is_set()

    @staticmethod
    def _phase_failed_or_stopped(results: Sequence[PlaybackResult]) -> bool:
        return bool(results) and results[-1].outcome is not PlaybackOutcome.COMPLETED

    def _on_player_event(self, event: PlaybackEvent) -> None:
        if event.kind == "action_executed":
            self._action_count += 1
            action_limit = self._active_limits.max_executed_actions
            if self._action_count >= action_limit and (
                self._active_is_infinite
                or self._active_total_actions > action_limit
            ):
                self._limit_error = (
                    f"为防止脚本失控，已在 {self._action_count} 个动作后自动停止"
                )
                self._stop_event.set()
                self._player.emergency_stop()
        if event.kind in {
            "started",
            "loop_started",
            "action_executed",
            "loop_interval",
        } and not self._stop_event.is_set():
            self.state_manager.update_progress(
                message=(
                    f"宏脚本已执行 {self._action_count} 个动作"
                    if self._action_count
                    else "正在运行宏脚本"
                )
            )
        phase_kind = {
            "started": "phase_started",
            "finished": "phase_finished",
        }.get(event.kind, event.kind)
        outer_event = PlaybackEvent(
            kind=phase_kind,
            state=self.state_manager.state,
            message=event.message,
            loop_index=event.loop_index,
            total_loops=event.total_loops,
            action_index=event.action_index,
            total_actions=event.total_actions,
            countdown_remaining=event.countdown_remaining,
            error=event.error,
            timestamp=event.timestamp,
        )
        self._notify_callbacks(outer_event)

    def _emit_macro_event(
        self, kind: str, message: str, *, error: str | None = None
    ) -> None:
        snapshot = self.state_manager.snapshot()
        self._notify_callbacks(
            PlaybackEvent(
                kind=kind,
                state=snapshot.state,
                message=message,
                total_loops=snapshot.total_loops,
                total_actions=snapshot.total_actions,
                error=error,
            )
        )

    def _notify_callbacks(self, event: PlaybackEvent) -> None:
        with self._condition:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                # Match Player's defensive callback boundary.
                logger.exception("宏脚本事件回调执行失败")

    def _runtime_limit_reached(self) -> None:
        with self._condition:
            if (
                self._thread is None
                or not self._thread.is_alive()
                or self._last_result is not None
                or self._finishing
            ):
                return
            self._limit_error = (
                "为防止脚本失控，已在 "
                f"{self._active_limits.max_runtime_seconds:g} 秒后自动停止"
            )
            self._stop_event.set()
            self._paused = False
            self._condition.notify_all()
        self.state_manager.update_progress(message="宏脚本达到安全时限，正在停止")
        self._player.emergency_stop()

    def _combine_results(self, results: Sequence[PlaybackResult]) -> PlaybackResult:
        cleanup_errors = tuple(
            error for result in results for error in result.cleanup_errors
        )
        actions = sum(result.actions_executed for result in results)
        loops = sum(result.loops_completed for result in results)
        failed = next(
            (result for result in results if result.outcome is PlaybackOutcome.FAILED),
            None,
        )
        if failed is not None:
            return PlaybackResult(
                PlaybackOutcome.FAILED,
                loops,
                actions,
                error=failed.error,
                cleanup_errors=cleanup_errors,
            )
        if self._limit_error is not None:
            return PlaybackResult(
                PlaybackOutcome.FAILED,
                loops,
                actions,
                error=self._limit_error,
                cleanup_errors=cleanup_errors,
            )
        stopped = self._stop_event.is_set() or any(
            result.outcome is PlaybackOutcome.STOPPED for result in results
        )
        return PlaybackResult(
            PlaybackOutcome.STOPPED if stopped else PlaybackOutcome.COMPLETED,
            loops,
            actions,
            cleanup_errors=cleanup_errors,
        )


__all__ = [
    "DEFAULT_CLICK_HOLD_MS",
    "DEFAULT_CLICK_INTERVAL_MS",
    "DEFAULT_KEY_HOLD_MS",
    "DEFAULT_NEXT_MS",
    "MAX_CLICK_HOLD_MS",
    "MAX_HOTKEY_KEYS",
    "MAX_KEY_HOLD_MS",
    "MIN_CLICK_HOLD_MS",
    "MIN_KEY_HOLD_MS",
    "BreakStatement",
    "CompiledAction",
    "CompiledMacro",
    "HotkeyStatement",
    "KeyStatement",
    "MacroCompileError",
    "MacroLanguageError",
    "MacroLimits",
    "MacroProgram",
    "MacroRunError",
    "MacroRunner",
    "MacroSyntaxError",
    "MouseStatement",
    "NextStatement",
    "RepeatStatement",
    "SourcePosition",
    "Statement",
    "WhileTrueStatement",
    "compile_macro",
    "parse_macro",
]
