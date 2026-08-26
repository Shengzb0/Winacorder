"""Validated, atomic storage for user-authored macro schemes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from .macro_dsl import MacroLimits, compile_macro

SCHEME_FORMAT_VERSION: Final[int] = 1
SCHEME_EXTENSION: Final[str] = ".wacscheme"
MAX_SCHEME_BYTES: Final[int] = 512 * 1024
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FIELDS = frozenset(
    {
        "format_version",
        "id",
        "name",
        "description",
        "source",
        "created_at",
        "updated_at",
    }
)


class SchemeValidationError(ValueError):
    """A scheme or scheme file failed strict validation."""


class SchemeStorageError(OSError):
    """A scheme could not be read or written safely."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class MacroScheme:
    """A named, savable macro source document."""

    id: str
    name: str
    source: str
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    builtin: bool = False
    format_version: int = SCHEME_FORMAT_VERSION

    def __post_init__(self) -> None:
        now = _now_iso()
        if not self.created_at:
            object.__setattr__(self, "created_at", now)
        if not self.updated_at:
            object.__setattr__(self, "updated_at", self.created_at)
        self.validate()

    @classmethod
    def create(
        cls, name: str, source: str, description: str = ""
    ) -> MacroScheme:
        return cls(
            id=f"user-{uuid4().hex}",
            name=name,
            source=source,
            description=description,
        )

    def validate(self, *, validate_source: bool = True) -> None:
        if (
            isinstance(self.format_version, bool)
            or not isinstance(self.format_version, int)
            or self.format_version != SCHEME_FORMAT_VERSION
        ):
            raise SchemeValidationError(
                f"不支持方案格式版本 {self.format_version}"
            )
        if not isinstance(self.id, str) or _ID_RE.fullmatch(self.id) is None:
            raise SchemeValidationError("方案 id 只能包含小写字母、数字、_ 和 -")
        _validate_text(self.name, "方案名称", minimum=1, maximum=80)
        if not self.name.strip():
            raise SchemeValidationError("方案名称不能为空白")
        _validate_text(self.description, "方案说明", minimum=0, maximum=500)
        _validate_text(self.source, "方案脚本", minimum=1, maximum=100_000)
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")
        if not isinstance(self.builtin, bool):
            raise SchemeValidationError("builtin 必须是布尔值")
        if validate_source:
            try:
                compile_macro(self.source)
            except (ValueError, TypeError) as exc:
                raise SchemeValidationError(f"方案脚本无效：{exc}") from exc

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "format_version": self.format_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> MacroScheme:
        if not isinstance(raw, dict):
            raise SchemeValidationError("方案 JSON 顶层必须是对象")
        keys = frozenset(raw)
        if keys != _FIELDS:
            missing = sorted(_FIELDS - keys)
            extra = sorted(keys - _FIELDS)
            details: list[str] = []
            if missing:
                details.append("缺少字段：" + "、".join(missing))
            if extra:
                details.append("未知字段：" + "、".join(extra))
            raise SchemeValidationError("；".join(details))
        try:
            return cls(
                format_version=raw["format_version"],  # type: ignore[arg-type]
                id=raw["id"],  # type: ignore[arg-type]
                name=raw["name"],  # type: ignore[arg-type]
                description=raw["description"],  # type: ignore[arg-type]
                source=raw["source"],  # type: ignore[arg-type]
                created_at=raw["created_at"],  # type: ignore[arg-type]
                updated_at=raw["updated_at"],  # type: ignore[arg-type]
            )
        except SchemeValidationError:
            raise
        except Exception as exc:
            raise SchemeValidationError(f"方案字段类型无效：{exc}") from exc


_BUILTIN_DATA: tuple[tuple[str, str, str, str], ...] = (
    (
        "builtin-gentle-click",
        "温和连点 20 次",
        "左键点击 20 次；每次按住 35 毫秒，释放后等待 120 毫秒再按下。",
        "mouse left[last:35ms, count:20, interval:120ms]\n",
    ),
    (
        "builtin-double-click",
        "双击",
        "每次按住 25 毫秒，两次点击之间等待 80 毫秒。",
        "mouse left[last:25ms, count:2, interval:80ms]\n",
    ),
    (
        "builtin-fixed-right-click",
        "定点右键示例",
        "在示例坐标右键 10 次；运行前请改成自己的目标位置。",
        "mouse right[last:40ms, count:10, interval:500ms] at 800 450\n",
    ),
    (
        "builtin-space-repeat",
        "重复空格键",
        "每秒按一次空格，共十次。",
        "repeat 10 {\n  key space\n  next 1s\n}\n",
    ),
    (
        "builtin-until-stop",
        "持续连点（Esc 停止）",
        "持续左键连点，每次释放后等待 100 毫秒；用 Esc 结束。",
        "while true {\n  mouse left[last:20ms, after:100ms]\n}\n",
    ),
    (
        "builtin-reusable-burst",
        "变量与复用函数",
        "用变量、算术和函数生成三轮按键；可修改次数与间隔后重复调用。",
        (
            "let rounds = 3\n"
            "let gap = 240ms\n\n"
            "function burst(times, delay) {\n"
            "  repeat times {\n"
            "    key f[last:delay / 8]\n"
            "    next delay random delay / 4\n"
            "  }\n"
            "}\n\n"
            "call burst(rounds, gap)\n"
        ),
    ),
)

