"""WinActionRecorder 的动作与脚本文档数据模型。

此模块只处理可信的内存对象和不可信 JSON 之间的边界，不依赖 Qt 或
``pynput``。所有反序列化入口都会进行显式类型、字段及取值校验。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, MutableSequence, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import uuid4

CURRENT_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = frozenset({CURRENT_FORMAT_VERSION})
DEFAULT_APP_VERSION = "0.9.1"


class ModelValidationError(ValueError):
    """数据模型校验失败，消息可以直接展示给中文用户。"""


class UnsupportedFormatVersionError(ModelValidationError):
    """脚本版本无法由当前程序读取。"""


class ActionType(str, Enum):
    """可录制及回放的基础动作类型。"""

    MOUSE_MOVE = "mouse_move"
    MOUSE_BUTTON = "mouse_button"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"


class MouseButton(str, Enum):
    """首版支持的鼠标按钮。"""

    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


_ACTION_FIELDS = frozenset(
    {"id", "type", "offset_ms", "delay_ms", "enabled", "params"}
)
_SCRIPT_FIELDS = frozenset(
    {
        "format_version",
        "app_version",
        "created_at",
        "screen_config",
        "recording_settings",
        "actions",
    }
)
_COORDINATE_MIN = -(2**31)
_COORDINATE_MAX = 2**31 - 1

# ``pynput.keyboard.Key`` 在 Windows 上可能产生的名称。普通字符（包括中文）
# 以单个 Unicode 字符保存，不会被本表限制。
_SPECIAL_KEY_NAMES = frozenset(
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
    }
    | {f"f{number}" for number in range(1, 25)}
)


def utc_now_iso() -> str:
    """返回带时区且便于人工阅读的 UTC ISO 8601 时间。"""

    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass(slots=True)
class Action:
    """单个鼠标或键盘动作。

    ``delay_ms`` 是执行本动作前的等待时间；``offset_ms`` 必须等于从列表
    开头累加到本动作的延迟。动作调整顺序或修改延迟后应调用
    :func:`recalculate_offsets`。
    """

    id: str
    type: ActionType
    offset_ms: int
    delay_ms: int
    enabled: bool
    params: dict[str, Any]

    REQUIRED_FIELDS: ClassVar[frozenset[str]] = _ACTION_FIELDS

    def __post_init__(self) -> None:
        self.type = _coerce_action_type(self.type)
        self.params = _copy_mapping(self.params, "动作参数 params")
        self.validate()

    @classmethod
    def create(
        cls,
        action_type: ActionType | str,
        params: Mapping[str, Any],
        *,
        delay_ms: int = 0,
        offset_ms: int | None = None,
        enabled: bool = True,
        action_id: str | None = None,
    ) -> Action:
        """创建一个带 UUID 的动作，适合录制器和动作编辑器使用。"""

        if offset_ms is None:
            offset_ms = delay_ms
        return cls(
            id=action_id or str(uuid4()),
            type=_coerce_action_type(action_type),
            offset_ms=offset_ms,
            delay_ms=delay_ms,
            enabled=enabled,
            params=dict(params),
        )

    def validate(self) -> None:
        """重新校验当前动作；编辑器修改字段后可显式调用。"""

        _validate_identifier(self.id)
        _require_non_negative_int(self.offset_ms, "动作时间偏移 offset_ms")
        _require_non_negative_int(self.delay_ms, "动作前延迟 delay_ms")
        if self.offset_ms > _COORDINATE_MAX or self.delay_ms > _COORDINATE_MAX:
            raise ModelValidationError(
                f"动作时间不能超过 {_COORDINATE_MAX} 毫秒"
            )
        if not isinstance(self.enabled, bool):
            raise ModelValidationError("动作字段 enabled 必须是布尔值")
        _validate_action_params(self.type, self.params)

    def to_dict(self) -> dict[str, Any]:
        """返回可安全交给 ``json`` 的独立字典。"""

        self.validate()
        return {
            "id": self.id,
            "type": self.type.value,
            "offset_ms": self.offset_ms,
            "delay_ms": self.delay_ms,
            "enabled": self.enabled,
            "params": deepcopy(self.params),
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        format_version: int = CURRENT_FORMAT_VERSION,
        action_index: int | None = None,
    ) -> Action:
        """从不可信映射反序列化并严格校验一个动作。"""

        _ensure_supported_version(format_version)
        label = "动作" if action_index is None else f"第 {action_index + 1} 个动作"
        data = _require_mapping(raw, label)
        _validate_exact_fields(data, _ACTION_FIELDS, label)
        try:
            return cls(
                id=data["id"],
                type=_coerce_action_type(data["type"], label=label),
                offset_ms=data["offset_ms"],
                delay_ms=data["delay_ms"],
                enabled=data["enabled"],
                params=data["params"],
            )
        except ModelValidationError as exc:
            if action_index is None:
                raise
            raise ModelValidationError(f"{label}无效：{exc}") from exc

    def clone(self, **changes: Any) -> Action:
        """复制动作；参数字典不会与原动作共享。"""

        values = {"params": deepcopy(self.params), **changes}
        return replace(self, **values)


@dataclass(slots=True)
class ActionScript:
    """动作脚本 JSON 的顶层模型。"""

    format_version: int = CURRENT_FORMAT_VERSION
    app_version: str = DEFAULT_APP_VERSION
    created_at: str = field(default_factory=utc_now_iso)
    screen_config: dict[str, Any] = field(default_factory=dict)
    recording_settings: dict[str, Any] = field(
        default_factory=lambda: {"mouse_move_sample_interval_ms": 20}
    )
    actions: list[Action] = field(default_factory=list)

    REQUIRED_FIELDS: ClassVar[frozenset[str]] = _SCRIPT_FIELDS

    def __post_init__(self) -> None:
        self.screen_config = _copy_mapping(self.screen_config, "screen_config")
        self.recording_settings = _copy_mapping(
            self.recording_settings, "recording_settings"
        )
        if not isinstance(self.actions, list):
            raise ModelValidationError("顶层字段 actions 必须是数组")
        converted: list[Action] = []
        for index, action in enumerate(self.actions):
            if isinstance(action, Action):
                converted.append(action)
            elif isinstance(action, Mapping):
                converted.append(
                    Action.from_dict(
                        action,
                        format_version=self.format_version,
                        action_index=index,
                    )
                )
            else:
                raise ModelValidationError(f"第 {index + 1} 个动作必须是对象")
        self.actions = converted
        self.validate()

    def validate(self, *, require_consistent_offsets: bool = True) -> None:
        """校验元数据、动作唯一性与时序一致性。"""

        _ensure_supported_version(self.format_version)
        if not isinstance(self.app_version, str) or not self.app_version.strip():
            raise ModelValidationError("顶层字段 app_version 必须是非空字符串")
        if len(self.app_version) > 64:
            raise ModelValidationError("顶层字段 app_version 过长")
        _validate_iso_datetime(self.created_at)
        if not self.screen_config:
            raise ModelValidationError("顶层字段 screen_config 不能为空")
        _validate_json_value(self.screen_config, "screen_config")
        _validate_recording_settings(self.recording_settings)
        validate_action_sequence(
            self.actions, require_consistent_offsets=require_consistent_offsets
        )

    def to_dict(self) -> dict[str, Any]:
        """按稳定字段顺序生成 JSON 兼容字典。"""

        self.validate()
        return {
            "format_version": self.format_version,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "screen_config": deepcopy(self.screen_config),
            "recording_settings": deepcopy(self.recording_settings),
            "actions": [action.to_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ActionScript:
        """从不可信 JSON 顶层对象创建脚本，并经过版本迁移入口。"""

        original = _require_mapping(raw, "JSON 顶层")
        migrated = migrate_script_data(original)
        _validate_exact_fields(migrated, _SCRIPT_FIELDS, "JSON 顶层")
        actions = migrated["actions"]
        if not isinstance(actions, list):
            raise ModelValidationError("顶层字段 actions 必须是数组")
        return cls(
            format_version=migrated["format_version"],
            app_version=migrated["app_version"],
            created_at=migrated["created_at"],
            screen_config=migrated["screen_config"],
            recording_settings=migrated["recording_settings"],
            actions=[
                Action.from_dict(
                    item,
                    format_version=migrated["format_version"],
                    action_index=index,
                )
                for index, item in enumerate(actions)
            ],
        )


# 每个迁移器负责把版本 n 的独立字典升级到 n + 1。首版暂时没有旧格式，
# 但入口是稳定公开 API，新增版本时不需要改动文件读取层。
FormatMigrator = Callable[[dict[str, Any]], dict[str, Any]]
_FORMAT_MIGRATORS: dict[int, FormatMigrator] = {}


def register_format_migration(from_version: int, migrator: FormatMigrator) -> None:
    """注册 ``from_version -> from_version + 1`` 的格式迁移器。"""

    _require_non_negative_int(from_version, "迁移源版本")
    if from_version >= CURRENT_FORMAT_VERSION:
        raise ValueError("只能为早于当前格式的版本注册迁移器")
    if not callable(migrator):
        raise TypeError("迁移器必须可调用")
    _FORMAT_MIGRATORS[from_version] = migrator


def migrate_script_data(raw: Mapping[str, Any]) -> dict[str, Any]:
    """验证版本并返回升级到当前版本的脚本字典副本。

    未提供 ``format_version``、版本类型错误或没有迁移链时都会给出明确的
    中文错误，而不会猜测外部文件格式。
    """

    data = deepcopy(_require_mapping(raw, "JSON 顶层"))
    if "format_version" not in data:
        raise ModelValidationError("JSON 顶层缺少必需字段：format_version")
    version = data["format_version"]
    if not _is_plain_int(version) or version < 0:
        raise ModelValidationError("format_version 必须是非负整数")
    if version > CURRENT_FORMAT_VERSION:
        raise UnsupportedFormatVersionError(
            f"不支持脚本格式版本 {version}；当前程序最高支持版本 "
            f"{CURRENT_FORMAT_VERSION}"
        )
    while version < CURRENT_FORMAT_VERSION:
        migrator = _FORMAT_MIGRATORS.get(version)
        if migrator is None:
            raise UnsupportedFormatVersionError(
                f"不支持脚本格式版本 {version}，缺少到当前版本的兼容迁移"
            )
        try:
            migrated = migrator(deepcopy(data))
        except Exception as exc:
            raise ModelValidationError(
                f"脚本格式从版本 {version} 升级失败：{exc}"
            ) from exc
        data = _copy_mapping(migrated, f"版本 {version} 迁移结果")
        expected = version + 1
        if data.get("format_version") != expected:
            raise ModelValidationError(
                f"版本迁移器必须把 format_version 更新为 {expected}"
            )
        version = expected
    return data


def validate_action_sequence(
    actions: Sequence[Action], *, require_consistent_offsets: bool = True
) -> None:
    """校验动作数组、唯一 ID 以及累计时序。"""

    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ModelValidationError("actions 必须是动作数组")
    seen_ids: set[str] = set()
    expected_offset = 0
    previous_offset = -1
    for index, action in enumerate(actions):
        if not isinstance(action, Action):
            raise ModelValidationError(f"第 {index + 1} 个动作不是 Action 对象")
        action.validate()
        if action.id in seen_ids:
            raise ModelValidationError(f"动作 id 重复：{action.id}")
        seen_ids.add(action.id)
        if action.offset_ms < previous_offset:
            raise ModelValidationError(
                f"第 {index + 1} 个动作的 offset_ms 不能小于前一个动作"
            )
        expected_offset += action.delay_ms
        if require_consistent_offsets and action.offset_ms != expected_offset:
            raise ModelValidationError(
                f"第 {index + 1} 个动作时序不一致：offset_ms 应为 "
                f"{expected_offset}，实际为 {action.offset_ms}"
            )
        previous_offset = action.offset_ms


def recalculate_offsets(actions: Sequence[Action]) -> list[Action]:
    """原地重算全部 ``offset_ms``，同时返回便于链式使用的列表视图。"""

    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ModelValidationError("actions 必须是动作数组")
    offset = 0
    result: list[Action] = []
    for index, action in enumerate(actions):
        if not isinstance(action, Action):
            raise ModelValidationError(f"第 {index + 1} 个动作不是 Action 对象")
        action.validate()
        offset += action.delay_ms
        action.offset_ms = offset
        result.append(action)
    validate_action_sequence(result)
    return result


def reorder_actions(
    actions: MutableSequence[Action], source_index: int, target_index: int
) -> MutableSequence[Action]:
    """原地移动一个动作，并立即重算所有时间偏移。"""

    _validate_mutable_action_list(actions)
    source = _validate_list_index(source_index, len(actions), "原位置")
    target = _validate_list_index(target_index, len(actions), "目标位置")
    if source != target:
        action = actions.pop(source)
        actions.insert(target, action)
    recalculate_offsets(actions)
    return actions


def delete_action(
    actions: MutableSequence[Action], index: int
) -> Action:
    """删除指定动作、重算偏移并返回被删除的动作。"""

    _validate_mutable_action_list(actions)
    valid_index = _validate_list_index(index, len(actions), "动作位置")
    deleted = actions.pop(valid_index)
    recalculate_offsets(actions)
    return deleted


# 便于调用端采用更自然的单数名称。
move_action = reorder_actions


def _validate_action_params(action_type: ActionType, params: Mapping[str, Any]) -> None:
    if action_type is ActionType.MOUSE_MOVE:
        _validate_param_fields(params, {"x", "y"}, action_type)
        _validate_coordinate(params["x"], "x")
        _validate_coordinate(params["y"], "y")
        return
    if action_type is ActionType.MOUSE_BUTTON:
        _validate_param_fields(
            params, {"button", "pressed", "x", "y"}, action_type
        )
        button = params["button"]
        if not isinstance(button, str) or button not in {
            item.value for item in MouseButton
        }:
            raise ModelValidationError(
                "鼠标按钮 button 必须是 left、right 或 middle"
            )
        if not isinstance(params["pressed"], bool):
            raise ModelValidationError("鼠标按钮参数 pressed 必须是布尔值")
        _validate_coordinate(params["x"], "x")
        _validate_coordinate(params["y"], "y")
        return
    if action_type is ActionType.MOUSE_SCROLL:
        _validate_param_fields(params, {"dx", "dy", "x", "y"}, action_type)
        _validate_int32(params["dx"], "滚轮水平位移 dx")
        _validate_int32(params["dy"], "滚轮垂直位移 dy")
        if params["dx"] == 0 and params["dy"] == 0:
            raise ModelValidationError("滚轮动作的 dx 和 dy 不能同时为 0")
        _validate_coordinate(params["x"], "x")
        _validate_coordinate(params["y"], "y")
        return
    if action_type in (ActionType.KEY_DOWN, ActionType.KEY_UP):
        _validate_param_fields(params, {"key"}, action_type)
        _validate_key(params["key"])
        return
    # ``ActionType`` 当前是封闭枚举，这一分支防止未来添加类型却漏掉校验器。
    raise ModelValidationError(f"动作类型 {action_type.value} 尚未定义参数规则")


def _validate_key(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ModelValidationError("键盘参数 key 必须是非空字符串")
    if len(value) > 128 or "\x00" in value:
        raise ModelValidationError("键盘参数 key 无效")
    normalized = value.lower()
    normalized = normalized.removeprefix("key.")
    if normalized.startswith("vk:"):
        virtual_key = normalized[3:]
        if virtual_key.isdecimal() and 0 <= int(virtual_key) <= 0xFFFF:
            return
    if len(value) != 1 and normalized not in _SPECIAL_KEY_NAMES:
        raise ModelValidationError(f"无法识别的按键：{value}")


def _validate_param_fields(
    params: Mapping[str, Any], expected: set[str], action_type: ActionType
) -> None:
    _validate_exact_fields(params, frozenset(expected), f"{action_type.value} 参数")


def _validate_coordinate(value: Any, name: str) -> None:
    _validate_int32(value, f"坐标 {name}")


def _validate_int32(value: Any, name: str) -> None:
    _require_int(value, name)
    if not _COORDINATE_MIN <= value <= _COORDINATE_MAX:
        raise ModelValidationError(
            f"{name} 超出支持范围 "
            f"[{_COORDINATE_MIN}, {_COORDINATE_MAX}]"
        )


def _validate_identifier(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError("动作 id 必须是非空字符串")
    if len(value) > 128:
        raise ModelValidationError("动作 id 过长")


def _coerce_action_type(value: Any, *, label: str = "动作") -> ActionType:
    if isinstance(value, ActionType):
        return value
    if not isinstance(value, str):
        raise ModelValidationError(f"{label}字段 type 必须是字符串")
    try:
        return ActionType(value)
    except ValueError as exc:
        raise ModelValidationError(f"非法动作类型：{value}") from exc


def _validate_recording_settings(settings: Mapping[str, Any]) -> None:
    _validate_json_value(settings, "recording_settings")
    if "mouse_move_sample_interval_ms" in settings:
        value = settings["mouse_move_sample_interval_ms"]
        _require_non_negative_int(value, "鼠标移动采样间隔")
        if value == 0:
            raise ModelValidationError("鼠标移动采样间隔必须大于 0 毫秒")


def _validate_iso_datetime(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelValidationError("顶层字段 created_at 必须是 ISO 8601 时间字符串")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ModelValidationError(
            "顶层字段 created_at 不是有效的 ISO 8601 时间"
        ) from exc
    if parsed.tzinfo is None:
        raise ModelValidationError("顶层字段 created_at 必须包含时区")


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)) or _is_plain_int(value):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelValidationError(f"{path} 不能包含 NaN 或无穷大")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ModelValidationError(f"{path} 的字段名必须是非空字符串")
            _validate_json_value(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    raise ModelValidationError(f"{path} 包含无法写入 JSON 的数据类型")


def _validate_exact_fields(
    data: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    keys = set(data)
    missing = sorted(expected - keys)
    if missing:
        raise ModelValidationError(
            f"{label}缺少必需字段：{', '.join(missing)}"
        )
    unknown = sorted(keys - expected)
    if unknown:
        raise ModelValidationError(
            f"{label}包含未知字段：{', '.join(str(item) for item in unknown)}"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelValidationError(f"{label}必须是对象")
    for key in value:
        if not isinstance(key, str):
            raise ModelValidationError(f"{label}的字段名必须是字符串")
    return value


def _copy_mapping(value: Any, label: str) -> dict[str, Any]:
    mapping = _require_mapping(value, label)
    return deepcopy(dict(mapping))


def _ensure_supported_version(version: Any) -> None:
    if not _is_plain_int(version) or version < 1:
        raise ModelValidationError("format_version 必须是大于 0 的整数")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise UnsupportedFormatVersionError(
            f"不支持脚本格式版本 {version}；当前支持版本 "
            f"{CURRENT_FORMAT_VERSION}"
        )


def _require_non_negative_int(value: Any, label: str) -> None:
    _require_int(value, label)
    if value < 0:
        raise ModelValidationError(f"{label}不能小于 0")


def _require_int(value: Any, label: str) -> None:
    if not _is_plain_int(value):
        raise ModelValidationError(f"{label}必须是整数")


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_mutable_action_list(actions: MutableSequence[Action]) -> None:
    if not isinstance(actions, MutableSequence) or isinstance(actions, (str, bytes)):
        raise ModelValidationError("actions 必须是可编辑的动作数组")
    for index, action in enumerate(actions):
        if not isinstance(action, Action):
            raise ModelValidationError(f"第 {index + 1} 个动作不是 Action 对象")


def _validate_list_index(value: Any, length: int, label: str) -> int:
    _require_int(value, label)
    if not 0 <= value < length:
        raise ModelValidationError(f"{label}超出动作列表范围")
    return value


# 向较早的调用端提供语义清晰的别名。
ScriptData = ActionScript
ValidationError = ModelValidationError


__all__ = [
    "CURRENT_FORMAT_VERSION",
    "DEFAULT_APP_VERSION",
    "SUPPORTED_FORMAT_VERSIONS",
    "Action",
    "ActionScript",
    "ActionType",
    "ModelValidationError",
    "MouseButton",
    "ScriptData",
    "UnsupportedFormatVersionError",
    "ValidationError",
    "delete_action",
    "migrate_script_data",
    "move_action",
    "recalculate_offsets",
    "register_format_migration",
    "reorder_actions",
    "utc_now_iso",
    "validate_action_sequence",
]
