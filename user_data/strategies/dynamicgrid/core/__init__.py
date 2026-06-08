"""
DynamicGrid 核心模块

提供动态网格策略的核心功能，包括：
- 事件系统（event.py）
- 配置管理（config.py）
- 层级管理（level.py）
- 管理器（manager.py）
"""

from .event import (
    EventDirection,
    DynamicGridEvent,
    DynamicGridDataUpdateEvent,
    DynamicGridEntrySignalEvent,
    DynamicGridExitSignalEvent,
    DynamicGridAddSuccessEvent,
    DynamicGridReduceSuccessEvent,
    DynamicGridGlobalExitSuccessEvent,
    DynamicGridEntryTriggerEvent,
    DynamicGridExitTriggerEvent,
    DynamicGridAddTriggerEvent,
    DynamicGridReduceTriggerEvent,
    DynamicGridGlobalExitTriggerEvent,
    DynamicGridStateChangeEvent,
    get_utc_timestamp,
    normalize_datetime,
    format_china_time,
    convert_to_timestamp,
    convert_to_datetime,
)

from .config import (
    DynamicGridConfig,
    DynamicGridLevelConfig,
)

from .level import (
    DynamicGridLevel,
    DynamicGridLevelState,
)

from .manager import (
    DynamicGridManager,
)

__all__ = [
    # Event system
    "EventDirection",
    "DynamicGridEvent",
    "DynamicGridDataUpdateEvent",
    "DynamicGridEntrySignalEvent",
    "DynamicGridExitSignalEvent",
    "DynamicGridAddSuccessEvent",
    "DynamicGridReduceSuccessEvent",
    "DynamicGridGlobalExitSuccessEvent",
    "DynamicGridEntryTriggerEvent",
    "DynamicGridExitTriggerEvent",
    "DynamicGridAddTriggerEvent",
    "DynamicGridReduceTriggerEvent",
    "DynamicGridGlobalExitTriggerEvent",
    "DynamicGridStateChangeEvent",
    # Event utilities
    "get_utc_timestamp",
    "normalize_datetime",
    "format_china_time",
    "convert_to_timestamp",
    "convert_to_datetime",
    # Configuration
    "DynamicGridConfig",
    "DynamicGridLevelConfig",
    # Level management
    "DynamicGridLevel",
    "DynamicGridLevelState",
    # Manager
    "DynamicGridManager",
]

