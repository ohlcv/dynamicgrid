import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union
from enum import Enum


def get_utc_timestamp() -> float:
    """生成 UTC 时间戳（秒，浮点数）"""
    timestamp = datetime.now(timezone.utc).timestamp()
    return timestamp


def normalize_datetime(dt: Union[datetime, float, int, None]) -> tuple[datetime, float]:
    """
    标准化时间输入，返回 (datetime对象, timestamp)

    Args:
        dt: datetime对象、时间戳(float/int)或None

    Returns:
        tuple: (datetime对象(UTC), timestamp)
    """
    if dt is None:
        now = datetime.now(timezone.utc)
        return now, now.timestamp()
    elif isinstance(dt, datetime):
        # 确保是UTC时区
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            dt = dt.astimezone(timezone.utc)
        return dt, dt.timestamp()
    elif isinstance(dt, (int, float)):
        # 时间戳转datetime
        dt_obj = datetime.fromtimestamp(dt, tz=timezone.utc)
        return dt_obj, float(dt)
    else:
        raise ValueError(f"不支持的时间类型: {type(dt)}")


def format_china_time(dt: Union[datetime, float, int, None]) -> str:
    """
    格式化为中国时间字符串

    Args:
        dt: datetime对象、时间戳或None

    Returns:
        str: 格式化的中国时间字符串
    """
    if dt is None:
        return "未知时间"

    dt_obj, _ = normalize_datetime(dt)
    # 转换为中国时区 (UTC+8)
    from datetime import timedelta

    china_offset = timezone(timedelta(hours=8))
    china_time = dt_obj.astimezone(china_offset)
    return china_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " CST"


def convert_to_timestamp(v: Union[datetime, float, int, None]) -> Optional[float]:
    """
    将时间值转换为时间戳（float）
    
    Args:
        v: datetime对象、时间戳或None
        
    Returns:
        时间戳（float）或None
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.timestamp()
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def convert_to_datetime(v: Union[datetime, float, int, None]) -> Optional[datetime]:
    """
    将时间值转换为datetime对象
    
    Args:
        v: datetime对象、时间戳或None
        
    Returns:
        datetime对象或None
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


class EventDirection(Enum):
    INBOUND = "inbound"  # External → System
    OUTBOUND = "outbound"  # System → External
    INTERNAL = "internal"  # Internal only

    @staticmethod
    def filter_events(
        events: List["DynamicGridEvent"], allowed_directions: List["EventDirection"]
    ) -> List["DynamicGridEvent"]:
        """过滤事件列表，只保留允许方向的事件"""
        filtered = [event for event in events if event.direction in allowed_directions]
        return filtered

    @staticmethod
    def validate_event_direction(
        event: "DynamicGridEvent", expected_direction: "EventDirection"
    ) -> bool:
        """验证单个事件的方向是否匹配预期"""
        if event.direction != expected_direction:
            return False
        return True