class MacroSchemeStore:
    """Store ``.wacscheme`` JSON files below one explicitly supplied root."""

    def __init__(
        self, root: str | os.PathLike[str], *, limits: MacroLimits | None = None
    ) -> None:
        self.root = Path(root)
        self.limits = limits or MacroLimits()

    def create(
        self, name: str, source: str, description: str = ""
    ) -> MacroScheme:
        scheme = MacroScheme.create(name, source, description)
        return self.save(scheme)

    def save(self, scheme: MacroScheme) -> MacroScheme:
        if not isinstance(scheme, MacroScheme):
            raise TypeError("scheme 必须是 MacroScheme")
        if scheme.builtin or scheme.id in _BUILTIN_BY_ID:
            raise SchemeValidationError("内置示例不能覆盖；请另存为新方案")
        try:
            compile_macro(scheme.source, limits=self.limits)
        except (ValueError, TypeError) as exc:
            raise SchemeValidationError(f"方案脚本无效：{exc}") from exc
        now = _now_iso()
        saved = replace(scheme, builtin=False, updated_at=now)
        payload = json.dumps(
            saved.to_dict(),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
        encoded = payload.encode("utf-8")
        if len(encoded) > MAX_SCHEME_BYTES:
            raise SchemeValidationError("方案文件过大")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path_for(saved.id)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=".macro-", suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except OSError as exc:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise SchemeStorageError(f"无法保存方案 {saved.name!r}：{exc}") from exc
        return saved

    def load(self, scheme_id: str) -> MacroScheme:
        builtin = _BUILTIN_BY_ID.get(scheme_id)
        if builtin is not None:
            return builtin
        path = self._path_for(scheme_id)
        try:
            size = path.stat().st_size
            if size > MAX_SCHEME_BYTES:
                raise SchemeValidationError("方案文件过大")
            text = path.read_text(encoding="utf-8")
            raw = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except SchemeValidationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SchemeStorageError(f"无法读取方案 {scheme_id!r}：{exc}") from exc
        scheme = MacroScheme.from_dict(raw)
        if scheme.id != scheme_id:
            raise SchemeValidationError("方案文件名与内部 id 不一致")
        return scheme

    def list_schemes(self) -> tuple[MacroScheme, ...]:
        user_schemes: list[MacroScheme] = []
        if self.root.exists():
            try:
                paths = sorted(self.root.glob(f"*{SCHEME_EXTENSION}"))
            except OSError as exc:
                raise SchemeStorageError(f"无法列出方案：{exc}") from exc
            for path in paths:
                user_schemes.append(self.load(path.stem))
        user_schemes.sort(key=lambda scheme: (scheme.name.casefold(), scheme.id))
        return BUILTIN_SCHEMES + tuple(user_schemes)

    def delete(self, scheme_id: str) -> bool:
        if scheme_id in _BUILTIN_BY_ID:
            raise SchemeValidationError("内置示例不能删除")
        path = self._path_for(scheme_id)
        try:
            if not path.exists():
                return False
            path.unlink()
            return True
        except OSError as exc:
            raise SchemeStorageError(f"无法删除方案 {scheme_id!r}：{exc}") from exc

    def _path_for(self, scheme_id: str) -> Path:
        if not isinstance(scheme_id, str) or _ID_RE.fullmatch(scheme_id) is None:
            raise SchemeValidationError("方案 id 无效")
        return self.root / f"{scheme_id}{SCHEME_EXTENSION}"


def _validate_text(
    value: object, label: str, *, minimum: int, maximum: int
) -> None:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        if minimum:
            raise SchemeValidationError(
                f"{label}长度必须在 {minimum} 到 {maximum} 个字符之间"
            )
        raise SchemeValidationError(f"{label}不能超过 {maximum} 个字符")
    if "\x00" in value:
        raise SchemeValidationError(f"{label}包含非法空字符")


def _validate_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise SchemeValidationError(f"{label} 必须是时间字符串")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise SchemeValidationError(f"{label} 不是有效 ISO 8601 时间") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemeValidationError(f"方案 JSON 字段重复：{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise SchemeValidationError(f"方案 JSON 不允许特殊数值：{value}")


BUILTIN_SCHEMES: Final[tuple[MacroScheme, ...]] = tuple(
    MacroScheme(
        id=scheme_id,
        name=name,
        source=source,
        description=description,
        builtin=True,
    )
    for scheme_id, name, description, source in _BUILTIN_DATA
)
_BUILTIN_BY_ID: Final[dict[str, MacroScheme]] = {
    scheme.id: scheme for scheme in BUILTIN_SCHEMES
}


__all__ = [
    "BUILTIN_SCHEMES",
    "MAX_SCHEME_BYTES",
    "SCHEME_EXTENSION",
    "SCHEME_FORMAT_VERSION",
    "MacroScheme",
    "MacroSchemeStore",
    "SchemeStorageError",
    "SchemeValidationError",
]
