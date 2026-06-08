import logging
import re
import pandas as pd
from datetime import datetime
from pandas import DataFrame
from freqtrade.strategy import IStrategy, informative
from freqtrade.persistence.trade_model import Trade

# 导入适配器和工具（包内相对导入）
from .adapter import DynamicGridFreqtradeAdapter
from ...core.event import format_china_time
from ...utils.tag_utils import DynamicGridTagUtils
import talib.abstract as ta
from technical import qtpylib

logger = logging.getLogger(__name__)


class DynamicGridStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1m"
    minimal_roi = {"0": 1.00}
    stoploss = -100.0  # 设置为 -100.0 禁用止损（杠杆 10.0 时计算出的止损价格为负数，几乎不会触发）
    trailing_stop = False
    position_adjustment_enable = True

    # 支持的时间框架
    SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
    
    # 指标计算函数映射（动态扩展）
    INDICATOR_CALCULATORS = {
        "rsi": lambda df, length: ta.RSI(df, timeperiod=length),
        "cci": lambda df, length: ta.CCI(df, timeperiod=length),
        # 可以在这里添加更多指标，如：
        # "macd": lambda df, fast, slow, signal: ta.MACD(df, fastperiod=fast, slowperiod=slow, signalperiod=signal),
        # "bbands": lambda df, length, std: ta.BBANDS(df, timeperiod=length, nbdevup=std, nbdevdn=std),
    }

    @property
    def plot_config(self):
        """动态生成绘图配置，根据实际配置的指标和时间框架"""
        plot_config = {
            # 主图：显示价格和关键指标
            'main_plot': {},
            # 子图：显示技术指标
            'subplots': {}
        }
        
        # 如果适配器已初始化，根据配置动态生成
        if hasattr(self, 'adapter') and self.adapter:
            # RSI 子图
            rsi_plots = {}
            rsi_configs = self.adapter.get_signal_configs("rsi")
            for rsi_cfg in rsi_configs:
                if rsi_cfg.get("use_signal", False):
                    timeframe = rsi_cfg.get("timeframe")
                    rsi_length = rsi_cfg.get("rsi_length", 14)
                    column_name = f"rsi_{rsi_length}_{timeframe}"
                    # 为不同时间框架使用不同颜色
                    colors = {
                        "1m": "blue",
                        "5m": "green",
                        "15m": "orange",
                        "1h": "purple",
                        "4h": "red"
                    }
                    color = colors.get(timeframe, "gray")
                    rsi_plots[column_name] = {
                        'color': color,
                        'type': 'line'
                    }
            
            if rsi_plots:
                plot_config['subplots']['RSI'] = rsi_plots
            
            # CCI 子图
            cci_plots = {}
            cci_configs = self.adapter.get_signal_configs("cci")
            for cci_cfg in cci_configs:
                if cci_cfg.get("use_signal", False):
                    timeframe = cci_cfg.get("timeframe")
                    cci_length = cci_cfg.get("cci_length", 14)
                    column_name = f"cci_{cci_length}_{timeframe}"
                    # 为不同时间框架使用不同颜色
                    colors = {
                        "1m": "blue",
                        "5m": "green",
                        "15m": "orange",
                        "1h": "purple",
                        "4h": "red"
                    }
                    color = colors.get(timeframe, "gray")
                    cci_plots[column_name] = {
                        'color': color,
                        'type': 'line'
                    }
            
            if cci_plots:
                plot_config['subplots']['CCI'] = cci_plots
        else:
            # 如果适配器未初始化，使用默认配置
            plot_config['subplots'] = {
                "RSI": {
                    'rsi_14_1m': {'color': 'blue'},
                },
                "CCI": {
                    'cci_14_1m': {'color': 'blue'},
                }
            }
        
        return plot_config

    def __init__(self, config: dict):
        super().__init__(config)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        self.adapter = DynamicGridFreqtradeAdapter(self)
        
        # 从freqtrade配置中获取dynamicgrid配置
        dynamicgrid_config = config.get("dynamicgrid", {})
        
        # 获取交易模式（从主配置或 add_config_files 中读取）
        # Freqtrade 会自动合并 add_config_files 中的配置到主配置
        trading_mode = config.get("trading_mode", "spot")
        
        self.adapter.initialize_from_config(dynamicgrid_config, trading_mode)
        self.logger.info("DGL-STRATEGY|初始化")

        # 验证多周期配置中的时间框架
        self._validate_signal_configs()

    def _validate_signal_configs(self) -> None:
        """验证信号配置中的时间框架"""
        cci_configs = self.adapter.get_signal_configs("cci")
        rsi_configs = self.adapter.get_signal_configs("rsi")
        
        all_timeframes = []
        for cfg in cci_configs:
            if cfg.get("use_signal", False):
                tf = cfg.get("timeframe")
                if tf:
                    all_timeframes.append(tf)
        
        for cfg in rsi_configs:
            if cfg.get("use_signal", False):
                tf = cfg.get("timeframe")
                if tf:
                    all_timeframes.append(tf)
        
        # 验证所有时间框架
        invalid_tfs = [tf for tf in all_timeframes if tf not in self.SUPPORTED_TIMEFRAMES]
        if invalid_tfs:
            self.logger.error(
                f"DGL-STRATEGY|不支持的时间框架|时间框架={invalid_tfs}|支持列表={self.SUPPORTED_TIMEFRAMES}"
            )
            raise ValueError(f"不支持的时间框架: {invalid_tfs}")
        
        self.logger.debug(
            f"DGL-STRATEGY|配置验证通过|CCI配置数={len([c for c in cci_configs if c.get('use_signal', False)])}|RSI配置数={len([c for c in rsi_configs if c.get('use_signal', False)])}"
        )

    def _has_enabled_signal_for_timeframe(self, timeframe: str) -> bool:
        """该 timeframe 是否存在任何启用(use_signal=true)的指标配置。

        说明：
        - `@informative()` 的 DataFrame 准备/merge 开销无法完全避免，
          但可以通过短路跳过 TA-Lib 指标计算与遍历日志，明显降低 CPU 开销。
        """
        if not timeframe:
            return False
        if not hasattr(self, "adapter") or not self.adapter:
            return False

        # 检查该时间框架是否有启用的信号配置
        for indicator_key in ("rsi", "cci"):
            configs = self.adapter.get_signal_configs(indicator_key)
            for cfg in configs:
                if cfg.get("use_signal", False) and cfg.get("timeframe") == timeframe:
                    return True
        return False

    def bot_start(self, **kwargs) -> None:
        """初始化策略"""
        self.logger.info("DGL-STRATEGY|策略启动")
        # 在数据库会话可用后恢复网格状态
        self.adapter.restore_all_grid_states()

    def _calculate_indicators_for_timeframe(self, dataframe: DataFrame, metadata: dict, timeframe: str) -> DataFrame:
        """为指定时间框架计算所有启用的指标（动态解析）"""
        pair = metadata["pair"]
        self.logger.debug(f"DGL-STRATEGY|开始计算指标|交易对={pair}|时间框架={timeframe}|数据行数={len(dataframe)}")
        
        # 动态遍历所有信号配置
        signals = getattr(self.adapter, "signals", {}) or {}
        for indicator_type, configs in signals.items():
            if indicator_type not in self.INDICATOR_CALCULATORS:
                self.logger.warning(f"DGL-STRATEGY|未知指标类型|指标类型={indicator_type}|跳过")
                continue
            
            calculator = self.INDICATOR_CALCULATORS[indicator_type]
            configs_list = self.adapter.get_signal_configs(indicator_type)
            
            self.logger.debug(f"DGL-STRATEGY|{indicator_type.upper()}配置总数={len(configs_list)}|时间框架={timeframe}")
            
            for idx, cfg in enumerate(configs_list):
                cfg_timeframe = cfg.get("timeframe")
                use_signal = cfg.get("use_signal", False)
                
                self.logger.debug(
                    f"DGL-STRATEGY|{indicator_type.upper()}配置[{idx}]|时间框架={cfg_timeframe}|use_signal={use_signal}|"
                    f"目标时间框架={timeframe}|是否匹配={cfg_timeframe == timeframe}"
                )
                
                if use_signal and cfg_timeframe == timeframe:
                    # 获取指标参数（动态获取，支持不同指标的不同参数名）
                    length = cfg.get(f"{indicator_type}_length", cfg.get("length", 14))
                    
                    # 列名不包含时间框架，@informative 装饰器会自动添加时间框架后缀
                    # 最终列名格式: {indicator_type}_{length}_{timeframe}
                    column_name = f"{indicator_type}_{length}"
                    
                    try:
                        # 动态调用计算函数
                        dataframe[column_name] = calculator(dataframe, length)
                        indicator_value = dataframe[column_name].iloc[-1] if not dataframe.empty else None
                        self.logger.debug(
                            f"DGL-STRATEGY|✓计算{indicator_type.upper()}指标成功|交易对={pair}|时间框架={timeframe}|"
                            f"长度={length}|列名={column_name}|值={round(indicator_value, 2) if indicator_value is not None else None}|"
                            f"数据行数={len(dataframe)}"
                        )
                    except Exception as e:
                        self.logger.error(
                            f"DGL-STRATEGY|✗计算{indicator_type.upper()}指标失败|交易对={pair}|时间框架={timeframe}|"
                            f"长度={length}|错误={e}"
                        )
                elif use_signal:
                    self.logger.debug(
                        f"DGL-STRATEGY|跳过{indicator_type.upper()}配置[{idx}]|配置时间框架={cfg_timeframe}|当前时间框架={timeframe}"
                    )
        
        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算 4h 时间框架的指标"""
        if not self._has_enabled_signal_for_timeframe("4h"):
            return dataframe
        return self._calculate_indicators_for_timeframe(dataframe, metadata, "4h")

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算 1h 时间框架的指标"""
        if not self._has_enabled_signal_for_timeframe("1h"):
            return dataframe
        return self._calculate_indicators_for_timeframe(dataframe, metadata, "1h")

    @informative("15m")
    def populate_indicators_15m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算 15m 时间框架的指标"""
        if not self._has_enabled_signal_for_timeframe("15m"):
            return dataframe
        return self._calculate_indicators_for_timeframe(dataframe, metadata, "15m")

    @informative("5m")
    def populate_indicators_5m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算 5m 时间框架的指标"""
        if not self._has_enabled_signal_for_timeframe("5m"):
            return dataframe
        return self._calculate_indicators_for_timeframe(dataframe, metadata, "5m")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """计算主时间框架的指标"""
        pair = metadata["pair"]
        self.logger.debug(f"DGL-STRATEGY|主时间框架指标计算|交易对={pair}|时间框架={self.timeframe}|可用列数={len(dataframe.columns)}")
        result = self._calculate_indicators_for_timeframe(dataframe, metadata, self.timeframe)
        
        # 主时间框架的指标列名不会自动添加后缀，需要手动添加以保持与其他时间框架一致
        # 只重命名主时间框架自己计算的列（格式为 cci_数字 或 rsi_数字，没有其他后缀）
        # 其他时间框架的列已经有后缀了（如 rsi_14_5m），不应该再次重命名
        rename_dict = {}
        for col in result.columns:
            # 检查是否是指标列，且格式为 cci_数字 或 rsi_数字（没有其他后缀）
            # 使用正则表达式匹配：^cci_\d+$ 或 ^rsi_\d+$
            if re.match(r'^(cci|rsi)_\d+$', col):
                new_col_name = f"{col}_{self.timeframe}"
                rename_dict[col] = new_col_name
                self.logger.debug(
                    f"DGL-STRATEGY|重命名主时间框架列|交易对={pair}|原列名={col}|新列名={new_col_name}"
                )
        
        if rename_dict:
            result = result.rename(columns=rename_dict)
            self.logger.debug(
                f"DGL-STRATEGY|主时间框架列重命名完成|交易对={pair}|重命名数量={len(rename_dict)}"
            )
        
        # 打印所有列名，帮助排查
        all_columns = list(result.columns)
        self.logger.debug(f"DGL-STRATEGY|主时间框架计算完成|交易对={pair}|总列数={len(all_columns)}|列名={all_columns}")
        return result

    def _build_signal_condition(self, dataframe: DataFrame, indicator_type: str, cfg: dict, is_long: bool) -> tuple:
        """构建单个指标的信号条件
        
        Args:
            dataframe: DataFrame数据
            indicator_type: 指标类型（"cci" 或 "rsi"）
            cfg: 配置字典
            is_long: 是否为多头信号
        
        Returns:
            (condition, indicator_description, current_value): 条件Series、描述字符串和当前值
        """
        timeframe = cfg.get("timeframe")
        
        self.logger.debug(
            f"DGL-STRATEGY|开始构建信号条件|指标类型={indicator_type}|时间框架={timeframe}|"
            f"方向={'多头' if is_long else '空头'}|数据列数={len(dataframe.columns)}"
        )
        
        # 优先使用分别配置的多空信号类型，向后兼容统一的signal_type
        if is_long:
            signal_type = cfg.get("signal_type_long", cfg.get("signal_type", "crossed_above"))
        else:
            signal_type = cfg.get("signal_type_short", cfg.get("signal_type", "crossed_below"))
        
        self.logger.debug(f"DGL-STRATEGY|信号类型|指标={indicator_type}|时间框架={timeframe}|信号类型={signal_type}")
        
        # 动态获取指标参数（支持不同指标的不同参数名）
        length = cfg.get(f"{indicator_type}_length", cfg.get("length", 14))
        long_threshold = cfg.get(f"{indicator_type}_long_threshold", cfg.get("long_threshold", None))
        short_threshold = cfg.get(f"{indicator_type}_short_threshold", cfg.get("short_threshold", None))
        
        if long_threshold is None or short_threshold is None:
            self.logger.warning(
                f"DGL-STRATEGY|指标阈值未配置|指标类型={indicator_type}|"
                f"long_threshold={long_threshold}|short_threshold={short_threshold}"
            )
            return None, f"{indicator_type.upper()}[{timeframe}]阈值未配置", None
        
        # @informative 装饰器会自动添加时间框架后缀，格式为: {column}_{timeframe}
        # 所以列名格式为: {indicator_type}_{length}_{timeframe}
        base_column_name = f"{indicator_type}_{length}"
        column_name = f"{base_column_name}_{timeframe}"
        threshold = long_threshold if is_long else short_threshold
        
        self.logger.debug(
            f"DGL-STRATEGY|列名构建|指标={indicator_type}|时间框架={timeframe}|"
            f"基础列名={base_column_name}|期望列名={column_name}|阈值={threshold}"
        )
        
        # 打印所有相关列名（用于调试）
        relevant_columns = [col for col in dataframe.columns if base_column_name in col]
        self.logger.debug(
            f"DGL-STRATEGY|相关列名查找|指标={indicator_type}|时间框架={timeframe}|"
            f"找到的相关列={relevant_columns}|期望列名={column_name}|列是否存在={column_name in dataframe.columns}"
        )
        
        # 检查列是否存在（现在所有时间框架的列名都统一带后缀）
        if column_name not in dataframe.columns:
            # 尝试查找所有可能的列名变体（向后兼容）
            possible_names = [
                column_name,  # {column}_{timeframe} (统一格式)
                base_column_name,  # {column} (向后兼容，如果存在)
            ]
            found = False
            for name in possible_names:
                if name in dataframe.columns:
                    column_name = name
                    found = True
                    self.logger.debug(
                        f"DGL-STRATEGY|找到替代列名|指标={indicator_type}|时间框架={timeframe}|"
                        f"使用列名={column_name}"
                    )
                    break
            if not found:
                all_columns = list(dataframe.columns)
                self.logger.error(
                    f"DGL-STRATEGY|✗列名不存在|指标={indicator_type}|时间框架={timeframe}|"
                    f"期望列名={column_name}|尝试的列名={possible_names}|所有列名={all_columns}"
                )
                return None, f"{indicator_type.upper()}[{timeframe}]列不存在，期望列名: {column_name}", None
        else:
            self.logger.debug(
                f"DGL-STRATEGY|✓列名存在|指标={indicator_type}|时间框架={timeframe}|列名={column_name}"
            )
        
        # 获取当前值用于日志
        current_value = dataframe[column_name].iloc[-1] if not dataframe.empty and len(dataframe) > 0 else None
        self.logger.debug(
            f"DGL-STRATEGY|列数据检查|指标={indicator_type}|时间框架={timeframe}|"
            f"列名={column_name}|当前值={round(current_value, 2) if current_value is not None else None}|"
            f"数据行数={len(dataframe)}|是否有NaN={dataframe[column_name].isna().any() if current_value is not None else 'N/A'}"
        )
        
        # 根据信号类型构建条件
        if signal_type == "crossed_above":
            condition = qtpylib.crossed_above(dataframe[column_name], threshold)
            desc = f"{indicator_type.upper()}[{timeframe}](crossed_above: {threshold})"
        elif signal_type == "crossed_below":
            condition = qtpylib.crossed_below(dataframe[column_name], threshold)
            desc = f"{indicator_type.upper()}[{timeframe}](crossed_below: {threshold})"
        elif signal_type == "less_than":
            condition = dataframe[column_name] < threshold
            desc = f"{indicator_type.upper()}[{timeframe}](less_than: {threshold})"
        elif signal_type == "greater_than":
            condition = dataframe[column_name] > threshold
            desc = f"{indicator_type.upper()}[{timeframe}](greater_than: {threshold})"
        else:
            # 默认使用 crossed_above（多头）或 crossed_below（空头）
            if is_long:
                condition = qtpylib.crossed_above(dataframe[column_name], threshold)
                desc = f"{indicator_type.upper()}[{timeframe}](crossed_above: {threshold})"
            else:
                condition = qtpylib.crossed_below(dataframe[column_name], threshold)
                desc = f"{indicator_type.upper()}[{timeframe}](crossed_below: {threshold})"
        
        # 检查条件结果
        condition_true_count = condition.sum() if hasattr(condition, 'sum') else 0
        last_condition = condition.iloc[-1] if not condition.empty and len(condition) > 0 else False
        
        desc = f"{desc}, value={round(current_value, 2) if current_value is not None else None}, last_condition={last_condition}"

        self.logger.debug(
            f"DGL-STRATEGY|✓条件构建完成|指标={indicator_type}|时间框架={timeframe}|"
            f"信号类型={signal_type}|阈值={threshold}|当前值={round(current_value, 2) if current_value is not None else None}|"
            f"条件为True的数量={condition_true_count}|最后一根K线满足={last_condition}"
        )

        return condition, desc, current_value

    def _find_add_order_by_level(self, trade: Trade, level_id: int):
        """查找指定层级的未配对加仓订单（支持同一层级多次开平仓）
        
        使用栈（LIFO）配对逻辑：
        - 收集所有该层级的加仓订单和减仓订单
        - 按时间顺序排序
        - 使用栈配对：加仓入栈，减仓出栈
        - 返回栈顶的未配对加仓订单（最近一次未平仓的加仓订单）
        
        Args:
            trade: 交易对象
            level_id: 层级ID
        
        Returns:
            找到的未配对加仓订单对象，如果未找到则返回 None
        """
        if not trade or not hasattr(trade, 'orders'):
            return None
        
        # 查找标签为 add:{level_id} 和 reduce:{level_id} 的订单（也支持 entry:1 和 exit:1/exit:all）
        add_tag_prefix = DynamicGridTagUtils.get_add_tag_prefix(level_id)
        reduce_tag_prefix = DynamicGridTagUtils.get_reduce_tag_prefix(level_id)
        
        # 收集所有相关订单（只收集已成交的订单）
        add_orders = []
        reduce_orders = []
        
        for order in trade.orders:
            order_tag = getattr(order, 'ft_order_tag', None) or ""
            order_status = getattr(order, 'status', '').lower() if hasattr(order, 'status') else ''
            
            # 只处理已成交的订单
            if order_status not in ('closed', 'filled') or not order.safe_filled or order.safe_filled <= 0:
                continue
            
            # 判断订单类型
            # 支持 entry:1 和 add:l{level_id} 标签
            if (order_tag.startswith(add_tag_prefix) or 
                (level_id == 1 and DynamicGridTagUtils.is_entry_tag(order_tag))):
                # 获取订单成交时间用于排序
                order_time = order.order_filled_date or order.order_date
                add_orders.append((order_time, order))
            # 支持 exit:1, exit:all 和 reduce:l{level_id} 标签
            elif (order_tag.startswith(reduce_tag_prefix) or 
                  DynamicGridTagUtils.is_exit_tag(order_tag)):
                order_time = order.order_filled_date or order.order_date
                reduce_orders.append((order_time, order))
        
        # 合并所有订单并按时间排序（使用栈 LIFO 配对逻辑）
        all_orders = []
        for add_time, add_order in add_orders:
            all_orders.append(('add', add_time, add_order))
        for reduce_time, reduce_order in reduce_orders:
            all_orders.append(('reduce', reduce_time, reduce_order))
        
        # 按时间排序
        all_orders.sort(key=lambda x: x[1] if x[1] else datetime.min)
        
        self.logger.debug(
            f"DGL-STRATEGY|订单配对|层级={level_id}|加仓订单数={len(add_orders)}|减仓订单数={len(reduce_orders)}|总订单数={len(all_orders)}"
        )
        
        # 使用栈（LIFO）配对逻辑：加仓入栈，减仓出栈
        unmatched_add_stack = []
        
        for order_type, order_time, order in all_orders:
            if order_type == 'add':
                # 加仓订单：入栈
                unmatched_add_stack.append(order)
                self.logger.debug(
                    f"DGL-STRATEGY|加仓订单入栈|层级={level_id}|订单ID={order.order_id}|"
                    f"成交时间={order_time}|栈大小={len(unmatched_add_stack)}"
                )
            elif order_type == 'reduce':
                # 减仓订单：出栈（配对最近的加仓订单）
                if unmatched_add_stack:
                    matched_add = unmatched_add_stack.pop()
                    self.logger.debug(
                        f"DGL-STRATEGY|订单配对成功|层级={level_id}|加仓订单={matched_add.order_id}|"
                        f"减仓订单={order.order_id}|加仓时间={matched_add.order_filled_date or matched_add.order_date}|"
                        f"减仓时间={order_time}|栈大小={len(unmatched_add_stack)}"
                    )
                else:
                    self.logger.warning(
                        f"DGL-STRATEGY|减仓订单无配对|层级={level_id}|订单ID={order.order_id}|"
                        f"减仓时间={order_time}"
                    )
        
        # 返回栈顶的未配对加仓订单（最近一次未平仓的）
        if unmatched_add_stack:
            latest_add_order = unmatched_add_stack[-1]  # 栈顶（最近的未配对加仓订单）
            self.logger.info(
                f"DGL-STRATEGY|找到未配对加仓订单|层级={level_id}|订单ID={latest_add_order.order_id}|"
                f"成交数量={latest_add_order.safe_filled}|成交价格={latest_add_order.safe_price}|"
                f"未配对加仓订单数={len(unmatched_add_stack)}"
            )
            return latest_add_order
        
        self.logger.warning(
            f"DGL-STRATEGY|未找到未配对加仓订单|层级={level_id}|交易ID={trade.id}|"
            f"加仓订单数={len(add_orders)}|减仓订单数={len(reduce_orders)}"
        )
        return None

    def _build_signal_summary(self, signal_info_list: list) -> str:
        """构建信号摘要，用于标签
        
        Args:
            signal_info_list: 信号信息列表，每个元素为 (indicator_type, timeframe, current_value)
                如 [("cci", "1m", -111.23), ("rsi", "5m", 27.45)]
        
        Returns:
            如果 detailed_signal_summary=True: "cci_1m_-111:rsi_5m_27"
            如果 detailed_signal_summary=False: "signal"
        """
        if not signal_info_list:
            return ""
        
        # 检查是否使用详细信号摘要
        detailed = getattr(self.adapter, 'detailed_signal_summary', True)
        
        if not detailed:
            # 简化模式：直接返回 "signal"
            return "signal"
        
        # 详细模式：格式化每个信号：指标类型_时间框架_值（四舍五入到整数）
        signal_parts = []
        for indicator_type, timeframe, current_value in signal_info_list:
            if current_value is not None:
                # 四舍五入到整数
                value_int = int(round(current_value))
                signal_parts.append(f"{indicator_type.lower()}_{timeframe}_{value_int}")
        
        # 按指标类型和时间框架排序，确保一致性
        signal_parts = sorted(signal_parts)
        # 用冒号连接
        return ":".join(signal_parts) if signal_parts else ""

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """生成首次开仓信号，要求所有启用指标条件（use_signal=true）同时满足（多周期共振）"""
        pair = metadata["pair"]
        
        self.logger.debug("=" * 80)
        self.logger.debug(f"DGL-STRATEGY|开始检查开仓信号|交易对={pair}|数据行数={len(dataframe)}")
        
        # 打印所有可用的列名（用于调试）
        all_columns = list(dataframe.columns)
        indicator_columns = [col for col in all_columns if any(x in col for x in ['cci', 'rsi', 'rsi_', 'cci_'])]
        self.logger.debug(
            f"DGL-STRATEGY|可用列名|总列数={len(all_columns)}|指标相关列={indicator_columns}"
        )

        # 检查该交易对是否有启用的网格管理器
        manager = self.adapter.grid_managers.get(pair)
        if not manager or not manager.enabled:
            self.logger.debug(f"DGL-STRATEGY|交易对无启用网格管理器|交易对={pair}")
            return dataframe

        # 根据网格配置的 is_short 决定检查哪个方向的信号
        is_short = manager.is_short
        self.logger.debug(
            f"DGL-STRATEGY|检查开仓信号|交易对={pair}|方向={'空头' if is_short else '多头'}"
        )

        # 构建动态条件
        long_conditions = []
        short_conditions = []
        enabled_indicators = []  # 用于日志
        signal_info_list = []  # 用于生成信号摘要：(indicator_type, timeframe, current_value)

        # 动态遍历所有信号配置
        signals = getattr(self.adapter, "signals", {}) or {}
        for indicator_type, configs in signals.items():
            if indicator_type not in self.INDICATOR_CALCULATORS:
                self.logger.warning(f"DGL-STRATEGY|未知指标类型|指标类型={indicator_type}|跳过")
                continue
            
            indicator_configs = self.adapter.get_signal_configs(indicator_type)
            self.logger.debug(f"DGL-STRATEGY|{indicator_type.upper()}配置总数={len(indicator_configs)}")
            
            for idx, cfg in enumerate(indicator_configs):
                use_signal = cfg.get("use_signal", False)
                cfg_timeframe = cfg.get("timeframe")
                
                self.logger.debug(
                    f"DGL-STRATEGY|{indicator_type.upper()}配置[{idx}]|时间框架={cfg_timeframe}|use_signal={use_signal}"
                )
                
                if use_signal:
                    # 在配置中添加 pair 信息用于日志
                    cfg_with_pair = cfg.copy()
                    cfg_with_pair["_pair"] = pair
                    condition, desc, current_value = self._build_signal_condition(dataframe, indicator_type, cfg_with_pair, not is_short)
                    
                    if condition is not None:
                        direction_str = "多头" if not is_short else "空头"
                        if not is_short:  # 多头网格
                            long_conditions.append(condition)
                            enabled_indicators.append(f"{indicator_type.upper()}{direction_str}-{desc}")
                            signal_info_list.append((indicator_type, cfg_timeframe, current_value))
                            self.logger.debug(f"DGL-STRATEGY|✓{indicator_type.upper()}条件已添加({direction_str})|配置[{idx}]|{desc}")
                        else:  # 空头网格
                            short_conditions.append(condition)
                            enabled_indicators.append(f"{indicator_type.upper()}{direction_str}-{desc}")
                            signal_info_list.append((indicator_type, cfg_timeframe, current_value))
                            self.logger.debug(f"DGL-STRATEGY|✓{indicator_type.upper()}条件已添加({direction_str})|配置[{idx}]|{desc}")
                    else:
                        self.logger.warning(
                            f"DGL-STRATEGY|✗{indicator_type.upper()}条件构建失败|交易对={pair}|配置[{idx}]={cfg}|描述={desc}"
                        )
                else:
                    self.logger.debug(f"DGL-STRATEGY|跳过{indicator_type.upper()}配置[{idx}]|use_signal=False")

        # 如果没有启用任何指标，禁止开仓
        if not enabled_indicators:
            self.logger.warning(f"DGL-STRATEGY|无启用指标禁止开仓|交易对={pair}")
            return dataframe

        self.logger.debug(f"DGL-STRATEGY|启用指标汇总|指标列表={', '.join(enabled_indicators)}")
        self.logger.debug(
            f"DGL-STRATEGY|条件统计|多头条件数={len(long_conditions)}|空头条件数={len(short_conditions)}"
        )

        # 根据网格方向设置对应的开仓信号（所有条件必须同时满足）
        if not is_short and long_conditions:  # 多头网格
            cond_long = pd.concat(long_conditions, axis=1).all(axis=1)
            true_count = cond_long.sum()
            last_condition = cond_long.iloc[-1] if not cond_long.empty else False
            
            # 构建包含信号信息的开仓标签
            # 格式: entry:1:{reason}，reason是信号摘要（包含具体值）
            # 信号摘要: 如 "cci_1m_-111:rsi_5m_27"
            signal_summary = self._build_signal_summary(signal_info_list)
            enter_tag = DynamicGridTagUtils.build_entry_tag(signal_summary)  # signal_summary作为reason
            
            dataframe.loc[cond_long, ["enter_long", "enter_tag"]] = (1, enter_tag)
            self.logger.debug(
                f"DGL-STRATEGY|多头开仓信号|交易对={pair}|满足条件数={len(long_conditions)}|"
                f"同时满足的K线数={true_count}|最后一根K线满足={last_condition}|标签={enter_tag}"
            )
        elif is_short and short_conditions:  # 空头网格
            cond_short = pd.concat(short_conditions, axis=1).all(axis=1)
            true_count = cond_short.sum()
            last_condition = cond_short.iloc[-1] if not cond_short.empty else False
            
            # 构建包含信号信息的开仓标签
            # 格式: entry:1:{reason}，reason是信号摘要（包含具体值）
            # 信号摘要: 如 "cci_1m_111:rsi_5m_73"
            signal_summary = self._build_signal_summary(signal_info_list)
            enter_tag = DynamicGridTagUtils.build_entry_tag(signal_summary)  # signal_summary作为reason
            
            dataframe.loc[cond_short, ["enter_short", "enter_tag"]] = (1, enter_tag)
            self.logger.debug(
                f"DGL-STRATEGY|空头开仓信号|交易对={pair}|满足条件数={len(short_conditions)}|"
                f"同时满足的K线数={true_count}|最后一根K线满足={last_condition}|标签={enter_tag}"
            )
        
        self.logger.debug("=" * 80)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        计算出场信号。
        """
        return dataframe

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        """使用网格第一层的配置作为首仓仓位（以 quote/stake 计）。

        - is_base_mode == False（U本位）: 直接取 config.amount[0]，表示 quote 货币金额
        - is_base_mode == True（币本位）: 用 base_amount * current_rate 转为 stake 金额
        - 最后夹在 [min_stake, max_stake] 范围内
        - 若无法获取配置，则回退 proposed_stake
        """
        manager = self.adapter.grid_managers.get(pair)
        if not manager or not manager.config:
            return proposed_stake

        cfg = manager.config
        try:
            first_amount = float(cfg.amount[0]) if cfg.amount else 0.0
            is_base_mode = getattr(cfg, 'is_base_mode', False)
            
            if is_base_mode:
                # 币本位：config.amount 是 base 数量，需要转换为 stake（quote 金额）
                stake = first_amount * float(current_rate)
                self.logger.info(
                    f"DGL-STRATEGY|首仓计算|交易对={pair}|币本位=True|"
                    f"base数量={first_amount}|价格={current_rate}|stake={stake}"
                )
            else:
                # U本位（默认）：config.amount 就是 stake（quote 金额）
                stake = first_amount
                self.logger.info(
                    f"DGL-STRATEGY|首仓计算|交易对={pair}|币本位=False|stake={stake}"
                )
            
            # clamp to exchange constraints
            if min_stake is not None:
                stake = max(stake, float(min_stake))
            stake = min(stake, float(max_stake))
            return stake
        except Exception:
            return proposed_stake

    def order_filled(
        self, pair: str, trade: Trade, order, current_time: datetime, **kwargs
    ) -> None:
        """订单成交回调：使用真实成交驱动网格 SuccessEvent"""
        self.adapter.on_order_filled(pair=pair, trade=trade, order=order, current_time=current_time)

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> float | None | tuple[float | None, str | None]:
        """动态调整仓位并生成成功事件"""
        pair = trade.pair
        is_short = bool(trade.is_short)
        
        # 添加 INFO 级别日志，确认方法被调用
        time_str = format_china_time(current_time)
        self.logger.debug(
            f"DGL-STRATEGY|检查仓位调整|交易对={pair}|交易ID={trade.id}|方向={'做空' if is_short else '做多'}|"
            f"当前价格={current_rate}|当前盈亏={current_profit:.4%}|K线时间={time_str}"
        )
        
        dataframe, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if not dataframe.empty:
            current_candle = dataframe.iloc[-1].to_dict()
        else:
            current_candle = None
            self.logger.warning(f"DGL-STRATEGY|无法获取K线数据|交易对={pair}")

        adj = self.adapter.handle_data_update(
            pair=pair,
            current_time=current_time,
            current_rate=current_rate,
            current_profit=current_profit,
            last_candle=current_candle or {},
            trade=trade,
        )
        if adj is None:
            self.logger.debug(
                f"DGL-STRATEGY|无需调整仓位|交易对={pair}|方向={'做空' if is_short else '做多'}|"
                f"当前价格={current_rate}|K线时间={time_str}"
            )
            return None
        # 允许 (amount, tag) 或 amount
        if isinstance(adj, tuple):
            amount, tag = adj
            
            # 记录适配器返回的原始值
            tag_type, level_id, extra = DynamicGridTagUtils.parse_tag(tag)
            
            # 处理减仓标签（reduce:l{level_id}，level_id >= 2）
            # 直接使用适配器返回的 quote_amount，因为 DynamicGridLevel 已经存储了正确的开仓金额
            # Freqtrade 会使用比例计算来确定实际减仓数量
            if DynamicGridTagUtils.is_reduce_tag(tag):
                self.logger.info(
                    f"DGL-STRATEGY|适配器返回减仓|交易对={pair}|交易ID={trade.id}|"
                    f"金额={amount}|标签={tag}|层级ID={level_id}"
                )
            # 处理平仓标签（exit:1:{reason} 或 exit:all:{reason}）
            elif DynamicGridTagUtils.is_exit_tag(tag):
                # 适配器已处理杠杆转换，直接使用返回的金额
                self.logger.info(
                    f"DGL-STRATEGY|适配器返回平仓|交易对={pair}|交易ID={trade.id}|"
                    f"金额={amount}|标签={tag}|层级ID={level_id}|原因={extra}"
                )
            # 使用中国时间显示
            time_str = format_china_time(current_time)
            
            # 详细的减仓/平仓日志
            if isinstance(adj, tuple) and (DynamicGridTagUtils.is_reduce_tag(adj[1]) or DynamicGridTagUtils.is_exit_tag(adj[1])):
                reduce_amount = adj[0]  # 减仓金额（负数）
                reduce_stake_abs = abs(reduce_amount)
                trade_amount = float(trade.amount or 0.0)
                trade_stake = float(trade.stake_amount or 0.0)
                current_rate_val = float(current_rate or 0.0)
                
                # 获取 stake_currency（从交易对解析 quote 货币）
                stake_currency = pair.split("/")[1].split(":")[0] if "/" in pair else "USDT"

                # Freqtrade 会使用比例计算：amount = abs(reduce_amount * trade.amount / trade.stake_amount)
                calculated_amount = abs(reduce_amount * trade_amount / trade_stake) if trade_stake > 0 else 0
                # 理论上应该减仓的数量（基于当前价格）
                expected_amount_by_price = reduce_stake_abs / current_rate_val if current_rate_val > 0 else 0
                
                # 检查是否会导致全平
                fill_ratio = calculated_amount / trade_amount if trade_amount > 0 else 0
                is_full_close = fill_ratio >= 0.95
                
                # 数据一致性检查：验证 trade.stake_amount 是否合理
                # 如果 trade.stake_amount 约等于 trade.amount * current_rate，说明数据一致
                expected_stake = trade_amount * current_rate_val if current_rate_val > 0 else 0
                stake_ratio = trade_stake / expected_stake if expected_stake > 0 else 0
                is_data_inconsistent = abs(stake_ratio - 1.0) > 0.1  # 10% 误差阈值

                self.logger.debug("=" * 80)
                self.logger.info(
                    f"DGL-STRATEGY|减仓计算详情|交易对={pair}|交易ID={trade.id}|"
                    f"减仓金额({stake_currency})={reduce_stake_abs:.6f}|"
                    f"当前持仓数量(基础币)={trade_amount:.6f}|"
                    f"当前持仓金额({stake_currency})={trade_stake:.6f}|"
                    f"当前价格={current_rate_val:.6f}|"
                    f"Freqtrade将计算数量={calculated_amount:.6f}|"
                    f"理论上应减数量(基于价格)={expected_amount_by_price:.6f}|"
                    f"计算比例={fill_ratio:.4%}|是否会全平={is_full_close}|"
                    f"标签={tag}|K线时间={time_str}"
                )
                if is_data_inconsistent:
                    self.logger.error(
                        f"DGL-STRATEGY|⚠️数据不一致警告|交易对={pair}|交易ID={trade.id}|"
                        f"trade.stake_amount={trade_stake:.6f}|预期金额={expected_stake:.6f}|"
                        f"比例={stake_ratio:.4%}|这可能是因为使用了旧的数据库！"
                    )
                if is_full_close:
                    self.logger.warning(
                        f"DGL-STRATEGY|⚠️警告|减仓将导致全平|"
                        f"计算数量={calculated_amount}|交易总数量={trade_amount}|"
                        f"减仓金额={reduce_stake_abs}|交易总金额={trade_stake}|"
                        f"比例={fill_ratio:.4%}"
                    )
                self.logger.debug("=" * 80)
            
            self.logger.info(
                f"DGL-STRATEGY|准备下单调整|交易对={pair}|方向={'做空' if is_short else '做多'}|金额={amount}|标签={tag}|K线时间={time_str}"
            )
            # time.sleep(3)
        else:
            # 使用中国时间显示
            time_str = format_china_time(current_time)
            self.logger.info(
                f"DGL-STRATEGY|准备下单调整|交易对={pair}|方向={'做空' if is_short else '做多'}|金额={adj}|K线时间={time_str}"
            )
        return adj

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | bool | None:
        """全局平仓三模式：金额止盈、金额止损、价格比例止盈（直接基于 Trade 计算）。

        返回 True 或 自定义字符串 以触发出场；并通过 trade.custom_data 写入 reason，
        由适配层在 order_filled 中回灌 GlobalExitSuccess 到网格管理器。
        """
        manager = self.adapter.grid_managers.get(pair)
        if not manager:
            return None

        cfg = manager.config
        if not cfg:
            return None

        # 基于 Trade 计算：
        # - 均价：trade.open_rate（freqtrade 按已成交订单加权，含费校正）
        # - 总净盈亏：trade.calculate_profit(current_rate).total_profit（已实现 + 当前未实现，含费/杠杆）
        profit_struct = trade.calculate_profit(current_rate)
        total_profit = float(profit_struct.total_profit)  # 总净盈亏（可能为正数或负数）
        avg_price = float(trade.open_rate or 0.0)

        # 价格比例止盈：基于 Trade 均价与当前价
        # 注意：只有在实际盈亏为正时才触发价格比例止盈，避免亏损时误触发
        if cfg.enable_global_tp_price_ratio_check and (cfg.global_tp_price_ratio is not None):
            if avg_price > 0 and trade.amount > 0:
                if not trade.is_short:
                    price_ratio = (float(current_rate) - avg_price) / avg_price
                else:
                    price_ratio = (avg_price - float(current_rate)) / avg_price
                # 修复：只有在实际盈亏为正时才触发价格比例止盈
                if price_ratio >= float(cfg.global_tp_price_ratio) and total_profit > 0:
                    trade.set_custom_data("reason", "global_take_profit")
                    return "global_take_profit"

        # 金额止盈：基于 Trade 总净盈亏
        if cfg.enable_global_tp_amount_check and (cfg.global_tp_amount_target is not None):
            if total_profit >= abs(float(cfg.global_tp_amount_target)):
                trade.set_custom_data("reason", "global_take_profit")
                return "global_take_profit"

        # 金额止损：基于 Trade 总净盈亏
        if cfg.enable_global_sl_amount_check and (cfg.global_sl_amount_target is not None):
            if total_profit <= -abs(float(cfg.global_sl_amount_target)):
                trade.set_custom_data("reason", "global_stop_loss")
                return "global_stop_loss"

        return None

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        """确认入场交易：参数按 interface.py 对齐；首层仍由订单成交回调驱动"""
        self.logger.info(
            f"确认入场交易: pair={pair}, amount={amount}, rate={rate}, tif={time_in_force}, time={current_time}, entry_tag={entry_tag}, side={side}"
        )
        return True

    def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time: datetime,
        **kwargs,
    ) -> bool:
        """确认出场交易：参数按 interface.py 对齐；由 Freqtrade 执行订单，网格侧仅接收成交回调"""
        self.logger.info(
            f"确认出场交易: pair={pair}, amount={amount}, rate={rate}, tif={time_in_force}, time={current_time}, exit_reason={exit_reason}"
        )
        return True

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        """
        自定义每个新交易的杠杆。仅在期货模式下调用。
        
        :param pair: 当前分析的交易对
        :param current_time: 包含当前时间的 datetime 对象
        :param current_rate: 根据 exit_pricing 中的定价设置计算的费率
        :param proposed_leverage: 机器人建议的杠杆
        :param max_leverage: 该交易对允许的最大杠杆
        :param entry_tag: 如果提供买入信号，可选的 entry_tag (buy_tag)
        :param side: 'long' 或 'short' - 表示提议交易的方向
        :return: 杠杆值，应在 1.0 和 max_leverage 之间
        """
        # 从配置中获取杠杆设置
        leverage_value = self.config.get("leverage", None)
        
        if leverage_value is not None:
            # 确保杠杆值在有效范围内
            leverage_value = float(leverage_value)
            leverage_value = max(1.0, min(leverage_value, max_leverage))
            self.logger.info(
                f"DGL-STRATEGY|使用配置杠杆|交易对={pair}|杠杆={leverage_value}|最大杠杆={max_leverage}|方向={side}"
            )
            return leverage_value
        
        # 如果没有配置，使用提议的杠杆（默认 1.0）
        self.logger.info(
            f"DGL-STRATEGY|使用默认杠杆|交易对={pair}|提议杠杆={proposed_leverage}|最大杠杆={max_leverage}|方向={side}"
        )
        return proposed_leverage