@dataclass
class DynamicGridEvent:
    """动态网格策略的基础事件类"""

    event_type: str = field(default="", init=False)
    direction: EventDirection = field(default=EventDirection.INTERNAL, init=False)
    timestamp: Optional[float] = None
    datetime: Optional[datetime] = None
    data: Optional[Dict] = None

    def __post_init__(self):
        # 标准化时间处理
        if self.data is None:
            self.data = {}

        # 如果提供了datetime或timestamp，自动标准化
        if self.datetime is not None or self.timestamp is not None:
            dt_input = self.datetime if self.datetime is not None else self.timestamp
            self.datetime, self.timestamp = normalize_datetime(dt_input)

        self.validate_direction()

    def to_json(self) -> str:
        """序列化为JSON字符串
        
        注意：datetime 对象会被转换为 ISO 格式字符串，反序列化时需要手动转换
        """
        # 深拷贝 data 以避免修改原始数据
        import copy
        data_copy = copy.deepcopy(self.data)
        
        # 将 data 中的 datetime 对象转换为 ISO 格式字符串
        def convert_datetime(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            elif isinstance(obj, dict):
                return {k: convert_datetime(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_datetime(item) for item in obj]
            return obj
        
        data_copy = convert_datetime(data_copy)
        
        json_str = json.dumps(
            {
                "event_type": self.event_type,
                "direction": self.direction.value,
                "timestamp": self.timestamp,
                "data": data_copy,
            },
            default=str,
            ensure_ascii=False,
        )
        return json_str

    @classmethod
    def from_json(cls, json_str: str) -> "DynamicGridEvent":
        """从JSON字符串反序列化
        
        注意：ISO 格式的 datetime 字符串会被转换为 datetime 对象
        """
        try:
            data = json.loads(json_str)
            data["direction"] = EventDirection(data["direction"])
            
            # 恢复 data 中的 datetime 对象（从 ISO 格式字符串）
            if "data" in data and isinstance(data["data"], dict):
                def restore_datetime(obj):
                    if isinstance(obj, str):
                        # 尝试解析 ISO 格式的 datetime 字符串
                        try:
                            # 支持 ISO 格式：YYYY-MM-DDTHH:MM:SS[.ffffff][+HH:MM] 或 YYYY-MM-DD HH:MM:SS
                            if "T" in obj or (" " in obj and ":" in obj):
                                # 移除时区信息（Z 或 +HH:MM），统一处理
                                obj_clean = obj.replace("Z", "").split("+")[0].split("-")[0] if "+" in obj else obj.replace("Z", "")
                                # 尝试使用 datetime.fromisoformat（Python 3.7+）
                                try:
                                    # 处理带微秒和不带微秒的情况
                                    if "." in obj_clean:
                                        dt_str, microsecond = obj_clean.split(".")
                                        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S" if "T" in dt_str else "%Y-%m-%d %H:%M:%S")
                                        # 添加微秒（最多6位）
                                        microsecond = microsecond[:6].ljust(6, "0")
                                        dt = dt.replace(microsecond=int(microsecond))
                                    else:
                                        dt = datetime.strptime(obj_clean, "%Y-%m-%dT%H:%M:%S" if "T" in obj_clean else "%Y-%m-%d %H:%M:%S")
                                    # 设置为 UTC 时区
                                    return dt.replace(tzinfo=timezone.utc)
                                except (ValueError, AttributeError):
                                    # 如果 fromisoformat 不可用或失败，尝试 strptime
                                    pass
                        except Exception:
                            pass
                    elif isinstance(obj, dict):
                        return {k: restore_datetime(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [restore_datetime(item) for item in obj]
                    return obj
                
                data["data"] = restore_datetime(data["data"])
            
            instance = cls(**data)
            return instance
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON解析失败: {e}")
        except Exception as e:
            raise ValueError(f"事件反序列化失败: {e}")

    def validate_direction(self):
        """验证事件方向"""
        if not isinstance(self.direction, EventDirection):
            raise ValueError(f"无效的事件方向: {self.direction}")

    @property
    def china_time_str(self) -> str:
        """获取中国时间字符串"""
        return format_china_time(self.datetime)

    @property
    def utc_time_str(self) -> str:
        """获取UTC时间字符串"""
        if self.datetime is None:
            return "未知时间"
        return self.datetime.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"

    def get_time_str(self, timezone: str = "china") -> str:
        """
        获取指定时区的时间字符串

        Args:
            timezone: "china" 或 "utc"

        Returns:
            str: 格式化的时间字符串
        """
        if timezone.lower() == "china":
            return self.china_time_str
        elif timezone.lower() == "utc":
            return self.utc_time_str
        else:
            raise ValueError(f"不支持的时区: {timezone}")


# External → System Events


@dataclass
class DynamicGridDataUpdateEvent(DynamicGridEvent):
    """K线数据更新事件，提供 OHLCV 数据"""

    event_type: str = field(default="data_update", init=False)
    direction: EventDirection = field(default=EventDirection.INBOUND, init=False)

    @classmethod
    def create(
        cls,
        pair: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridDataUpdateEvent":
        """
        创建K线数据更新事件的便利方法

        Args:
            pair: 交易对
            open_price: 开盘价
            high: 最高价
            low: 最低价
            close: 收盘价
            volume: 成交量
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridDataUpdateEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "pair": pair,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "current_time": dt_obj,  # 在data中也保存datetime对象
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = ["pair", "open", "high", "low", "close", "volume"]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"K线数据更新事件需要包含 '{key}' 数据")
        for key in ["open", "high", "low", "close", "volume"]:
            try:
                float(self.data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是有效的数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")
        # 可选的当前时间与原始K线
        if "current_time" in self.data:
            current_time = self.data["current_time"]
            # 允许 datetime 对象或数值时间戳
            if not (isinstance(current_time, datetime) or isinstance(current_time, (int, float))):
                try:
                    float(current_time)  # 尝试转换为数值
                except (TypeError, ValueError):
                    raise ValueError("current_time 必须是 datetime 对象或数值(时间戳)")
        if "last_candle" in self.data and not isinstance(self.data["last_candle"], dict):
            raise ValueError("last_candle 必须是字典")

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def price(self) -> float:
        return float(self.data["close"])

    @property
    def open(self) -> float:
        return float(self.data["open"])

    @property
    def high(self) -> float:
        return float(self.data["high"])

    @property
    def low(self) -> float:
        return float(self.data["low"])

    @property
    def volume(self) -> float:
        return float(self.data["volume"])

    @property
    def current_time(self) -> Optional[float]:
        """获取当前时间戳（float）"""
        v = self.data.get("current_time")
        return convert_to_timestamp(v)
    
    @property
    def current_time_dt(self) -> Optional[datetime]:
        """获取当前时间（datetime对象）"""
        v = self.data.get("current_time")
        return convert_to_datetime(v)

    @property
    def last_candle(self) -> Dict:
        return self.data.get("last_candle", {})


@dataclass
class DynamicGridEntrySignalEvent(DynamicGridEvent):
    """外部开仓信号事件，针对第一层级（level_id=1）"""

    event_type: str = field(default="entry_signal", init=False)
    direction: EventDirection = field(default=EventDirection.INBOUND, init=False)

    @classmethod
    def create(
        cls,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        target_price: float,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridEntrySignalEvent":
        """
        创建开仓信号事件的便利方法

        Args:
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            target_price: 目标价格
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridEntrySignalEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "target_price": target_price,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = ["is_short", "pair"]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"开仓信号事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        # 可选价格（例如 confirm_trade_entry 传入的 rate）
        if "price" in self.data:
            try:
                float(self.data["price"])
            except (TypeError, ValueError):
                raise ValueError("price 必须是有效的数值")

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def signal_name(self) -> str:
        return self.data.get("signal_name", "unknown")

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def price(self) -> Optional[float]:
        v = self.data.get("price")
        return float(v) if v is not None else None


@dataclass
class DynamicGridExitSignalEvent(DynamicGridEvent):
    """外部平仓信号事件，针对最后一层级"""

    event_type: str = field(default="exit_signal", init=False)
    direction: EventDirection = field(default=EventDirection.INBOUND, init=False)

    @classmethod
    def create(
        cls,
        level_id: int,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        target_price: float,
        reason: str,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridExitSignalEvent":
        """
        创建平仓信号事件的便利方法

        Args:
            level_id: 层级ID
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            target_price: 目标价格
            reason: 平仓原因
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridExitSignalEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": level_id,
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "target_price": target_price,
            "reason": reason,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = ["is_short", "pair"]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"平仓信号事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        # 可选价格
        if "price" in self.data:
            try:
                float(self.data["price"])
            except (TypeError, ValueError):
                raise ValueError("price 必须是有效的数值")

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def signal_name(self) -> str:
        return self.data.get("signal_name", "unknown")

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def price(self) -> Optional[float]:
        v = self.data.get("price")
        return float(v) if v is not None else None


@dataclass
class DynamicGridAddSuccessEvent(DynamicGridEvent):
    """外部加仓成功事件，针对后续层级"""

    event_type: str = field(default="add_success", init=False)
    direction: EventDirection = field(default=EventDirection.INBOUND, init=False)

    @classmethod
    def create(
        cls,
        level_id: int,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        executed_price: float,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridAddSuccessEvent":
        """
        创建加仓成功事件的便利方法

        Args:
            level_id: 层级ID
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            executed_price: 执行价格
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridAddSuccessEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": level_id,
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "executed_price": executed_price,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "level_id",
            "is_short",
            "base_amount",
            "quote_amount",
            "executed_price",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"加仓成功事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        try:
            base_amount = float(self.data["base_amount"])
            quote_amount = float(self.data["quote_amount"])
            price = float(self.data["executed_price"])
            if base_amount <= 0 or quote_amount <= 0 or price <= 0:
                raise ValueError("base_amount, quote_amount 和 executed_price 必须为正数")
        except (TypeError, ValueError):
            raise ValueError("base_amount, quote_amount 和 executed_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def level_id(self) -> int:
        return self.data["level_id"]

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def base_amount(self) -> float:
        return float(self.data["base_amount"])

    @property
    def quote_amount(self) -> float:
        return float(self.data["quote_amount"])

    @property
    def executed_price(self) -> float:
        return float(self.data["executed_price"])

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def current_time(self) -> Optional[float]:
        """获取当前时间戳（float）"""
        v = self.data.get("current_time")
        return convert_to_timestamp(v)
    
    @property
    def current_time_dt(self) -> Optional[datetime]:
        """获取当前时间（datetime对象）"""
        v = self.data.get("current_time")
        return convert_to_datetime(v)

    @property
    def last_candle(self) -> Dict:
        return self.data.get("last_candle", {})

    # Optional order fields to allow precise backfill
    @property
    def order_id(self) -> Optional[str]:
        return self.data.get("order_id")

    @property
    def fee_base(self) -> Optional[float]:
        v = self.data.get("fee_base")
        return float(v) if v is not None else None

    @property
    def fee_quote(self) -> Optional[float]:
        v = self.data.get("fee_quote")
        return float(v) if v is not None else None


@dataclass
class DynamicGridReduceSuccessEvent(DynamicGridEvent):
    """外部减仓成功事件，针对后续层级"""

    event_type: str = field(default="reduce_success", init=False)
    direction: EventDirection = field(default=EventDirection.INBOUND, init=False)

    @classmethod
    def create(
        cls,
        level_id: int,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        executed_price: float,
        reason: str,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridReduceSuccessEvent":
        """
        创建减仓成功事件的便利方法

        Args:
            level_id: 层级ID
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            executed_price: 执行价格
            reason: 减仓原因
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridReduceSuccessEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": level_id,
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "executed_price": executed_price,
            "reason": reason,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "level_id",
            "is_short",
            "base_amount",
            "quote_amount",
            "executed_price",
            "reason",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"减仓成功事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        # reason 验证：允许常见原因，但不强制限制（便于扩展）
        valid_reasons = [
            "take_profit",
            "global_take_profit",
            "global_stop_loss",
            "reduce",
            "unknown",
        ]
        if self.data["reason"] not in valid_reasons:
            # 使用警告而非错误，允许扩展新的 reason
            import warnings
            warnings.warn(
                f"减仓原因 '{self.data['reason']}' 不在常见列表中 {valid_reasons}，"
                f"但允许使用（便于扩展）",
                UserWarning
            )
        try:
            base_amount = float(self.data["base_amount"])
            quote_amount = float(self.data["quote_amount"])
            price = float(self.data["executed_price"])
            if base_amount <= 0 or quote_amount <= 0 or price <= 0:
                raise ValueError("base_amount, quote_amount 和 executed_price 必须为正数")
        except (TypeError, ValueError):
            raise ValueError("base_amount, quote_amount 和 executed_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def level_id(self) -> int:
        return self.data["level_id"]

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def base_amount(self) -> float:
        return float(self.data["base_amount"])

    @property
    def quote_amount(self) -> float:
        return float(self.data["quote_amount"])

    @property
    def executed_price(self) -> float:
        return float(self.data["executed_price"])

    @property
    def reason(self) -> str:
        return self.data["reason"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def current_time(self) -> Optional[float]:
        """获取当前时间戳（float）"""
        v = self.data.get("current_time")
        return convert_to_timestamp(v)
    
    @property
    def current_time_dt(self) -> Optional[datetime]:
        """获取当前时间（datetime对象）"""
        v = self.data.get("current_time")
        return convert_to_datetime(v)

    @property
    def last_candle(self) -> Dict:
        return self.data.get("last_candle", {})

    # Optional order fields to allow precise backfill
    @property
    def order_id(self) -> Optional[str]:
        return self.data.get("order_id")

    @property
    def fee_base(self) -> Optional[float]:
        v = self.data.get("fee_base")
        return float(v) if v is not None else None

    @property
    def fee_quote(self) -> Optional[float]:
        v = self.data.get("fee_quote")
        return float(v) if v is not None else None


@dataclass
class DynamicGridGlobalExitSuccessEvent(DynamicGridEvent):
    """外部全局退出成功事件，针对所有层级"""

    event_type: str = field(default="global_exit_success", init=False)
    direction: EventDirection = field(default=EventDirection.INBOUND, init=False)

    @classmethod
    def create(
        cls,
        is_short: bool,
        total_base_amount: float,
        total_quote_amount: float,
        executed_price: float,
        reason: str,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridGlobalExitSuccessEvent":
        """
        创建全局退出成功事件的便利方法

        Args:
            is_short: 是否做空
            total_base_amount: 总基础币数量
            total_quote_amount: 总计价币数量
            executed_price: 执行价格
            reason: 退出原因
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridGlobalExitSuccessEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "is_short": is_short,
            "total_base_amount": total_base_amount,
            "total_quote_amount": total_quote_amount,
            "executed_price": executed_price,
            "reason": reason,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "is_short",
            "total_base_amount",
            "total_quote_amount",
            "executed_price",
            "reason",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"全局退出成功事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        valid_reasons = ["global_take_profit", "global_stop_loss"]
        if self.data["reason"] not in valid_reasons:
            raise ValueError(f"全局退出原因必须是 {valid_reasons} 中的一种")
        try:
            total_base_amount = float(self.data["total_base_amount"])
            total_quote_amount = float(self.data["total_quote_amount"])
            price = float(self.data["executed_price"])
            if total_base_amount <= 0 or total_quote_amount <= 0 or price <= 0:
                raise ValueError(
                    "total_base_amount, total_quote_amount 和 executed_price 必须为正数"
                )
        except (TypeError, ValueError):
            raise ValueError(
                "total_base_amount, total_quote_amount 和 executed_price 必须是有效数值"
            )
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def total_base_amount(self) -> float:
        return float(self.data["total_base_amount"])

    @property
    def total_quote_amount(self) -> float:
        return float(self.data["total_quote_amount"])

    @property
    def executed_price(self) -> float:
        return float(self.data["executed_price"])

    @property
    def reason(self) -> str:
        return self.data["reason"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def current_time(self) -> Optional[float]:
        """获取当前时间戳（float）"""
        v = self.data.get("current_time")
        return convert_to_timestamp(v)
    
    @property
    def current_time_dt(self) -> Optional[datetime]:
        """获取当前时间（datetime对象）"""
        v = self.data.get("current_time")
        return convert_to_datetime(v)

    @property
    def last_candle(self) -> Dict:
        return self.data.get("last_candle", {})


# System → External Events


@dataclass
class DynamicGridEntryTriggerEvent(DynamicGridEvent):
    """通知外部执行开仓，针对第一层级（level_id=1）"""

    event_type: str = field(default="entry_trigger", init=False)
    direction: EventDirection = field(default=EventDirection.OUTBOUND, init=False)

    @classmethod
    def create(
        cls,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        target_price: float,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridEntryTriggerEvent":
        """
        创建开仓触发事件的便利方法

        Args:
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            target_price: 目标价格
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridEntryTriggerEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": 1,  # 第一层固定为1
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "target_price": target_price,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "level_id",
            "is_short",
            "base_amount",
            "quote_amount",
            "target_price",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"开仓触发事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        if self.data.get("level_id") != 1:
            raise ValueError("开仓触发事件的 level_id 必须为 1")
        try:
            base_amount = float(self.data["base_amount"])
            quote_amount = float(self.data["quote_amount"])
            price = float(self.data["target_price"])
            if base_amount < 0 or quote_amount < 0 or price < 0:
                raise ValueError("base_amount, quote_amount 和 target_price 必须不为负数")
        except (TypeError, ValueError):
            raise ValueError("base_amount, quote_amount 和 target_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def level_id(self) -> int:
        return 1  # 第一层固定为1

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def base_amount(self) -> float:
        return float(self.data["base_amount"])

    @property
    def quote_amount(self) -> float:
        return float(self.data["quote_amount"])

    @property
    def target_price(self) -> float:
        return float(self.data["target_price"])

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})


@dataclass
class DynamicGridExitTriggerEvent(DynamicGridEvent):
    """通知外部执行平仓，针对第一层级（level_id=1）"""

    event_type: str = field(default="exit_trigger", init=False)
    direction: EventDirection = field(default=EventDirection.OUTBOUND, init=False)

    @classmethod
    def create(
        cls,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        target_price: float,
        reason: str,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridExitTriggerEvent":
        """
        创建平仓触发事件的便利方法

        Args:
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            target_price: 目标价格
            reason: 平仓原因
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridExitTriggerEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": 1,  # 第一层固定为1
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "target_price": target_price,
            "reason": reason,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "level_id",
            "is_short",
            "base_amount",
            "quote_amount",
            "target_price",
            "reason",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"平仓触发事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        if self.data.get("level_id") != 1:
            raise ValueError("平仓触发事件的 level_id 必须为 1")
        try:
            base_amount = float(self.data["base_amount"])
            quote_amount = float(self.data["quote_amount"])
            price = float(self.data["target_price"])
            if base_amount < 0 or quote_amount < 0 or price < 0:
                raise ValueError("base_amount, quote_amount 和 target_price 必须不为负数")
        except (TypeError, ValueError):
            raise ValueError("base_amount, quote_amount 和 target_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def level_id(self) -> int:
        return 1  # 第一层固定为1

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def base_amount(self) -> float:
        return float(self.data["base_amount"])

    @property
    def quote_amount(self) -> float:
        return float(self.data["quote_amount"])

    @property
    def target_price(self) -> float:
        return float(self.data["target_price"])

    @property
    def reason(self) -> str:
        return self.data["reason"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})


@dataclass
class DynamicGridAddTriggerEvent(DynamicGridEvent):
    """通知外部执行加仓，针对后续层级（level_id >= 2）"""

    event_type: str = field(default="add_trigger", init=False)
    direction: EventDirection = field(default=EventDirection.OUTBOUND, init=False)

    @classmethod
    def create(
        cls,
        level_id: int,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        target_price: float,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridAddTriggerEvent":
        """
        创建加仓触发事件的便利方法

        Args:
            level_id: 层级ID
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            target_price: 目标价格
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridAddTriggerEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": level_id,
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "target_price": target_price,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "level_id",
            "is_short",
            "base_amount",
            "quote_amount",
            "target_price",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"加仓触发事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        try:
            base_amount = float(self.data["base_amount"])
            quote_amount = float(self.data["quote_amount"])
            price = float(self.data["target_price"])
            if base_amount < 0 or quote_amount < 0 or price < 0:
                raise ValueError("base_amount, quote_amount 和 target_price 必须不为负数")
        except (TypeError, ValueError):
            raise ValueError("base_amount, quote_amount 和 target_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def level_id(self) -> int:
        return self.data["level_id"]

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def base_amount(self) -> float:
        return float(self.data["base_amount"])

    @property
    def quote_amount(self) -> float:
        return float(self.data["quote_amount"])

    @property
    def target_price(self) -> float:
        return float(self.data["target_price"])

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})


@dataclass
class DynamicGridReduceTriggerEvent(DynamicGridEvent):
    """通知外部执行减仓，针对后续层级"""

    event_type: str = field(default="reduce_trigger", init=False)
    direction: EventDirection = field(default=EventDirection.OUTBOUND, init=False)

    @classmethod
    def create(
        cls,
        level_id: int,
        is_short: bool,
        base_amount: float,
        quote_amount: float,
        target_price: float,
        reason: str,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridReduceTriggerEvent":
        """
        创建减仓触发事件的便利方法

        Args:
            level_id: 层级ID
            is_short: 是否做空
            base_amount: 基础币数量
            quote_amount: 计价币数量
            target_price: 目标价格
            reason: 减仓原因
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridReduceTriggerEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": level_id,
            "is_short": is_short,
            "base_amount": base_amount,
            "quote_amount": quote_amount,
            "target_price": target_price,
            "reason": reason,
            "pair": pair,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "level_id",
            "is_short",
            "base_amount",
            "quote_amount",
            "target_price",
            "reason",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"减仓触发事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        valid_reasons = ["take_profit", "global_take_profit", "global_stop_loss"]
        if self.data["reason"] not in valid_reasons:
            raise ValueError(f"减仓原因必须是 {valid_reasons} 中的一种")
        try:
            base_amount = float(self.data["base_amount"])
            quote_amount = float(self.data["quote_amount"])
            price = float(self.data["target_price"])
            if base_amount < 0 or quote_amount < 0 or price < 0:
                raise ValueError("base_amount, quote_amount 和 target_price 必须不为负数")
        except (TypeError, ValueError):
            raise ValueError("base_amount, quote_amount 和 target_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def level_id(self) -> int:
        return self.data["level_id"]

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def base_amount(self) -> float:
        return float(self.data["base_amount"])

    @property
    def quote_amount(self) -> float:
        return float(self.data["quote_amount"])

    @property
    def target_price(self) -> float:
        return float(self.data["target_price"])

    @property
    def reason(self) -> str:
        return self.data["reason"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})


@dataclass
class DynamicGridGlobalExitTriggerEvent(DynamicGridEvent):
    """通知外部执行全局退出"""

    event_type: str = field(default="global_exit_trigger", init=False)
    direction: EventDirection = field(default=EventDirection.OUTBOUND, init=False)

    @classmethod
    def create(
        cls,
        is_short: bool,
        reason: str,
        pair: str,
        total_base_amount: float,
        total_quote_amount: float,
        target_price: float,
        current_time: Union[datetime, float, int, None] = None,
        **kwargs,
    ) -> "DynamicGridGlobalExitTriggerEvent":
        """
        创建全局退出触发事件的便利方法

        Args:
            is_short: 是否做空
            reason: 退出原因
            pair: 交易对
            total_base_amount: 总基础币数量
            total_quote_amount: 总计价币数量
            target_price: 目标价格
            current_time: 当前时间 (datetime对象或时间戳)
            **kwargs: 其他事件参数

        Returns:
            DynamicGridGlobalExitTriggerEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "is_short": is_short,
            "reason": reason,
            "pair": pair,
            "total_base_amount": total_base_amount,
            "total_quote_amount": total_quote_amount,
            "target_price": target_price,
        }

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = [
            "is_short",
            "total_base_amount",
            "total_quote_amount",
            "target_price",
            "reason",
            "pair",
        ]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"全局退出触发事件需要包含 '{key}' 数据")
        if not isinstance(self.data["is_short"], bool):
            raise ValueError("方向必须是布尔值 is_short")
        # reason 验证：允许常见原因，但不强制限制（便于扩展）
        valid_reasons = ["global_take_profit", "global_stop_loss"]
        if self.data["reason"] not in valid_reasons:
            # 使用警告而非错误，允许扩展新的 reason
            import warnings
            warnings.warn(
                f"全局退出原因 '{self.data['reason']}' 不在常见列表中 {valid_reasons}，"
                f"但允许使用（便于扩展）",
                UserWarning
            )
        try:
            total_base_amount = float(self.data["total_base_amount"])
            total_quote_amount = float(self.data["total_quote_amount"])
            price = float(self.data["target_price"])
            if total_base_amount < 0 or total_quote_amount < 0 or price < 0:
                raise ValueError(
                    "total_base_amount, total_quote_amount 和 target_price 必须不为负数"
                )
        except (TypeError, ValueError):
            raise ValueError("total_base_amount, total_quote_amount 和 target_price 必须是有效数值")
        if not isinstance(self.data["pair"], str) or not self.data["pair"].strip():
            raise ValueError("交易对必须是非空字符串")

    @property
    def is_short(self) -> bool:
        return self.data["is_short"]

    @property
    def total_base_amount(self) -> float:
        return float(self.data["total_base_amount"])

    @property
    def total_quote_amount(self) -> float:
        return float(self.data["total_quote_amount"])

    @property
    def target_price(self) -> float:
        return float(self.data["target_price"])

    @property
    def reason(self) -> str:
        return self.data["reason"]

    @property
    def pair(self) -> str:
        return self.data["pair"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})


# Internal Events


@dataclass
class DynamicGridStateChangeEvent(DynamicGridEvent):
    """状态变更事件，触发网格层级状态转换"""

    event_type: str = field(default="state_change", init=False)
    direction: EventDirection = field(default=EventDirection.INTERNAL, init=False)

    @classmethod
    def create(
        cls,
        level_id: int,
        from_state: str,
        to_state: str,
        pair: str,
        current_time: Union[datetime, float, int, None] = None,
        reason: Optional[str] = None,
        **kwargs,
    ) -> "DynamicGridStateChangeEvent":
        """
        创建状态变更事件的便利方法

        Args:
            level_id: 层级ID
            from_state: 原状态
            to_state: 目标状态
            pair: 交易对
            current_time: 当前时间 (datetime对象或时间戳)
            reason: 状态变更原因（可选）
            **kwargs: 其他事件参数

        Returns:
            DynamicGridStateChangeEvent: 创建的事件
        """
        # 标准化时间
        dt_obj, timestamp = normalize_datetime(current_time)

        data = {
            "level_id": level_id,
            "from_state": from_state,
            "to_state": to_state,
            "pair": pair,
        }
        if reason is not None:
            data["reason"] = reason

        return cls(timestamp=timestamp, datetime=dt_obj, data=data, **kwargs)

    def __post_init__(self):
        super().__post_init__()
        required_keys = ["level_id", "from_state", "to_state"]
        for key in required_keys:
            if key not in self.data:
                raise ValueError(f"状态变更事件需要包含 '{key}' 数据")
        try:
            int(self.data["level_id"])
        except (TypeError, ValueError):
            raise ValueError("level_id 必须是有效整数")

    @property
    def level_id(self) -> int:
        return self.data["level_id"]

    @property
    def from_state(self) -> str:
        return self.data["from_state"]

    @property
    def to_state(self) -> str:
        return self.data["to_state"]

    @property
    def reason(self) -> str:
        return self.data["reason"]

    @property
    def custom_data(self) -> Dict:
        return self.data.get("custom_data", {})

    @property
    def current_time(self) -> Optional[float]:
        """获取当前时间戳（float）"""
        v = self.data.get("current_time")
        return convert_to_timestamp(v)
    
    @property
    def current_time_dt(self) -> Optional[datetime]:
        """获取当前时间（datetime对象）"""
        v = self.data.get("current_time")
        return convert_to_datetime(v)

    @property
    def last_candle(self) -> Dict:
        return self.data.get("last_candle", {})
