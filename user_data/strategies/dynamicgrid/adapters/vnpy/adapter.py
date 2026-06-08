"""
DynamicGrid vnpy 适配器

将 vnpy 框架的数据和事件转换为 DynamicGrid 核心事件系统
"""

import logging
from pathlib import Path
import json
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

# 设置 dynamicgrid 相关 logger 的默认级别为 INFO
# 这样无论在哪里使用，都会有一个合理的默认值
# 如果用户需要 DEBUG 日志，可以在外部覆盖（例如在 run_dynamicgrid_backtest.py 中）
_dynamicgrid_loggers = [
    'dynamicgrid',
    'dynamicgrid.level',
    'dynamicgrid.manager',
    'dynamicgrid.adapters',
    'dynamicgrid.adapters.vnpy',
    'dynamicgrid.adapters.vnpy.strategy',
    'dynamicgrid.adapters.vnpy.adapter',
]

for logger_name in _dynamicgrid_loggers:
    logging.getLogger(logger_name).setLevel(logging.INFO)

try:
    from vnpy.trader.object import BarData, TickData, TradeData, OrderData
    from vnpy.trader.constant import Direction, Offset
    from vnpy.trader.utility import BarGenerator, ArrayManager
except ImportError:
    # 如果 vnpy 未安装，提供占位符
    BarData = None
    TickData = None
    TradeData = None
    OrderData = None
    Direction = None
    Offset = None
    BarGenerator = None
    ArrayManager = None

# 导入核心模块（包内相对导入）
from ...core.event import (
    DynamicGridDataUpdateEvent,
    DynamicGridAddSuccessEvent,
    DynamicGridReduceSuccessEvent,
    DynamicGridGlobalExitSuccessEvent,
    DynamicGridEntryTriggerEvent,
    DynamicGridExitTriggerEvent,
    DynamicGridAddTriggerEvent,
    DynamicGridReduceTriggerEvent,
    DynamicGridGlobalExitTriggerEvent,
    DynamicGridEntrySignalEvent,
    DynamicGridEvent,
    format_china_time,
)
from ...core.config import DynamicGridConfig
from ...core.manager import DynamicGridManager
from ...utils.tag_utils import DynamicGridTagUtils


