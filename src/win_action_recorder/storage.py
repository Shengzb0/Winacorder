"""UTF-8 JSON 动作脚本的安全读写。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import (
    CURRENT_FORMAT_VERSION,
    DEFAULT_APP_VERSION,
    Action,
    ActionScript,
    ModelValidationError,
    UnsupportedFormatVersionError,
    recalculate_offsets,
    utc_now_iso,
)
from .screen import ScreenConfig, ScreenConfigError, normalize_screen_config

LOGGER = logging.getLogger(__name__)
MAX_SCRIPT_SIZE_BYTES = 100 * 1024 * 1024


class StorageError(Exception):
    """脚本存储操作的可恢复错误，消息可以直接展示给用户。"""


class ScriptNotFoundError(StorageError, FileNotFoundError):
    """用户选择的脚本不存在。"""


class ScriptReadError(StorageError):
    """脚本文件无法读取。"""


class ScriptWriteError(StorageError):
    """脚本文件无法保存。"""


class ScriptFormatError(StorageError, ValueError):
    """JSON 或脚本数据结构损坏。"""


class UnsupportedScriptVersionError(ScriptFormatError):
    """脚本格式版本高于当前版本或缺少迁移链。"""


class _DuplicateJsonKeyError(ValueError):
    pass


class _InvalidJsonConstantError(ValueError):
    pass


def create_script(
    actions: Sequence[Action],
    *,
    screen_config: ScreenConfig | Mapping[str, Any],
    recording_settings: Mapping[str, Any] | None = None,
    app_version: str = DEFAULT_APP_VERSION,
    created_at: str | None = None,
) -> ActionScript:
    """根据动作和元数据创建一份当前版本脚本。

    动作会被复制并重算偏移，调用方的列表及动作不会被修改。
    """

    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ScriptFormatError("actions 必须是动作数组")
    copied_actions: list[Action] = []
    for index, action in enumerate(actions):
        if not isinstance(action, Action):
            raise ScriptFormatError(f"第 {index + 1} 个动作不是 Action 对象")
        copied_actions.append(action.clone())
    try:
        recalculate_offsets(copied_actions)
        normalized_screen = normalize_screen_config(screen_config)
        return ActionScript(
            format_version=CURRENT_FORMAT_VERSION,
            app_version=app_version,
            created_at=created_at or utc_now_iso(),
            screen_config=normalized_screen,
            recording_settings=dict(
                recording_settings
                if recording_settings is not None
                else {"mouse_move_sample_interval_ms": 20}
            ),
            actions=copied_actions,
        )
    except (ModelValidationError, ScreenConfigError, TypeError, ValueError) as exc:
        if isinstance(exc, ScriptFormatError):
            raise
        raise ScriptFormatError(f"无法创建动作脚本：{exc}") from exc


def save_script(
    path: str | os.PathLike[str],
    script: ActionScript | Mapping[str, Any] | Sequence[Action],
    *,
    screen_config: ScreenConfig | Mapping[str, Any] | None = None,
    recording_settings: Mapping[str, Any] | None = None,
    app_version: str = DEFAULT_APP_VERSION,
    created_at: str | None = None,
) -> Path:
    """以 UTF-8、两空格缩进和末尾换行原子保存脚本。

    ``script`` 可以是完整 :class:`ActionScript`、不可信顶层映射，或动作
    数组。传入动作数组时必须同时提供 ``screen_config``。
    """

    target = _coerce_path(path)
    document = _coerce_document_for_save(
        script,
        screen_config=screen_config,
        recording_settings=recording_settings,
        app_version=app_version,
        created_at=created_at,
    )
    try:
        payload = json.dumps(
            document.to_dict(),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError, ModelValidationError) as exc:
        raise ScriptFormatError(f"脚本数据无法序列化：{exc}") from exc

    temporary_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except (OSError, UnicodeError) as exc:
        raise ScriptWriteError(f"无法保存脚本文件“{target}”：{exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                # 仅清理本次写入创建的临时文件；失败不覆盖原始保存错误。
                LOGGER.warning("无法清理脚本保存临时文件", exc_info=True)

    LOGGER.info("动作脚本已保存：动作数=%d", len(document.actions))
    return target


def load_script(path: str | os.PathLike[str]) -> ActionScript:
    """读取并严格校验一个动作脚本。

    文件系统、UTF-8、JSON、版本、顶层元数据、显示器配置及每个动作的错
    误分别转成稳定的中文异常，调用端无需解析底层异常文字。
    """

    source = _coerce_path(path)
    try:
        if not source.exists():
            raise ScriptNotFoundError(f"脚本文件不存在：“{source}”")
        if not source.is_file():
            raise ScriptReadError(f"所选路径不是脚本文件：“{source}”")
        size = source.stat().st_size
        if size > MAX_SCRIPT_SIZE_BYTES:
            raise ScriptReadError(
                f"脚本文件过大（{size} 字节），最大支持 "
                f"{MAX_SCRIPT_SIZE_BYTES} 字节"
            )
        text = source.read_text(encoding="utf-8")
    except (ScriptNotFoundError, ScriptReadError):
        raise
    except UnicodeDecodeError as exc:
        raise ScriptReadError(
            f"无法读取脚本文件“{source}”：文件不是有效的 UTF-8 文本"
        ) from exc
    except OSError as exc:
        raise ScriptReadError(f"无法读取脚本文件“{source}”：{exc}") from exc

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ScriptFormatError(
            f"JSON 格式损坏：第 {exc.lineno} 行、第 {exc.colno} 列附近有错误"
        ) from exc
    except _DuplicateJsonKeyError as exc:
        raise ScriptFormatError(f"JSON 格式损坏：{exc}") from exc
    except _InvalidJsonConstantError as exc:
        raise ScriptFormatError(f"JSON 格式损坏：{exc}") from exc

    if not isinstance(raw, Mapping):
        raise ScriptFormatError("JSON 顶层必须是对象")
    try:
        document = ActionScript.from_dict(raw)
        document.screen_config = normalize_screen_config(document.screen_config)
        document.validate()
    except UnsupportedFormatVersionError as exc:
        raise UnsupportedScriptVersionError(str(exc)) from exc
    except (ModelValidationError, ScreenConfigError) as exc:
        raise ScriptFormatError(f"脚本数据无效：{exc}") from exc

    LOGGER.info("动作脚本已加载：动作数=%d", len(document.actions))
    return document


def save_actions(
    path: str | os.PathLike[str],
    actions: Sequence[Action],
    *,
    screen_config: ScreenConfig | Mapping[str, Any],
    recording_settings: Mapping[str, Any] | None = None,
    app_version: str = DEFAULT_APP_VERSION,
) -> Path:
    """保存动作数组的便捷入口。"""

    return save_script(
        path,
        actions,
        screen_config=screen_config,
        recording_settings=recording_settings,
        app_version=app_version,
    )


def load_actions(path: str | os.PathLike[str]) -> list[Action]:
    """只读取动作数组的便捷入口，返回与文档独立的动作副本。"""

    return [action.clone() for action in load_script(path).actions]


def _coerce_document_for_save(
    value: ActionScript | Mapping[str, Any] | Sequence[Action],
    *,
    screen_config: ScreenConfig | Mapping[str, Any] | None,
    recording_settings: Mapping[str, Any] | None,
    app_version: str,
    created_at: str | None,
) -> ActionScript:
    try:
        if isinstance(value, ActionScript):
            # 用新实例做边界校验，避免规范化时修改调用方对象。
            return ActionScript(
                format_version=value.format_version,
                app_version=value.app_version,
                created_at=value.created_at,
                screen_config=normalize_screen_config(value.screen_config),
                recording_settings=value.recording_settings,
                actions=[action.clone() for action in value.actions],
            )
        if isinstance(value, Mapping):
            document = ActionScript.from_dict(value)
            document.screen_config = normalize_screen_config(document.screen_config)
            document.validate()
            return document
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if screen_config is None:
                raise ScriptFormatError(
                    "保存动作数组时必须提供 screen_config 显示器配置"
                )
            return create_script(
                value,
                screen_config=screen_config,
                recording_settings=recording_settings,
                app_version=app_version,
                created_at=created_at,
            )
    except UnsupportedFormatVersionError as exc:
        raise UnsupportedScriptVersionError(str(exc)) from exc
    except ScriptFormatError:
        raise
    except (ModelValidationError, ScreenConfigError, TypeError, ValueError) as exc:
        raise ScriptFormatError(f"脚本数据无效：{exc}") from exc
    raise ScriptFormatError("script 必须是 ActionScript、顶层对象或动作数组")


def _coerce_path(value: str | os.PathLike[str]) -> Path:
    if isinstance(value, str) and not value.strip():
        raise StorageError("脚本文件路径不能为空")
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError) as exc:
        raise StorageError(f"脚本文件路径无效：{exc}") from exc
    if "\x00" in str(path):
        raise StorageError("脚本文件路径包含无效字符")
    return path


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"字段“{key}”重复出现")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise _InvalidJsonConstantError(f"不允许使用非常规数字 {value}")


# 常用语义别名，保留单一实现。
read_script = load_script
write_script = save_script


__all__ = [
    "MAX_SCRIPT_SIZE_BYTES",
    "ScriptFormatError",
    "ScriptNotFoundError",
    "ScriptReadError",
    "ScriptWriteError",
    "StorageError",
    "UnsupportedScriptVersionError",
    "create_script",
    "load_actions",
    "load_script",
    "read_script",
    "save_actions",
    "save_script",
    "write_script",
]