class DynamicGridVnpyAdapter:
    """vnpy 框架适配器
    
    负责：
    1. 将 vnpy 的 BarData/TickData 转换为 DynamicGridDataUpdateEvent
    2. 将 vnpy 的 TradeData 转换为 DynamicGridAddSuccessEvent/ReduceSuccessEvent
    3. 将 DynamicGridAddTriggerEvent/ReduceTriggerEvent 转换为 vnpy 的订单操作
    4. 管理网格状态恢复
    """
    
    def __init__(self, strategy_name: str = "DynamicGrid"):
        """初始化适配器
        
        Args:
            strategy_name: 策略名称，用于日志标识
        """
        self.strategy_name = strategy_name
        self.grid_managers: Dict[str, DynamicGridManager] = {}
        
        # 由 Cta 策略注入的写日志函数（如 CtaTemplate.write_log）
        self._write_log: Optional[Callable[[str], None]] = None
        
        # 由 Cta 策略注入的获取实际持仓量函数（用于平仓时使用实际持仓量，避免精度问题）
        self._get_position: Optional[Callable[[], float]] = None
        
        # 订单映射：存储订单ID到层级ID的映射
        self._order_to_level: Dict[str, int] = {}
        self._order_to_reason: Dict[str, str] = {}
    
        # 信号配置
        self.signals_config: Dict[str, Any] = {}
        
        # 每个交易对、每个时间框架的 ArrayManager 和 BarGenerator
        # key: (pair, timeframe), value: (BarGenerator, ArrayManager)
        # 例如: ("BTCUSDT", "1m") -> (BarGenerator, ArrayManager)
        self._indicator_managers: Dict[Tuple[str, str], Tuple[Any, Any]] = {}
        
        # 每个交易对、每个时间框架的上一个指标值（用于检测交叉）
        # key: (pair, timeframe, indicator_name), value: previous value
        # 例如: ("BTCUSDT", "1m", "rsi") -> 45.2
        self._prev_indicator_values: Dict[Tuple[str, str, str], Optional[float]] = {}
        
        # 指标值获取函数映射（动态扩展）
        self._indicator_getters = {
            "rsi": lambda am, length: am.rsi(length) if hasattr(am, 'rsi') else None,
            "cci": lambda am, length: am.cci(length) if hasattr(am, 'cci') else None,
            # 可以在这里添加更多指标，如：
            # "macd": lambda am, fast, slow, signal: am.macd(fast, slow, signal) if hasattr(am, 'macd') else None,
        }

    def set_write_log(self, write_log: Callable[[str], None]) -> None:
        """由策略注入 vnpy 的 write_log，用于统一日志出口"""
        self._write_log = write_log
        
        # 创建一个自定义的 logging handler，将 dynamicgrid 的日志转发到 write_log
        # 这样 Station 的日志窗口就能显示 dynamicgrid 的日志了
        if write_log:
            class WriteLogHandler(logging.Handler):
                """将 logging 消息转发到 vnpy 的 write_log"""
                def __init__(self, write_log_func):
                    super().__init__()
                    self.write_log_func = write_log_func
                    # 设置格式
                    self.setFormatter(
                        logging.Formatter(
                            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S'
                        )
                    )
                
                def emit(self, record):
                    """发送日志记录到 write_log"""
                    try:
                        msg = self.format(record)
                        self.write_log_func(msg)
                    except Exception:
                        self.handleError(record)
            
            # 为所有 dynamicgrid logger 添加这个 handler
            dynamicgrid_logger = logging.getLogger("dynamicgrid")
            # 注意：不要在这里修改日志级别，保持模块级别的默认设置（INFO）
            # 如果用户需要 DEBUG 日志，应该在外部设置
            
            # 检查是否已经添加过 handler（避免重复添加）
            handler_exists = any(
                isinstance(h, WriteLogHandler) for h in dynamicgrid_logger.handlers
            )
            
            if not handler_exists:
                handler = WriteLogHandler(write_log)
                # Handler 的级别应该与 logger 的级别一致，或者设置为 NOTSET 以继承 logger 的级别
                handler.setLevel(logging.NOTSET)  # NOTSET 表示继承 logger 的级别
                dynamicgrid_logger.addHandler(handler)
                # 确保日志能够传播（这样即使子 logger 没有 handler，也能通过父 logger 输出）
                dynamicgrid_logger.propagate = True
    
    def set_get_position(self, get_position: Callable[[], float]) -> None:
        """设置获取实际持仓量的函数
        
        Args:
            get_position: 返回当前实际持仓量的函数（正数=多头，负数=空头，0=无持仓）
                         用于平仓时使用实际持仓量，避免浮点数精度问题
        """
        self._get_position = get_position
    
    def _log(self, msg: str) -> None:
        """统一日志输出，使用 vnpy 的 write_log"""
        if self._write_log:
            self._write_log(msg)

    # ---------------- 工具方法 ----------------
    def _normalize_pair_key(self, pair: str) -> str:
        """规范化交易对键，确保配置与 vt_symbol 能对上。

        支持示例：
        - "BTCUSDT"            -> "BTCUSDT"
        - "BTC/USDT"           -> "BTCUSDT"
        - "BTC/USDT:USDT"      -> "BTCUSDT"
        - "BTCUSDT_SPOT_OKX.GLOBAL" -> "BTCUSDT"
        """
        if not pair:
            return pair

        # 去掉交易所/报价币等后缀（Freqtrade/OKX 风格）
        if ":" in pair:
            pair = pair.split(":", 1)[0]

        # 去掉斜杠（现货对）
        if "/" in pair:
            base, quote = pair.split("/", 1)
            pair = f"{base}{quote}"

        # 去掉下划线后的交易所/类型后缀（如 _SPOT_OKX、_SWAP_OKX 等）
        if "_" in pair:
            pair = pair.split("_", 1)[0]

        # 去掉 vnpy 的 ".EXCHANGE" 等后缀
        if "." in pair:
            pair = pair.split(".", 1)[0]

        return pair
    
    def get_signal_configs(self, indicator_key: str) -> List[Dict[str, Any]]:
        """获取指标配置列表
        
        Args:
            indicator_key: 指标键名（如 "rsi", "cci"）
        
        Returns:
            配置列表
        """
        signal_data = self.signals_config.get(indicator_key)
        if signal_data is None:
            return []
        
        # 直接返回列表（新格式）
        if isinstance(signal_data, list):
            return signal_data
        
        # 如果不是列表，返回空列表
        return []
    
    def _timeframe_to_minutes(self, timeframe: str) -> Optional[int]:
        """将时间框架字符串转换为分钟数
        
        Args:
            timeframe: 时间框架字符串，如 "1m", "5m", "15m", "1h", "4h"
        
        Returns:
            分钟数，如果无法解析返回 None
        """
        timeframe = timeframe.lower().strip()
        
        if timeframe.endswith("m"):
            try:
                return int(timeframe[:-1])
            except ValueError:
                return None
        elif timeframe.endswith("h"):
            try:
                hours = int(timeframe[:-1])
                return hours * 60
            except ValueError:
                return None
        elif timeframe.endswith("d"):
            try:
                days = int(timeframe[:-1])
                return days * 24 * 60
            except ValueError:
                return None
        
        return None
    
    def _process_timeframe_bar(self, bar: BarData, pair: str, timeframe: str) -> None:
        """处理特定时间框架的 K 线
        
        Args:
            bar: K 线数据
            pair: 交易对
            timeframe: 时间框架
        """
        key = (pair, timeframe)
        if key not in self._indicator_managers:
            return
        
        _, am = self._indicator_managers[key]
        prev_inited = am.inited
        am.update_bar(bar)
        buf_len = getattr(am, "size", None) or getattr(am, "count", None) or 0

        self._log(
            f"DGL-VNPY-ADAPTER|时间框架K线|pair={pair}|timeframe={timeframe}|"
            f"价格={bar.close_price}|ArrayManager更新前inited={prev_inited}|"
            f"更新后inited={am.inited}|缓冲区长度={buf_len}"
        )
    
    def _get_indicator_value(self, pair: str, timeframe: str, indicator_name: str, **kwargs) -> Optional[float]:
        """获取指标值（使用 ArrayManager + talib）
        
        Args:
            pair: 交易对
            timeframe: 时间框架
            indicator_name: 指标名称（"rsi", "cci" 等）
            **kwargs: 指标参数（如 rsi_length=14）
        
        Returns:
            指标值，如果数据不足或计算失败返回 None
        """
        if not ArrayManager:
            return None
        
        key = (pair, timeframe)
        if key not in self._indicator_managers:
            return None
        
        _, am = self._indicator_managers[key]
        if not am.inited:
            self._log(
                f"DGL-VNPY-ADAPTER|指标未就绪|pair={pair}|timeframe={timeframe}|"
                f"indicator={indicator_name}|inited={am.inited}"
            )
            return None
        
        # 动态获取指标值
        if indicator_name not in self._indicator_getters:
            self._log(f"DGL-VNPY-ADAPTER|未知指标类型|指标={indicator_name}")
            return None
        
        try:
            getter = self._indicator_getters[indicator_name]
            # 获取 length 参数（支持不同指标的不同参数名）
            length = kwargs.get("length", kwargs.get(f"{indicator_name}_length", 14))
            value = getter(am, length)
            self._log(
                f"DGL-VNPY-ADAPTER|指标计算|pair={pair}|timeframe={timeframe}|"
                f"indicator={indicator_name}|length={length}|value={value}"
            )
            return value
        except Exception as e:
            self._log(
                f"DGL-VNPY-ADAPTER|指标计算失败|pair={pair}|timeframe={timeframe}|"
                f"indicator={indicator_name}|error={e}"
            )
            return None
    
    def _check_signal(self, pair: str, current_price: float, current_time, manager: DynamicGridManager) -> List[DynamicGridEvent]:
        """检查并生成信号事件（支持多周期共振）
        
        Args:
            pair: 交易对
            current_price: 当前价格
            current_time: 当前时间（datetime 对象）
            manager: 网格管理器
        
        Returns:
            信号事件列表
        """
        signal_events = []
        
        # 收集所有启用的信号配置，按时间框架分组（动态解析）
        active_signals_by_timeframe: Dict[str, List[Dict]] = {}
        
        # 动态遍历所有信号配置
        for indicator_type, configs in self.signals_config.items():
            if indicator_type not in self._indicator_getters:
                self._log(f"DGL-VNPY-ADAPTER|未知指标类型|指标={indicator_type}|跳过")
                continue
            
            indicator_configs = self.get_signal_configs(indicator_type)
            for config in indicator_configs:
                if not config.get("use_signal", False):
                    continue
                timeframe = config.get("timeframe", "1m")
                if timeframe not in active_signals_by_timeframe:
                    active_signals_by_timeframe[timeframe] = []
                active_signals_by_timeframe[timeframe].append({
                    "type": indicator_type,
                    "config": config
                })
        
        # 检查每个时间框架的信号
        # 多周期共振：所有启用的时间框架都必须满足条件
        all_timeframes_triggered = True
        triggered_signals = []
        
        for timeframe, signals in active_signals_by_timeframe.items():
            self._log(
                f"DGL-VNPY-ADAPTER|检查时间框架信号|pair={pair}|timeframe={timeframe}|"
                f"信号数={len(signals)}"
            )
            timeframe_triggered = False
            
            for signal_info in signals:
                signal_type = signal_info["type"]
                config = signal_info["config"]
                
                # 动态获取指标参数
                length = config.get(f"{signal_type}_length", config.get("length", 14))
                long_threshold = config.get(f"{signal_type}_long_threshold", None)
                short_threshold = config.get(f"{signal_type}_short_threshold", None)
                signal_type_long = config.get("signal_type_long", "crossed_below")
                signal_type_short = config.get("signal_type_short", "crossed_above")
                
                if long_threshold is None or short_threshold is None:
                    self._log(
                        f"DGL-VNPY-ADAPTER|指标阈值未配置|pair={pair}|timeframe={timeframe}|"
                        f"indicator={signal_type}|long_threshold={long_threshold}|"
                        f"short_threshold={short_threshold}|跳过"
                    )
                    continue
                
                # 获取当前指标值
                current_value = self._get_indicator_value(pair, timeframe, signal_type, length=length)
                if current_value is None:
                    self._log(
                        f"DGL-VNPY-ADAPTER|指标值为空|pair={pair}|timeframe={timeframe}|"
                        f"indicator={signal_type}|length={length}|跳过本配置"
                    )
                    continue
                
                # 检测信号（根据 signal_type_long/short 动态判断）
                if not manager.is_short:
                    # 多头信号
                    if signal_type_long == "crossed_below":
                        # 需要上一个值来判断交叉
                        prev_key = (pair, timeframe, signal_type)
                        prev_value = self._prev_indicator_values.get(prev_key)
                        self._log(
                            f"DGL-VNPY-ADAPTER|多头信号检查(crossed_below)|pair={pair}|timeframe={timeframe}|"
                            f"indicator={signal_type}|prev={prev_value}|cur={current_value}|threshold={long_threshold}"
                        )
                        if prev_value is not None and prev_value > long_threshold and current_value <= long_threshold:
                            timeframe_triggered = True
                            triggered_signals.append({
                                "type": signal_type,
                                "timeframe": timeframe,
                                "value": current_value,
                                "threshold": long_threshold,
                                "direction": "long"
                            })
                        self._prev_indicator_values[prev_key] = current_value
                    elif signal_type_long == "less_than":
                        self._log(
                            f"DGL-VNPY-ADAPTER|多头信号检查(less_than)|pair={pair}|timeframe={timeframe}|"
                            f"indicator={signal_type}|cur={current_value}|threshold={long_threshold}"
                        )
                        if current_value < long_threshold:
                            timeframe_triggered = True
                            triggered_signals.append({
                                "type": signal_type,
                                "timeframe": timeframe,
                                "value": current_value,
                                "threshold": long_threshold,
                                "direction": "long"
                            })
                else:
                    # 空头信号
                    if signal_type_short == "crossed_above":
                        # 需要上一个值来判断交叉
                        prev_key = (pair, timeframe, signal_type)
                        prev_value = self._prev_indicator_values.get(prev_key)
                        self._log(
                            f"DGL-VNPY-ADAPTER|空头信号检查(crossed_above)|pair={pair}|timeframe={timeframe}|"
                            f"indicator={signal_type}|prev={prev_value}|cur={current_value}|threshold={short_threshold}"
                        )
                        if prev_value is not None and prev_value < short_threshold and current_value >= short_threshold:
                            timeframe_triggered = True
                            triggered_signals.append({
                                "type": signal_type,
                                "timeframe": timeframe,
                                "value": current_value,
                                "threshold": short_threshold,
                                "direction": "short"
                            })
                        self._prev_indicator_values[prev_key] = current_value
                    elif signal_type_short == "greater_than":
                        self._log(
                            f"DGL-VNPY-ADAPTER|空头信号检查(greater_than)|pair={pair}|timeframe={timeframe}|"
                            f"indicator={signal_type}|cur={current_value}|threshold={short_threshold}"
                        )
                        if current_value > short_threshold:
                            timeframe_triggered = True
                            triggered_signals.append({
                                "type": signal_type,
                                "timeframe": timeframe,
                                "value": current_value,
                                "threshold": short_threshold,
                                "direction": "short"
                            })
            
            # 如果这个时间框架没有触发，则多周期共振失败
            if not timeframe_triggered:
                self._log(
                    f"DGL-VNPY-ADAPTER|时间框架未触发共振|pair={pair}|timeframe={timeframe}"
                )
                all_timeframes_triggered = False
                break
        
        # 只有所有时间框架都触发时，才生成信号事件（多周期共振）
        if all_timeframes_triggered and triggered_signals:
            level_configs = manager.config.get_level_configs()
            if level_configs:
                first_level = level_configs[0]
                quote_amount = first_level.amount
                base_amount = quote_amount / current_price if current_price > 0 else 0
                
                # 生成信号事件
                entry_event = DynamicGridEntrySignalEvent.create(
                    is_short=manager.is_short,
                    base_amount=base_amount,
                    quote_amount=quote_amount,
                    target_price=current_price,
                    pair=pair,
                    current_time=current_time,
                )
                
                # 记录所有触发的信号信息
                signal_names = [f"{s['type']}_{s['timeframe']}" for s in triggered_signals]
                entry_event.data["signal_name"] = "+".join(signal_names)
                entry_event.data["custom_data"] = {
                    "triggered_signals": triggered_signals,
                    "resonance": True
                }
                signal_events.append(entry_event)
                
                self._log(
                    f"DGL-VNPY-ADAPTER|多周期共振信号|pair={pair}|"
                    f"方向={'空头' if manager.is_short else '多头'}|"
                    f"信号={entry_event.data['signal_name']}|"
                    f"价格={current_price}"
                )
        
        return signal_events
    
    def initialize_from_config(self, config: Dict[str, Any]) -> None:
        """从配置字典初始化网格系统
        
        Args:
            config: 配置字典，包含 pairs 和 signals 等配置
        """
        pairs_config = config.get("pairs", [])
        
        # 保存信号配置
        self.signals_config = config.get("signals", {})
        
        # 创建网格管理器
        for pair_config in pairs_config:
            grid_config = DynamicGridConfig(**pair_config)
            raw_pair = grid_config.pair
            pair = self._normalize_pair_key(raw_pair)
            manager = DynamicGridManager(grid_config)
            # 确保 Manager 使用规范化后的 pair 键，避免事件中的 pair 不匹配
            manager.pair = pair
            self.grid_managers[pair] = manager
            
            # 为每个启用的信号配置创建 ArrayManager 和 BarGenerator
            # 遍历所有信号配置，找出需要的时间框架
            timeframes = set()
            for signal_name in self.signals_config.keys():
                configs = self.get_signal_configs(signal_name)
                for cfg in configs:
                    if cfg.get("use_signal", False):
                        tf = cfg.get("timeframe", "1m")
                        timeframes.add(tf)
            
            # 为每个时间框架创建指标管理器
            for timeframe in timeframes:
                # 将时间框架转换为 BarGenerator 需要的分钟数
                minutes = self._timeframe_to_minutes(timeframe)
                if minutes is None:
                    continue
                
                # 创建 BarGenerator 和 ArrayManager
                if BarGenerator and ArrayManager:
                    # 创建回调函数（使用闭包捕获 pair 和 timeframe）
                    def create_timeframe_callback(p, tf):
                        def callback(bar: BarData):
                            self._process_timeframe_bar(bar, p, tf)
                        return callback
                    
                    # BarGenerator(on_bar, window, on_window_bar)
                    # on_bar: 1分钟K线回调（我们不需要，传个空函数）
                    # window: 时间窗口（分钟数）
                    # on_window_bar: 时间窗口K线回调（我们的指标计算回调）
                    bg = BarGenerator(
                        on_bar=lambda bar: None,  # 1分钟K线不需要处理
                        window=minutes,
                        on_window_bar=create_timeframe_callback(pair, timeframe)
                    )
                    am = ArrayManager(size=200)  # 足够大的缓冲区
                    self._indicator_managers[(pair, timeframe)] = (bg, am)
                    
                    self._log(
                        f"DGL-VNPY-ADAPTER|创建指标管理器|pair={pair}|timeframe={timeframe}|minutes={minutes}"
                    )
            
            self._log(
                f"DGL-VNPY-ADAPTER|初始化网格|配置对={raw_pair}|规范键={pair}|"
                f"{'空头' if grid_config.is_short else '多头'}"
            )
        
        self._log(
            f"DGL-VNPY-ADAPTER|配置加载完成|交易对数={len(self.grid_managers)}"
        )
    
    def initialize_from_file(self, config_path: str | Path) -> None:
        """从 JSON 文件加载配置并初始化
        
        Args:
            config_path: 配置文件路径
        """
        config_path = Path(config_path)
        if not config_path.is_absolute():
            config_path = (Path(__file__).parent.parent.parent.parent / config_path).resolve()
        
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        # 兼容两种格式：
        # 1）顶层就是 {"pairs": [...], "signals": {...}, ...}（适配器原生格式）
        # 2）Freqtrade 风格：{"dynamicgrid": { "pairs": [...], "signals": {...}, ... }, ...}
        if "pairs" in data:
            config = data
        elif "dynamicgrid" in data and isinstance(data["dynamicgrid"], dict):
            config = data["dynamicgrid"]
        else:
            raise ValueError("配置文件缺少 'pairs' 字段（或 'dynamicgrid.pairs' 字段）")

        self.initialize_from_config(config)
    
    def check_entry_signal(self, bar: BarData, vt_symbol: str) -> Optional[DynamicGridEntrySignalEvent]:
        """检查开仓信号（第一层开仓）
        
        注意：这个方法只检查信号，不处理数据更新。
        信号检查应该在策略层面进行，只有在没有持仓时才检查。
        
        Args:
            bar: vnpy BarData 对象
            vt_symbol: 交易对符号（如 "BTCUSDT.BINANCE"）
        
        Returns:
            DynamicGridEntrySignalEvent: 如果信号满足，返回信号事件；否则返回 None
        """
        # 提取并规范化交易对键
        pair = self._normalize_pair_key(vt_symbol)
        
        manager = self.grid_managers.get(pair)
        if not manager:
            return None
        
        if not manager.enabled:
            return None
        
        # 更新所有时间框架的 BarGenerator（用于计算多周期指标）
        for (p, tf), (bg, am) in self._indicator_managers.items():
            if p == pair and bg:
                bg.update_bar(bar)
        
        # 检查并生成信号事件（多周期共振）
        signal_events = self._check_signal(pair, bar.close_price, bar.datetime, manager)
        
        if signal_events:
            self._log(
                f"DGL-VNPY-ADAPTER|check_entry_signal|检测到信号事件|pair={pair}|信号事件数={len(signal_events)}|"
                f"信号类型={[type(e).__name__ for e in signal_events]}"
            )
            # 只返回第一个信号事件（通常只有一个）
            return signal_events[0] if signal_events else None
        
        return None
    
    def handle_data_update(self, bar: BarData, vt_symbol: str) -> List[Tuple[str, float, float, Optional[str]]]:
        """处理 K 线数据更新（用于后续加仓逻辑）
        
        注意：这个方法只处理数据更新，不检查开仓信号。
        开仓信号应该在策略层面检查（通过 check_entry_signal）。
        
        Args:
            bar: vnpy BarData 对象
            vt_symbol: 交易对符号（如 "BTCUSDT.BINANCE"）
        
        Returns:
            List[Tuple[str, float, float, Optional[str]]]: 订单操作列表
            每个元组格式: (action, price, volume, tag)
            action: "buy", "sell", "short", "cover"
            price: 目标价格
            volume: 订单数量（基础币数量）
            tag: 订单标签（可选）
        """
        # 提取并规范化交易对键
        pair = self._normalize_pair_key(vt_symbol)
        
        manager = self.grid_managers.get(pair)
        if not manager:
            self._log(f"DGL-VNPY-ADAPTER|handle_data_update|交易对未找到|pair={pair}|vt_symbol={vt_symbol}")
            return []
        
        if not manager.enabled:
            self._log(f"DGL-VNPY-ADAPTER|handle_data_update|网格未启用|pair={pair}")
            return []
        
        self._log(
            f"DGL-VNPY-ADAPTER|handle_data_update|收到K线|pair={pair}|"
            f"时间={format_china_time(bar.datetime)}|"
            f"O={bar.open_price}|H={bar.high_price}|L={bar.low_price}|C={bar.close_price}|V={bar.volume}"
        )
        
        # 更新所有时间框架的 BarGenerator（用于计算多周期指标）
        for (p, tf), (bg, am) in self._indicator_managers.items():
            if p == pair and bg:
                bg.update_bar(bar)
        
        # 创建数据更新事件（不检查信号，只处理数据更新）
        data_event = DynamicGridDataUpdateEvent.create(
            pair=pair,
            open_price=bar.open_price,
            high=bar.high_price,
            low=bar.low_price,
            close=bar.close_price,
            volume=bar.volume,
            current_time=bar.datetime,
        )
        
        # 处理数据更新事件并获取输出事件
        events = manager.process_event(data_event)
        
        # 统计事件类型
        event_types = {}
        for e in events:
            event_type = type(e).__name__
            event_types[event_type] = event_types.get(event_type, 0) + 1
        
        self._log(
            f"DGL-VNPY-ADAPTER|handle_data_update|事件处理完成|pair={pair}|"
            f"事件总数={len(events)}|事件类型={event_types}"
        )
        
        # 映射事件为 vnpy 订单操作
        orders = self._map_events_to_orders(pair, events, manager.is_short)
        
        if orders:
            self._log(
                f"DGL-VNPY-ADAPTER|handle_data_update|生成订单|pair={pair}|订单数={len(orders)}|"
                f"订单详情={[(o[0], o[1], o[2]) for o in orders]}"
            )
        else:
            self._log(f"DGL-VNPY-ADAPTER|handle_data_update|无订单生成|pair={pair}")
        
        return orders
    
    def handle_bar(self, bar: BarData, vt_symbol: str) -> List[Tuple[str, float, float, Optional[str]]]:
        """处理 K 线数据更新（兼容旧接口，内部调用 handle_data_update）
        
        注意：这个方法已废弃，建议使用 check_entry_signal 和 handle_data_update。
        保留此方法以保持向后兼容。
        
        Args:
            bar: vnpy BarData 对象
            vt_symbol: 交易对符号（如 "BTCUSDT.BINANCE"）
        
        Returns:
            List[Tuple[str, float, float, Optional[str]]]: 订单操作列表
        """
        return self.handle_data_update(bar, vt_symbol)
    
    def is_entry_or_add_event(self, event: Any) -> bool:
        """检查事件是否为开仓或加仓事件（用于策略层判断）
        
        Args:
            event: 事件对象
            
        Returns:
            bool: 如果是开仓或加仓事件返回 True，否则返回 False
        """
        from ...core.event import DynamicGridEntryTriggerEvent, DynamicGridAddTriggerEvent
        return isinstance(event, (DynamicGridEntryTriggerEvent, DynamicGridAddTriggerEvent))
    
    def get_level_configs_for_display(self, pair_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """获取层级配置信息（用于显示，不暴露核心类型）
        
        Args:
            pair_config: 交易对配置字典
            
        Returns:
            List[Dict]: 层级配置列表，每个元素包含 level_id, amount, op_ratio, tp_ratio
        """
        try:
            from ...core.config import DynamicGridConfig
            grid_config = DynamicGridConfig(**pair_config)
            level_configs = grid_config.get_level_configs()
            
            # 转换为字典列表，避免暴露核心类型
            result = []
            for level_config in level_configs:
                result.append({
                    "level_id": level_config.level_id,
                    "amount": level_config.amount,
                    "op_ratio": level_config.op_ratio,
                    "tp_ratio": level_config.tp_ratio,
                })
            return result
        except Exception:
            return []
    
    def _map_events_to_orders(
        self, 
        pair: str, 
        events: List[DynamicGridEvent], 
        is_short: bool
    ) -> List[Tuple[str, float, float, Optional[str]]]:
        """将 OUTBOUND 事件映射为 vnpy 订单操作
        
        Args:
            pair: 交易对
            events: 输出事件列表
            is_short: 是否做空
        
        Returns:
            List[Tuple[str, float, float, Optional[str]]]: 订单操作列表
            每个元组格式: (action, price, volume, tag)
        """
        orders = []
        
        # 优先处理全局退出事件
        global_exit_events = [
            e for e in events if isinstance(e, DynamicGridGlobalExitTriggerEvent)
        ]
        
        # 处理第一层平仓事件
        exit_events = [
            e for e in events if isinstance(e, DynamicGridExitTriggerEvent)
        ]
        
        # 处理后续层减仓事件
        reduce_events = [
            e for e in events if isinstance(e, DynamicGridReduceTriggerEvent)
        ]
        
        # 处理第一层开仓事件
        entry_events = [
            e for e in events if isinstance(e, DynamicGridEntryTriggerEvent)
        ]
        
        # 处理后续层加仓事件
        add_events = [
            e for e in events if isinstance(e, DynamicGridAddTriggerEvent)
        ]
        
        self._log(
            f"DGL-VNPY-ADAPTER|_map_events_to_orders|事件分类|pair={pair}|"
            f"开仓事件={len(entry_events)}|加仓事件={len(add_events)}|"
            f"平仓事件={len(exit_events)}|减仓事件={len(reduce_events)}|"
            f"全局退出={len(global_exit_events)}|总事件={len(events)}"
        )
        
        # 优先处理全局退出
        if global_exit_events:
            event = global_exit_events[0]
            data = event.data or {}
            target_price = float(data.get("target_price", 0))
            total_base_amount = float(data.get("total_base_amount", 0))
            
            # 安全检查：确保 target_price 有效
            if math.isnan(target_price) or math.isinf(target_price) or target_price <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：全局退出target_price无效|"
                    f"target_price={target_price}|跳过订单"
                )
                return orders
            
            # 方案3：优先使用实际持仓量，避免浮点数精度问题
            base_amount = None
            if self._get_position is not None:
                try:
                    actual_pos = self._get_position()
                    # 根据方向判断：多头持仓为正，空头持仓为负
                    if (is_short and actual_pos < 0) or (not is_short and actual_pos > 0):
                        base_amount = abs(actual_pos)
                        self._log(
                            f"DGL-VNPY-ADAPTER|全局退出使用实际持仓量|"
                            f"实际持仓={actual_pos}|使用数量={base_amount}|"
                            f"事件中的total_base_amount={total_base_amount}"
                        )
                except Exception as e:
                    self._log(
                        f"DGL-VNPY-ADAPTER|警告：获取实际持仓量失败|错误={e}|"
                        f"回退到使用事件中的total_base_amount"
                    )
            
            # 如果没有获取到实际持仓量，使用事件中的 total_base_amount
            if base_amount is None or base_amount == 0:
                base_amount = total_base_amount
            
            # 安全检查：确保 base_amount 有效
            if math.isnan(base_amount) or math.isinf(base_amount) or base_amount <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：全局退出base_amount无效|"
                    f"base_amount={base_amount}|事件中的total_base_amount={total_base_amount}|跳过订单"
                )
                return orders
            if is_short:
                # 空头：平仓用 buy
                orders.append(("buy", target_price, base_amount, f"exit:all:{event.reason}"))
            else:
                # 多头：平仓用 sell
                orders.append(("sell", target_price, base_amount, f"exit:all:{event.reason}"))
            
            self._log(
                f"DGL-VNPY-ADAPTER|全局退出|交易对={pair}|价格={target_price}|"
                f"数量={total_base_amount}|原因={event.reason}"
            )
            return orders
        
        # 处理第一层平仓事件
        if exit_events:
            event = exit_events[0]  # 只处理第一个
            data = event.data or {}
            target_price = float(data.get("target_price", 0))
            reason = data.get("reason", "exit")
            
            # 第一层平仓标签
            signal_name = data.get("signal_name", "")
            tag = DynamicGridTagUtils.build_exit_tag(1, reason if not signal_name else f"{reason}:{signal_name}")
            
            # 安全检查：先检查 target_price 是否有效
            if math.isnan(target_price) or math.isinf(target_price) or target_price <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：第一层平仓target_price无效|"
                    f"target_price={target_price}|跳过订单"
                )
                return orders
            
            # 方案3：优先使用实际持仓量，避免浮点数精度问题
            base_amount = None
            if self._get_position is not None:
                try:
                    actual_pos = self._get_position()
                    # 根据方向判断：多头持仓为正，空头持仓为负
                    if (is_short and actual_pos < 0) or (not is_short and actual_pos > 0):
                        base_amount = abs(actual_pos)
                        self._log(
                            f"DGL-VNPY-ADAPTER|第一层平仓使用实际持仓量|"
                            f"实际持仓={actual_pos}|使用数量={base_amount}|"
                            f"事件中的base_amount={data.get('base_amount', 0)}"
                        )
                except Exception as e:
                    self._log(
                        f"DGL-VNPY-ADAPTER|警告：获取实际持仓量失败|错误={e}|"
                        f"回退到使用事件中的base_amount"
                    )
            
            # 如果没有获取到实际持仓量，使用事件中的 base_amount
            if base_amount is None or base_amount == 0:
                base_amount = float(data.get("base_amount", 0))
                if base_amount == 0:
                    # 如果没有 base_amount，从 quote_amount 计算
                    quote_amount = float(data.get("quote_amount", 0))
                    if quote_amount > 0:
                        base_amount = quote_amount / target_price
            
            # 安全检查：确保 base_amount 有效
            if math.isnan(base_amount) or math.isinf(base_amount) or base_amount <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：第一层平仓base_amount无效|"
                    f"base_amount={base_amount}|quote_amount={data.get('quote_amount', 0)}|target_price={target_price}"
                )
                return orders  # 跳过无效订单
            
            if is_short:
                # 空头：平仓用 buy
                orders.append(("buy", target_price, base_amount, tag))
            else:
                # 多头：平仓用 sell
                orders.append(("sell", target_price, base_amount, tag))
            
            self._log(
                f"DGL-VNPY-ADAPTER|第一层平仓触发(限价单，价格=收盘价)|交易对={pair}|"
                f"限价={target_price}|数量={base_amount}|原因={reason}|注意：限价单价格设为收盘价，通常能立即成交"
            )
            return orders
        
        # 处理后续层减仓事件
        if reduce_events:
            event = reduce_events[0]  # 只处理第一个
            data = event.data or {}
            level_id = data.get("level_id", 2)
            target_price = float(data.get("target_price", 0))
            reason = data.get("reason", "reduce")
            
            # 后续层减仓标签
            tag = DynamicGridTagUtils.build_reduce_tag(level_id, reason)
            
            # 安全检查：先检查 target_price 是否有效
            if math.isnan(target_price) or math.isinf(target_price) or target_price <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：后续层减仓target_price无效|层级={level_id}|"
                    f"target_price={target_price}|跳过订单"
                )
                return orders
            
            # 方案3：对于减仓，优先使用事件中的 base_amount（因为减仓通常是部分平仓）
            # 但如果事件中的 base_amount 无效，尝试使用实际持仓量作为备选
            base_amount = float(data.get("base_amount", 0))
            if base_amount == 0:
                # 如果没有 base_amount，从 quote_amount 计算
                quote_amount = float(data.get("quote_amount", 0))
                if quote_amount > 0:
                    base_amount = quote_amount / target_price
                # 如果还是 0，且可以获取实际持仓量，使用实际持仓量作为备选
                elif self._get_position is not None:
                    try:
                        actual_pos = self._get_position()
                        if (is_short and actual_pos < 0) or (not is_short and actual_pos > 0):
                            base_amount = abs(actual_pos)
                            self._log(
                                f"DGL-VNPY-ADAPTER|后续层减仓使用实际持仓量|层级={level_id}|"
                                f"实际持仓={actual_pos}|使用数量={base_amount}"
                            )
                    except Exception as e:
                        self._log(
                            f"DGL-VNPY-ADAPTER|警告：获取实际持仓量失败|层级={level_id}|错误={e}"
                        )
            
            # 安全检查：确保 base_amount 有效
            if math.isnan(base_amount) or math.isinf(base_amount) or base_amount <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：后续层减仓base_amount无效|层级={level_id}|"
                    f"base_amount={base_amount}|quote_amount={data.get('quote_amount', 0)}|target_price={target_price}"
                )
                return orders  # 跳过无效订单
            
            if is_short:
                # 空头：减仓用 buy
                orders.append(("buy", target_price, base_amount, tag))
            else:
                # 多头：减仓用 sell
                orders.append(("sell", target_price, base_amount, tag))
            
            self._log(
                f"DGL-VNPY-ADAPTER|后续层减仓触发(限价单，价格=收盘价)|交易对={pair}|层级={level_id}|"
                f"限价={target_price}|原因={reason}|注意：限价单价格设为收盘价，通常能立即成交"
            )
            return orders
        
        # 处理第一层开仓事件
        if entry_events:
            event = entry_events[0]  # 只处理第一个
            data = event.data or {}
            target_price = float(data.get("target_price", 0))
            
            # 使用信号事件中的 signal_name 作为 reason
            signal_name = data.get("signal_name", "signal")
            tag = DynamicGridTagUtils.build_entry_tag(signal_name)
            
            # 安全检查：先检查 target_price 是否有效
            if math.isnan(target_price) or math.isinf(target_price) or target_price <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：第一层开仓target_price无效|"
                    f"target_price={target_price}|跳过订单"
                )
                return orders
            
            # 计算订单数量
            base_amount = float(data.get("base_amount", 0))
            if base_amount == 0:
                # 如果没有 base_amount，从 quote_amount 计算
                quote_amount = float(data.get("quote_amount", 0))
                if quote_amount > 0:
                    base_amount = quote_amount / target_price
            
            # 安全检查：确保 base_amount 有效
            if math.isnan(base_amount) or math.isinf(base_amount) or base_amount <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：第一层开仓base_amount无效|"
                    f"base_amount={base_amount}|quote_amount={data.get('quote_amount', 0)}|target_price={target_price}"
                )
                return orders  # 跳过无效订单
            
            if is_short:
                # 空头：开仓用 sell
                orders.append(("short", target_price, base_amount, tag))
            else:
                # 多头：开仓用 buy
                orders.append(("buy", target_price, base_amount, tag))
            
            self._log(
                f"DGL-VNPY-ADAPTER|第一层开仓触发(限价单，价格=收盘价)|交易对={pair}|限价={target_price}|信号={signal_name}|注意：限价单价格设为收盘价，通常能立即成交"
            )
            return orders
        
        # 处理后续层加仓事件
        if add_events:
            event = add_events[0]  # 只处理第一个
            data = event.data or {}
            level_id = data.get("level_id", 2)
            target_price = float(data.get("target_price", 0))
            
            # 后续层加仓标签
            tag = DynamicGridTagUtils.build_add_tag(level_id)
            
            # 安全检查：先检查 target_price 是否有效
            if math.isnan(target_price) or math.isinf(target_price) or target_price <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：后续层加仓target_price无效|层级={level_id}|"
                    f"target_price={target_price}|跳过订单"
                )
                return orders
            
            # 计算订单数量
            base_amount = float(data.get("base_amount", 0))
            if base_amount == 0:
                # 如果没有 base_amount，从 quote_amount 计算
                quote_amount = float(data.get("quote_amount", 0))
                if quote_amount > 0:
                    base_amount = quote_amount / target_price
            
            # 安全检查：确保 base_amount 有效
            if math.isnan(base_amount) or math.isinf(base_amount) or base_amount <= 0:
                self._log(
                    f"DGL-VNPY-ADAPTER|警告：后续层加仓base_amount无效|层级={level_id}|"
                    f"base_amount={base_amount}|quote_amount={data.get('quote_amount', 0)}|target_price={target_price}"
                )
                return orders  # 跳过无效订单
            
            if is_short:
                # 空头：加仓用 sell
                orders.append(("short", target_price, base_amount, tag))
            else:
                # 多头：加仓用 buy
                orders.append(("buy", target_price, base_amount, tag))
            
            self._log(
                f"DGL-VNPY-ADAPTER|加仓触发(限价单，价格=收盘价)|交易对={pair}|层级={level_id}|限价={target_price}|注意：限价单价格设为收盘价，通常能立即成交"
            )
            return orders
        
        return orders
    
    def handle_trade(self, trade: TradeData, vt_symbol: str, order_tag: Optional[str] = None) -> None:
        """处理成交数据
        
        Args:
            trade: vnpy TradeData 对象
            vt_symbol: 交易对符号
            order_tag: 订单标签（从订单中获取）
        """
        # 提取并规范化交易对键
        pair = self._normalize_pair_key(vt_symbol)
        
        manager = self.grid_managers.get(pair)
        if not manager:
            return
        
        # 从订单映射中获取层级ID
        level_id_from_order = self._order_to_level.get(trade.orderid, 1)
        reason_from_order = self._order_to_reason.get(trade.orderid, "")
        
        self._log(
            f"DGL-VNPY-ADAPTER|handle_trade开始|订单ID={trade.orderid}|"
            f"传入标签={order_tag}|订单映射level_id={level_id_from_order}|"
            f"价格={trade.price}|数量={trade.volume}"
        )
        
        # 如果没有标签，尝试从订单映射中获取
        if not order_tag:
            if level_id_from_order == 1:
                order_tag = DynamicGridTagUtils.build_entry_tag(reason_from_order or "signal")
            else:
                order_tag = DynamicGridTagUtils.build_add_tag(level_id_from_order)
            self._log(
                f"DGL-VNPY-ADAPTER|handle_trade|无标签，从订单映射构建|"
                f"构建标签={order_tag}|level_id={level_id_from_order}"
            )
        
        # 解析标签
        tag_type, tag_level_id, tag_reason = DynamicGridTagUtils.parse_tag(order_tag)
        
        self._log(
            f"DGL-VNPY-ADAPTER|handle_trade|标签解析结果|"
            f"标签={order_tag}|类型={tag_type}|标签level_id={tag_level_id}|"
            f"标签reason={tag_reason}|订单映射level_id={level_id_from_order}"
        )
        
        # 使用标签中的信息（如果有效）
        if tag_level_id is not None:
            level_id = tag_level_id
            self._log(
                f"DGL-VNPY-ADAPTER|handle_trade|使用标签中的level_id|"
                f"level_id={level_id}"
            )
        else:
            level_id = level_id_from_order
            self._log(
                f"DGL-VNPY-ADAPTER|handle_trade|标签无level_id，使用订单映射|"
                f"level_id={level_id}"
            )
        
        if tag_reason:
            reason = tag_reason
        else:
            reason = reason_from_order
        
        # 判断是加仓还是减仓
        is_entry = DynamicGridTagUtils.is_entry_tag(order_tag) or DynamicGridTagUtils.is_add_tag(order_tag)
        is_exit = DynamicGridTagUtils.is_exit_tag(order_tag) or DynamicGridTagUtils.is_reduce_tag(order_tag)
        is_global_exit = DynamicGridTagUtils.is_global_exit_tag(order_tag)
        
        # 计算成交金额
        base_amount = float(trade.volume)
        quote_amount = float(trade.price * trade.volume)
        executed_price = float(trade.price)
        
        # 创建成功事件
        if is_global_exit:
            event = DynamicGridGlobalExitSuccessEvent.create(
                is_short=manager.is_short,
                total_base_amount=base_amount,
                total_quote_amount=quote_amount,
                executed_price=executed_price,
                reason=reason or "global_exit",
                pair=pair,
                current_time=trade.datetime,
            )
        elif is_entry:
            self._log(
                f"DGL-VNPY-ADAPTER|handle_trade|创建AddSuccessEvent|"
                f"level_id={level_id}|价格={executed_price}|数量={base_amount}|"
                f"标签={order_tag}|订单ID={trade.orderid}|"
                f"标签解析level_id={tag_level_id}|订单映射level_id={level_id_from_order}"
            )
            event = DynamicGridAddSuccessEvent.create(
                level_id=level_id,
                is_short=manager.is_short,
                base_amount=base_amount,
                quote_amount=quote_amount,
                executed_price=executed_price,
                pair=pair,
                current_time=trade.datetime,
            )
        elif is_exit:
            event = DynamicGridReduceSuccessEvent.create(
                level_id=level_id,
                is_short=manager.is_short,
                base_amount=base_amount,
                quote_amount=quote_amount,
                executed_price=executed_price,
                reason=reason or "reduce",
                pair=pair,
                current_time=trade.datetime,
            )
        else:
            self._log(f"DGL-VNPY-ADAPTER|无法识别订单类型|标签={order_tag}")
            return
        
        # 处理事件前记录详细信息
        self._log(
            f"DGL-VNPY-ADAPTER|handle_trade|准备处理事件|"
            f"事件类型={event.event_type}|level_id={level_id}|"
            f"价格={executed_price}|数量={base_amount}|标签={order_tag}"
        )
        
        # 处理事件
        manager.process_event(event)
        
        self._log(
            f"DGL-VNPY-ADAPTER|成交处理|交易对={pair}|层级={level_id}|"
            f"基础数量={base_amount}|价格={executed_price}|标签={order_tag}"
        )
    
    def register_order(self, order_id: str, level_id: int, reason: str = "") -> None:
        """注册订单到层级映射
        
        Args:
            order_id: 订单ID
            level_id: 层级ID
            reason: 原因
        
        说明：
        - 回测环境：buy() 返回的订单ID可能是 'BACKTESTING.3'，但 trade.orderid 可能是 '3'
        - 实盘环境：订单ID由交易所生成，通常是纯数字或字母数字组合，不带前缀
        - 为了兼容两种情况，如果检测到 BACKTESTING. 前缀，同时注册两种格式
        """
        # 首先注册原始订单ID
        self._order_to_level[order_id] = level_id
        if reason:
            self._order_to_reason[order_id] = reason
        
        # 如果订单ID包含 BACKTESTING. 前缀（回测环境），也注册不带前缀的版本
        # 因为回测引擎的 trade.orderid 可能不带前缀
        if order_id.startswith("BACKTESTING."):
            order_id_without_prefix = order_id.replace("BACKTESTING.", "")
            self._order_to_level[order_id_without_prefix] = level_id
            if reason:
                self._order_to_reason[order_id_without_prefix] = reason
            self._log(
                f"DGL-VNPY-ADAPTER|register_order|回测环境：同时注册两种格式|"
                f"原始ID={order_id}|无前缀ID={order_id_without_prefix}|level_id={level_id}"
            )
        # 实盘环境：订单ID不带前缀，直接使用即可，无需额外处理

