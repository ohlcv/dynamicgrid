from pathlib import Path
import logging
import math
from typing import Any, Dict, List
import csv

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from datetime import datetime

# 导入核心模块（包内相对导入）
from ...core.level import DynamicGridLevelState
from ...core.event import (
    DynamicGridDataUpdateEvent,
    DynamicGridAddSuccessEvent,
    DynamicGridReduceSuccessEvent,
    DynamicGridGlobalExitSuccessEvent,
    DynamicGridEntryTriggerEvent,
    DynamicGridExitTriggerEvent,
    DynamicGridAddTriggerEvent,
    DynamicGridReduceTriggerEvent,
    DynamicGridEvent,
    format_china_time,
)
from ...core.config import DynamicGridConfig
from ...core.manager import DynamicGridManager
from ...utils.tag_utils import DynamicGridTagUtils

logger = logging.getLogger("dynamicgrid.adapter")


class DynamicGridFreqtradeAdapter:
    def __init__(self, strategy: IStrategy):
        self.strategy = strategy
        self.grid_managers: Dict[str, DynamicGridManager] = {}
        self.signals: Dict[str, Dict[str, Any]] = {}
        self.is_base_mode: bool = False  # 是否为币本位模式
        self.detailed_signal_summary: bool = False  # 是否使用详细信号摘要
        self.logger = strategy.logger
        self.logger.debug("DGL-ADAPTER|初始化")
    
        # 统一日志级别
        self._harmonize_logger_levels()

        # 调试导出配置
        logs_dir = Path("logs")  # 相对于当前工作目录
        # logs_dir.mkdir(parents=True, exist_ok=True)
        self._debug_export_path = logs_dir / "dynamicgrid_debug.csv"
        self._debug_export_initialized = False
        self._debug_export_enabled = False

    def get_signal_configs(self, indicator_key: str) -> List[Dict[str, Any]]:
        """获取指标配置列表
        
        Args:
            indicator_key: 指标键名（如 "rsi", "cci"）
        
        Returns:
            配置列表
        """
        signal_data = self.signals.get(indicator_key)
        if signal_data is None:
            return []
        
        # 直接返回列表（新格式）
        if isinstance(signal_data, list):
            return signal_data
        
        # 如果不是列表，返回空列表
        return []

    def _harmonize_logger_levels(self) -> None:
        """统一 dynamicgrid 各组件 logger 的 level 与策略 logger 对齐。"""
        level = self.logger.level
        # 父级统一控制
        logging.getLogger("dynamicgrid").setLevel(level)
        # 子模块
        logging.getLogger("dynamicgrid.event").setLevel(level)
        logging.getLogger("dynamicgrid.adapter").setLevel(level)
        logging.getLogger("dynamicgrid.config").setLevel(level)
        logging.getLogger("dynamicgrid.manager").setLevel(level)
        logging.getLogger("dynamicgrid.level").setLevel(level)

    # ===== 配置管理 =====
    def _normalize_pair_for_freqtrade(self, pair: str, trading_mode: str) -> str:
        """根据交易模式规范化 pair 格式
        
        Args:
            pair: 原始 pair 字符串
            trading_mode: 交易模式 ("futures" 或 "spot")
        
        Returns:
            规范化后的 pair 字符串
        """
        if trading_mode == "futures":
            # 合约模式：确保格式为 "BTC/USDT:USDT"
            if ":" not in pair:
                # 如果是 "BTC/USDT"，添加 ":USDT"
                if "/" in pair:
                    base, quote = pair.split("/", 1)
                    return f"{base}/{quote}:{quote}"
                else:
                    # 如果是 "BTCUSDT"，转换为 "BTC/USDT:USDT"
                    # 简单处理：假设最后4个字符是USDT
                    if pair.endswith("USDT"):
                        base = pair[:-4]
                        return f"{base}/USDT:USDT"
                    return pair
            # 如果已经有 ":"，直接返回
            return pair
        elif trading_mode == "spot":
            # 现货模式：确保格式为 "BTC/USDT"（去掉 ":USDT" 后缀）
            if ":" in pair:
                # 去掉 ":USDT" 后缀
                return pair.split(":")[0]
            # 如果没有 ":"，确保有 "/"
            if "/" not in pair:
                # 如果是 "BTCUSDT"，转换为 "BTC/USDT"
                if pair.endswith("USDT"):
                    base = pair[:-4]
                    return f"{base}/USDT"
            return pair
        else:
            # 未知模式，保持原样
            self.logger.warning(f"DGL-ADAPTER|未知交易模式|trading_mode={trading_mode}|保持原pair={pair}")
            return pair

    def initialize_from_config(self, dynamicgrid_config: dict, trading_mode: str = "spot") -> None:
        """从freqtrade配置中初始化网格系统"""
        self._debug_export_enabled = bool(dynamicgrid_config.get("debug_export", False))

        # 从配置中获取网格配置和信号
        pairs_config = dynamicgrid_config.get("pairs", [])
        signals = dynamicgrid_config.get("signals", {})
        is_base_mode = dynamicgrid_config.get("is_base_mode", False)
        detailed_signal_summary = dynamicgrid_config.get("detailed_signal_summary", True)

        self.signals = signals
        self.is_base_mode = is_base_mode
        self.detailed_signal_summary = detailed_signal_summary

        # 创建网格管理器
        for pair_config in pairs_config:
            # 将全局 is_base_mode 合并到每个 pair_config 中（如果 pair_config 没有指定）
            if "is_base_mode" not in pair_config:
                pair_config["is_base_mode"] = is_base_mode
            grid_config = DynamicGridConfig(**pair_config)
            original_pair = grid_config.pair
            
            # 根据 trading_mode 规范化 pair 格式
            normalized_pair = self._normalize_pair_for_freqtrade(original_pair, trading_mode)
            
            # 如果 pair 被规范化了，更新 grid_config
            if normalized_pair != original_pair:
                grid_config.pair = normalized_pair
                self.logger.debug(
                    f"DGL-ADAPTER|规范化pair|原始={original_pair}|规范化={normalized_pair}|模式={trading_mode}"
                )
            
            manager = DynamicGridManager(grid_config)
            self.grid_managers[normalized_pair] = manager
            self.logger.debug(
                f"DGL-ADAPTER|初始化网格|pair={normalized_pair}|{'空头' if grid_config.is_short else '多头'}|币本位={grid_config.is_base_mode}"
            )

        self._harmonize_logger_levels()
        self.logger.info(
            f"DGL-ADAPTER|配置加载完成|交易对数={len(self.grid_managers)}|币本位={is_base_mode}"
        )

    def restore_all_grid_states(self) -> None:
        """恢复所有网格管理器的状态（在数据库会话可用后调用）"""
        self.logger.debug("DGL-ADAPTER|开始从数据库恢复网格状态")
        for pair, manager in self.grid_managers.items():
            self._restore_grid_state_from_database(pair, manager)
        self.logger.debug("DGL-ADAPTER|网格状态恢复完成")

    def _restore_grid_state_from_database(
        self, pair: str, manager: DynamicGridManager
    ) -> None:
        """从数据库恢复网格状态
        
        恢复逻辑（完全基于订单的 side，不使用标签）：
        1. 查询开放交易：获取该交易对的所有开放交易
        2. 收集订单并排序：收集所有已成交订单，按 order_filled_date 排序
        3. 栈配对（LIFO）：使用栈配对逻辑，buy/sell 抵消
           - 加仓订单（buy/sell 根据网格方向判断）：入栈
           - 减仓订单：从栈顶弹出配对（LIFO）
           - 原理：不可能平掉已经平了的仓位，所以栈配对逻辑是正确的
        4. 按时间顺序分配层级：未配对的加仓订单按时间顺序分配给层级
        
        优点：
        - 不依赖标签，即使标签乱了或修改了也能正确恢复
        - 使用时间顺序和栈配对，逻辑简单可靠
        - 兼容新旧标签格式
        """
        # ===== 步骤 1：查询该交易对的所有开放交易 =====
        open_trades = Trade.get_open_trades()
        pair_trades = [
            trade
            for trade in open_trades
            if trade.pair == pair and trade.is_short == manager.is_short
        ]

        if not pair_trades:
            self.logger.info(
                f"DGL-ADAPTER|状态恢复|交易对={pair}|方向={'空头' if manager.is_short else '多头'}|无开放交易"
            )
            return

        self.logger.info(
            f"DGL-ADAPTER|状态恢复|交易对={pair}|方向={'空头' if manager.is_short else '多头'}|开放交易数={len(pair_trades)}"
        )

        # ===== 步骤 2：收集所有订单并按成交时间排序 =====
        all_orders = []
        for trade in pair_trades:
            for order in trade.orders:
                # 只处理已成交的订单
                if (
                    order.status
                    and order.status.lower() in ("closed", "filled")
                    and order.order_filled_date
                ):
                    all_orders.append(order)

        if not all_orders:
            self.logger.info(f"DGL-ADAPTER|状态恢复|交易对={pair}|无已成交订单")
            return

        # 按成交时间排序（时间顺序很重要，用于栈配对）
        all_orders.sort(key=lambda o: o.order_filled_date or o.order_date)

        # ===== 步骤 3：栈配对（LIFO）- 完全基于 ft_order_side，不使用标签 =====
        # 定义加仓和减仓的 side（根据网格方向）
        # 对于多头网格：buy 是加仓，sell 是减仓
        # 对于空头网格：sell 是加仓，buy 是减仓
        is_short = manager.is_short
        if is_short:
            add_side = "sell"  # 空头网格：sell 是加仓
            reduce_side = "buy"  # 空头网格：buy 是减仓
        else:
            add_side = "buy"  # 多头网格：buy 是加仓
            reduce_side = "sell"  # 多头网格：sell 是减仓

        self.logger.debug(
            f"DGL-ADAPTER|状态恢复|交易对={pair}|"
            f"总订单数={len(all_orders)}|加仓side={add_side}|减仓side={reduce_side}"
        )

        # 使用栈配对（LIFO）：加仓订单入栈，减仓订单出栈配对
        # 原理：不可能平掉已经平了的仓位，所以栈配对逻辑是正确的
        unmatched_add_orders: list = []  # 未配对的加仓订单栈

        for order in all_orders:
            # 获取订单 side（优先使用 ft_order_side，回退到 side）
            order_side = (
                order.ft_order_side.lower()
                if order.ft_order_side
                else (order.side.lower() if order.side else None)
            )

            # 判断订单类型：只使用 side，不使用标签
            if not order_side:
                self.logger.warning(
                    f"DGL-ADAPTER|订单无side信息|订单ID={order.order_id}|"
                    f"标签={order.ft_order_tag or '无标签'}"
                )
                continue

            if order_side == add_side:
                # 加仓订单：入栈
                unmatched_add_orders.append(order)
                self.logger.debug(
                    f"DGL-ADAPTER|加仓订单入栈|订单ID={order.order_id}|"
                    f"side={order_side}|价格={order.safe_price}|数量={order.safe_filled}|"
                    f"时间={order.order_filled_date}|标签={order.ft_order_tag or '无标签'}"
                )
            elif order_side == reduce_side:
                # 减仓订单：从栈顶弹出配对（LIFO）
                if unmatched_add_orders:
                    matched_add = unmatched_add_orders.pop()
                    self.logger.info(
                        f"DGL-ADAPTER|订单配对成功(栈配对)|加仓订单={matched_add.order_id}|"
                        f"减仓订单={order.order_id}|加仓价格={matched_add.safe_price}|"
                        f"减仓价格={order.safe_price}|加仓数量={matched_add.safe_filled}|"
                        f"减仓数量={order.safe_filled}|加仓时间={matched_add.order_filled_date}|"
                        f"减仓时间={order.order_filled_date}"
                    )
                else:
                    # 没有可配对的加仓订单（可能是首次开仓前的减仓，或数据异常）
                    self.logger.warning(
                        f"DGL-ADAPTER|减仓订单无配对|订单ID={order.order_id}|"
                        f"side={order_side}|价格={order.safe_price}|数量={order.safe_filled}|"
                        f"时间={order.order_filled_date}|标签={order.ft_order_tag or '无标签'}"
                    )

        # ===== 步骤 4：按时间顺序分配层级 =====
        # 未配对的加仓订单就是当前还开着的仓位
        # 按时间顺序分配给层级（从低到高）
        self.logger.info(
            f"DGL-ADAPTER|状态恢复|交易对={pair}|总未配对加仓订单数={len(unmatched_add_orders)}"
        )

        if not unmatched_add_orders:
            self.logger.info(
                f"DGL-ADAPTER|状态恢复|交易对={pair}|无未配对订单，所有层级已平仓"
            )
            manager._update_level_relations()
            return

        # 按成交时间排序所有未配对订单（时间顺序重建层级）
        # 注意：栈中的订单已经是按时间顺序的（因为 all_orders 已排序），但为了确保，再次排序
        unmatched_add_orders.sort(key=lambda o: o.order_filled_date or o.order_date)

        self.logger.info(
            f"DGL-ADAPTER|状态恢复|交易对={pair}|未配对加仓订单数={len(unmatched_add_orders)}|"
            f"将按时间顺序分配层级（idx=0 → level_id=1, idx=1 → level_id=2, ...）"
        )

        # 按时间顺序将订单分配给层级（从低到高）
        # 注意：level_id从1开始，manager.levels[0]对应level_id=1
        for idx, add_order in enumerate(unmatched_add_orders):
            if idx >= len(manager.levels):
                self.logger.warning(
                    f"DGL-ADAPTER|状态恢复|层级超出范围|索引={idx}|总层级数={len(manager.levels)}|"
                    f"订单ID={add_order.order_id}|成交时间={add_order.order_filled_date}|"
                    f"标签={add_order.ft_order_tag or '无标签'}"
                )
                break

            level = manager.levels[idx]  # idx=0对应level_id=1, idx=1对应level_id=2, ...

            # 恢复层级状态
            level._op_price = (
                float(add_order.safe_price) if add_order.safe_price else None
            )
            level._base_amount = (
                float(add_order.safe_filled) if add_order.safe_filled else None
            )
            level._quote_amount = (
                float(add_order.safe_cost) if add_order.safe_cost else None
            )

            if level._op_price and level._base_amount:
                # 设置层级状态为等待止盈激活（已开仓，等待止盈条件）
                level.current_state = DynamicGridLevelState.WAITING_TP_ACTIVE
                self.logger.info(
                    f"DGL-ADAPTER|状态恢复(按时间顺序)|层级={level.level_id}|价格={level._op_price}|"
                    f"基础数量={level.base_amount}|计价数量={level.quote_amount}|"
                    f"订单ID={add_order.order_id}|成交时间={add_order.order_filled_date}|"
                    f"标签={add_order.ft_order_tag or '无标签'}|时间顺序索引={idx}"
                )
            else:
                self.logger.warning(
                    f"DGL-ADAPTER|状态恢复(按时间顺序)|层级={level.level_id}|订单数据不完整|"
                    f"价格={level._op_price}|数量={level._base_amount}|订单ID={add_order.order_id}"
                )

        # ===== 步骤 5：更新层级关系 =====
        # 更新层级之间的前后关系（prev_level, next_level）
        manager._update_level_relations()
        self.logger.info(
            f"DGL-ADAPTER|状态恢复完成|交易对={pair}|方向={'空头' if manager.is_short else '多头'}|"
            f"已恢复层级数={len([level for level in manager.levels if level.is_open])}"
        )

    # ===== 调试导出 =====
    def _ensure_export_header(self, fieldnames: List[str]) -> None:
        if not self._debug_export_enabled:
            return
        if not self._debug_export_initialized:
            if not self._debug_export_path.exists():
                with self._debug_export_path.open(
                    "w", newline="", encoding="utf-8"
                ) as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
            self._debug_export_initialized = True

    def _export_debug_rows(
        self,
        pair: str,
        current_time,
        current_rate: float,
        events: List[DynamicGridEvent] | None,
        selected_type: str | None,
        selected_tag: str | None,
        selected_amount: float | None,
    ) -> None:
        if not self._debug_export_enabled:
            return
        manager = self.grid_managers.get(pair)
        if manager is None:
            return

        # 定义字段
        fields = [
            "ts",
            "dt",
            "pair",
            "cur_price",
            "level_id",
            "state",
            "op_price",
            "base_amount",
            "quote_amount",
            "min_price",
            "max_price",
            "op_active_price",
            "op_rebound_price",
            "tp_active_price",
            "tp_rebound_price",
            "pending_action",
            "prev_opened",
            "next_opened",
            "out_events",
            "selected_type",
            "selected_tag",
            "selected_amount",
        ]
        self._ensure_export_header(fields)

        out_types = [e.event_type for e in events] if events else []
        ts_timestamp = float(current_time.timestamp() if current_time else 0.0)

        rows: List[Dict[str, Any]] = []
        for level in manager.levels:
            prev_opened = bool(level.prev.is_open) if level.prev else False
            next_opened = bool(level.next.is_open) if level.next else False
            pair_with_side = f"{pair}:{'short' if manager.config.is_short else 'long'}"

            row = {
                "ts": ts_timestamp,
                "dt": str(ts_timestamp),
                "pair": pair_with_side,
                "cur_price": float(current_rate),
                "level_id": int(level.level_id),
                "state": level.current_state.value,
                "op_price": float(level.op_price) if level.op_price else None,
                "base_amount": float(level.base_amount) if level.base_amount else None,
                "quote_amount": float(level.quote_amount)
                if level.quote_amount
                else None,
                "min_price": float(
                    level.max_price if level.is_short else level.min_price
                )
                if (level.max_price or level.min_price)
                else None,
                "max_price": float(
                    level.min_price if level.is_short else level.max_price
                )
                if (level.max_price or level.min_price)
                else None,
                "op_active_price": float(level.op_active_price)
                if level.op_active_price
                else None,
                "op_rebound_price": float(level.op_rebound_price)
                if level.op_rebound_price
                else None,
                "tp_active_price": float(level.tp_active_price)
                if level.tp_active_price
                else None,
                "tp_rebound_price": float(level.tp_rebound_price)
                if level.tp_rebound_price
                else None,
                "pending_action": level._pending_action,
                "prev_opened": prev_opened,
                "next_opened": next_opened,
                "out_events": ",".join(out_types),
                "selected_type": selected_type or "",
                "selected_tag": selected_tag or "",
                "selected_amount": selected_amount
                if selected_amount is not None
                else "",
            }
            rows.append(row)

        with self._debug_export_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writerows(rows)

    # ===== 核心业务逻辑 =====
    def handle_data_update(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        last_candle: dict,
        trade: Trade | None = None,
    ) -> float | None | tuple[float, str]:
        """处理数据更新，触发网格计算，并将 OUTBOUND 事件映射为调整金额直接返回。

        返回：
          - 正数/负数：增减仓 stake 金额（计价币）
          - (amount, order_tag)：同时返回下单标签（原因）
          - None：不调整
        """
        # 使用中国时间显示
        time_str = format_china_time(current_time)
        self.logger.debug(
            f"DGL-ADAPTER|处理数据更新|交易对={pair}|价格={current_rate}|盈亏={current_profit:.4%}|K线时间={time_str}"
        )

        # 获取管理器并检查启用状态
        manager = self.grid_managers.get(pair)
        if not manager:
            self.logger.debug(f"DGL-ADAPTER|网格管理器不存在|交易对={pair}")
            return None
        if not manager.enabled:
            self.logger.debug(f"DGL-ADAPTER|网格管理器未启用|交易对={pair}")
            return None

        # 验证K线数据完整性
        required_keys = ["open", "high", "low", "close", "volume"]
        missing_keys = [k for k in required_keys if k not in last_candle]
        if missing_keys:
            raise KeyError(f"last_candle missing keys: {missing_keys}")

        # 数据验证：检查价格和数量是否为 NaN/Inf
        open_price = float(last_candle["open"])
        high_price = float(last_candle["high"])
        low_price = float(last_candle["low"])
        close_price = float(current_rate)
        volume = float(last_candle["volume"])
        
        # 检查价格有效性
        if (math.isnan(open_price) or math.isinf(open_price) or open_price <= 0 or
            math.isnan(high_price) or math.isinf(high_price) or high_price <= 0 or
            math.isnan(low_price) or math.isinf(low_price) or low_price <= 0 or
            math.isnan(close_price) or math.isinf(close_price) or close_price <= 0):
            self.logger.warning(
                f"DGL-ADAPTER|K线价格数据无效|交易对={pair}|"
                f"open={open_price}|high={high_price}|low={low_price}|close={close_price}|跳过"
            )
            return None
        
        # 检查成交量有效性
        if math.isnan(volume) or math.isinf(volume) or volume < 0:
            self.logger.warning(
                f"DGL-ADAPTER|K线成交量数据无效|交易对={pair}|volume={volume}|跳过"
            )
            return None

        # 创建数据更新事件
        event = DynamicGridDataUpdateEvent.create(
            pair=pair,
            open_price=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            current_time=current_time,
        )

        # 处理事件并获取输出事件
        events = manager.process_event(event)

        types = [e.event_type for e in events]
        self.logger.debug(
            f"DGL-ADAPTER|输出事件|交易对={pair}|数量={len(events)}|类型={types}"
        )

        # 映射事件为调仓指令
        selected_type = None
        selected_tag = None
        selected_amount = None
        res = self._map_events_to_adjustment(pair=pair, events=events, trade=trade, current_rate=current_rate)

        self.logger.debug(f"DGL-ADAPTER|映射结果|交易对={pair}|结果={res}")

        if isinstance(res, tuple):
            selected_amount, selected_tag = res
            # 使用工具类判断标签类型
            if DynamicGridTagUtils.is_exit_tag(selected_tag) or DynamicGridTagUtils.is_reduce_tag(selected_tag):
                selected_type = "reduce"  # exit 和 reduce 都视为减仓类型
            elif DynamicGridTagUtils.is_entry_tag(selected_tag) or DynamicGridTagUtils.is_add_tag(selected_tag):
                selected_type = "add"  # entry 和 add 都视为加仓类型
            else:
                selected_type = None

        # 导出调试数据
        if self._debug_export_enabled:
            self._export_debug_rows(
                pair=pair,
                current_time=current_time,
                current_rate=current_rate,
                events=events,
                selected_type=selected_type,
                selected_tag=selected_tag,
                selected_amount=selected_amount,
            )

        return res

    def _convert_nominal_to_actual_stake(
        self, nominal_amount: float, trade: Trade | None, current_rate: float | None
    ) -> float:
        """将名义金额转换为实际投入金额（考虑杠杆）
        
        Args:
            nominal_amount: 名义金额（网格框架计算的金额）
            trade: Freqtrade 交易对象
            current_rate: 当前价格
            
        Returns:
            转换后的实际投入金额
        """
        if trade is None or current_rate is None or current_rate <= 0:
            return nominal_amount
        
        trade_amount = float(trade.amount or 0.0)
        trade_stake = float(trade.stake_amount or 0.0)
        
        if trade_amount <= 0 or trade_stake <= 0:
            return nominal_amount
        
        # 计算预期金额（名义金额）
        expected_stake = trade_amount * current_rate
        
        if expected_stake <= 0:
            return nominal_amount
        
        # 检查是否使用了杠杆（stake_ratio 应该接近 1.0，如果差异 > 10% 说明使用了杠杆）
        stake_ratio = trade_stake / expected_stake
        is_data_inconsistent = abs(stake_ratio - 1.0) > 0.1  # 10% 误差阈值
        
        if not is_data_inconsistent:
            # 数据一致，未使用杠杆，直接返回名义金额
            return nominal_amount
        
        # 使用杠杆，按比例转换
        actual_stake = abs(nominal_amount) * (trade_stake / expected_stake)
        
        self.logger.info(
            f"DGL-ADAPTER|杠杆转换|交易对={trade.pair}|交易ID={trade.id}|"
            f"名义金额={abs(nominal_amount):.6f}|预期金额={expected_stake:.6f}|"
            f"实际投入金额={trade_stake:.6f}|转换后金额={actual_stake:.6f}|比例={stake_ratio:.4%}"
        )
        
        # 保持原始符号
        return actual_stake if nominal_amount >= 0 else -actual_stake

    def _map_events_to_adjustment(
        self, pair: str, events: List[DynamicGridEvent], trade: Trade | None = None, current_rate: float | None = None
    ) -> float | None | tuple[float, str]:
        """按优先级将 OUTBOUND 事件映射为 Freqtrade 所需的调整金额：
        ReduceTrigger > AddTrigger
        如果有多个减仓事件，合并它们的金额
        
        注意：如果检测到杠杆交易，会将名义金额转换为实际投入金额
        """
        self.logger.debug(f"DGL-ADAPTER|映射开始|交易对={pair}|事件数={len(events)}")

        # 收集第一层平仓事件
        exit_events = [
            e for e in events if isinstance(e, DynamicGridExitTriggerEvent)
        ]
        
        # 收集后续层减仓事件
        reduce_events = [
            e for e in events if isinstance(e, DynamicGridReduceTriggerEvent)
        ]
        
        # 收集第一层开仓事件
        entry_events = [
            e for e in events if isinstance(e, DynamicGridEntryTriggerEvent)
        ]
        
        # 收集后续层加仓事件
        add_events = [
            e for e in events if isinstance(e, DynamicGridAddTriggerEvent)
        ]

        # 优先处理第一层平仓事件
        if exit_events:
            selected = exit_events[0]
            data = selected.data or {}
            reason = data.get("reason", "exit")
            
            # 优先使用事件中的 quote_amount（已根据当前价格计算）
            quote_amount = float(data.get("quote_amount", 0) or 0)
            base_amount = float(data.get("base_amount", 0) or 0)
            target_price = float(data.get("target_price", 0) or 0)
            
            # 数据验证：检查价格和数量是否为 NaN/Inf
            if (math.isnan(target_price) or math.isinf(target_price) or target_price <= 0):
                self.logger.warning(
                    f"DGL-ADAPTER|第一层平仓target_price无效|"
                    f"target_price={target_price}|跳过订单"
                )
                return None
            
            if (math.isnan(base_amount) or math.isinf(base_amount) or base_amount < 0):
                self.logger.warning(
                    f"DGL-ADAPTER|第一层平仓base_amount无效|"
                    f"base_amount={base_amount}|跳过订单"
                )
                return None
            
            # 如果 quote_amount 为 0 但 base_amount 和 target_price 有效，则计算
            if quote_amount == 0 and base_amount > 0 and target_price > 0:
                quote_amount = base_amount * target_price
                # 验证计算结果
                if math.isnan(quote_amount) or math.isinf(quote_amount):
                    self.logger.warning(
                        f"DGL-ADAPTER|第一层平仓quote_amount计算无效|"
                        f"base_amount={base_amount}|target_price={target_price}|计算结果={quote_amount}|跳过订单"
                    )
                    return None
            
            amount = -quote_amount
            # 转换名义金额为实际投入金额（考虑杠杆）
            amount = self._convert_nominal_to_actual_stake(amount, trade, current_rate)
            
            if trade is not None:
                trade.set_custom_data("level_id", 1)
                trade.set_custom_data("reason", reason)
            
            # 第一层平仓标签
            signal_name = data.get("signal_name", "")
            order_tag = DynamicGridTagUtils.build_exit_tag(1, reason if not signal_name else f"{reason}:{signal_name}")
            
            self.logger.debug("-" * 60)
            self.logger.debug(
                f"DGL-ADAPTER|映射第一层平仓|原因={reason}|金额={amount}|标签={order_tag}"
            )
            return amount, order_tag
        
        # 优先处理后续层减仓事件（如果有多个，合并金额）
        if reduce_events:
            if len(reduce_events) > 1:
                # 多个减仓事件：合并金额
                total_quote_amount = 0.0
                level_ids = []
                reasons = []
                
                for e in reduce_events:
                    data = e.data or {}
                    # 优先使用事件中的 quote_amount（已根据当前价格计算）
                    # 如果没有，则使用 base_amount * target_price 计算
                    quote_amount = float(data.get("quote_amount", 0) or 0)
                    base_amount = float(data.get("base_amount", 0) or 0)
                    target_price = float(data.get("target_price", 0) or 0)
                    
                    # 数据验证：检查价格和数量是否为 NaN/Inf
                    if (math.isnan(target_price) or math.isinf(target_price) or target_price <= 0):
                        self.logger.warning(
                            f"DGL-ADAPTER|减仓target_price无效|层级={data.get('level_id', 1)}|"
                            f"target_price={target_price}|跳过"
                        )
                        continue
                    
                    if (math.isnan(base_amount) or math.isinf(base_amount) or base_amount < 0):
                        self.logger.warning(
                            f"DGL-ADAPTER|减仓base_amount无效|层级={data.get('level_id', 1)}|"
                            f"base_amount={base_amount}|跳过"
                        )
                        continue
                    
                    # 如果 quote_amount 为 0 但 base_amount 和 target_price 有效，则计算
                    if quote_amount == 0 and base_amount > 0 and target_price > 0:
                        quote_amount = base_amount * target_price
                        # 验证计算结果
                        if math.isnan(quote_amount) or math.isinf(quote_amount):
                            self.logger.warning(
                                f"DGL-ADAPTER|减仓quote_amount计算无效|层级={data.get('level_id', 1)}|"
                                f"base_amount={base_amount}|target_price={target_price}|计算结果={quote_amount}|跳过"
                            )
                            continue
                        self.logger.debug(
                            f"DGL-ADAPTER|减仓金额计算|层级={data.get('level_id', 1)}|"
                            f"基础数量={base_amount}|目标价格={target_price}|计算金额={quote_amount}"
                        )
                    
                    level_id = data.get("level_id", 1)
                    reason = data.get("reason", "reduce")
                    total_quote_amount += quote_amount
                    level_ids.append(level_id)
                    reasons.append(reason)
                
                amount = -total_quote_amount
                # 转换名义金额为实际投入金额（考虑杠杆）
                amount = self._convert_nominal_to_actual_stake(amount, trade, current_rate)
                
                # 使用第一个层级的ID作为标签（主要用于日志）
                # 注意：减仓标签只用于 level_id >= 2，如果 level_id == 1 应该使用 exit:1
                first_level_id = level_ids[0] if level_ids else 2
                if first_level_id < 2:
                    # 如果第一层需要减仓，应该使用 exit:1 标签
                    first_reason = reasons[0] if reasons else "exit"
                    order_tag = DynamicGridTagUtils.build_exit_tag(1, first_reason)
                else:
                    first_reason = reasons[0] if reasons else "reduce"
                    order_tag = DynamicGridTagUtils.build_reduce_tag(first_level_id, first_reason)
                
                if trade is not None:
                    # 存储所有层级ID（用逗号分隔）
                    trade.set_custom_data("level_id", ",".join(map(str, level_ids)))
                    # 如果有多个原因，使用第一个
                    trade.set_custom_data("reason", reasons[0] if reasons else "reduce")
                
                self.logger.debug("-" * 60)
                self.logger.debug(
                    f"DGL-ADAPTER|映射减仓(合并)|层级={level_ids}|原因={reasons}|"
                    f"合并金额={amount}|标签={order_tag}|事件数={len(reduce_events)}"
                )
                return amount, order_tag
            else:
                # 单个减仓事件
                selected = reduce_events[0]
                data = selected.data or {}
                level_id = data.get("level_id", 1)
                reason = data.get("reason", "reduce")
                
                # 优先使用事件中的 quote_amount（已根据当前价格计算）
                # 如果没有，则使用 base_amount * target_price 计算
                quote_amount = float(data.get("quote_amount", 0) or 0)
                base_amount = float(data.get("base_amount", 0) or 0)
                target_price = float(data.get("target_price", 0) or 0)
                
                # 数据验证：检查价格和数量是否为 NaN/Inf
                if (math.isnan(target_price) or math.isinf(target_price) or target_price <= 0):
                    self.logger.warning(
                        f"DGL-ADAPTER|减仓target_price无效|层级={level_id}|"
                        f"target_price={target_price}|跳过订单"
                    )
                    return None
                
                if (math.isnan(base_amount) or math.isinf(base_amount) or base_amount < 0):
                    self.logger.warning(
                        f"DGL-ADAPTER|减仓base_amount无效|层级={level_id}|"
                        f"base_amount={base_amount}|跳过订单"
                    )
                    return None
                
                # 如果 quote_amount 为 0 但 base_amount 和 target_price 有效，则计算
                if quote_amount == 0 and base_amount > 0 and target_price > 0:
                    quote_amount = base_amount * target_price
                    # 验证计算结果
                    if math.isnan(quote_amount) or math.isinf(quote_amount):
                        self.logger.warning(
                            f"DGL-ADAPTER|减仓quote_amount计算无效|层级={level_id}|"
                            f"base_amount={base_amount}|target_price={target_price}|计算结果={quote_amount}|跳过订单"
                        )
                        return None
                    self.logger.debug(
                        f"DGL-ADAPTER|减仓金额计算|层级={level_id}|"
                        f"基础数量={base_amount}|目标价格={target_price}|计算金额={quote_amount}"
                    )
                
                amount = -quote_amount
                # 转换名义金额为实际投入金额（考虑杠杆）
                amount = self._convert_nominal_to_actual_stake(amount, trade, current_rate)
                
                if trade is not None:
                    trade.set_custom_data("level_id", int(level_id))
                    trade.set_custom_data("reason", reason)
                # 根据层级ID选择标签：level_id == 1 使用 exit:1，level_id >= 2 使用 reduce:{level_id}
                if level_id == 1:
                    order_tag = DynamicGridTagUtils.build_exit_tag(1, reason)
                else:
                    order_tag = DynamicGridTagUtils.build_reduce_tag(level_id, reason)
                self.logger.debug("-" * 60)
                self.logger.debug(
                    f"DGL-ADAPTER|映射减仓|层级={level_id}|原因={reason}|金额={amount}|标签={order_tag}"
                )
                return amount, order_tag

        # 其次处理第一层开仓事件
        if entry_events:
            selected = entry_events[0]
            data = selected.data or {}
            quote_amount = float(data.get("quote_amount", 0) or 0)
            
            # 数据验证：检查金额是否为 NaN/Inf
            if math.isnan(quote_amount) or math.isinf(quote_amount) or quote_amount <= 0:
                self.logger.warning(
                    f"DGL-ADAPTER|第一层开仓quote_amount无效|"
                    f"quote_amount={quote_amount}|跳过订单"
                )
                return None
            
            amount = quote_amount
            if trade is not None:
                trade.set_custom_data("level_id", 1)
            
            # 第一层开仓标签
            signal_name = data.get("signal_name", "signal")
            order_tag = DynamicGridTagUtils.build_entry_tag(signal_name)
            
            self.logger.debug("-" * 60)
            self.logger.debug(
                f"DGL-ADAPTER|映射第一层开仓|金额={amount}|标签={order_tag}|信号={signal_name}"
            )
            return amount, order_tag
        
        # 最后处理后续层加仓事件（只选择第一个）
        if add_events:
            selected = add_events[0]
            data = selected.data or {}
            level_id = data.get("level_id", 2)
            quote_amount = float(data.get("quote_amount", 0) or 0)
            
            # 数据验证：检查金额是否为 NaN/Inf
            if math.isnan(quote_amount) or math.isinf(quote_amount) or quote_amount <= 0:
                self.logger.warning(
                    f"DGL-ADAPTER|加仓quote_amount无效|层级={level_id}|"
                    f"quote_amount={quote_amount}|跳过订单"
                )
                return None
            
            amount = quote_amount
            if trade is not None:
                trade.set_custom_data("level_id", int(level_id))
            
            # 构建订单标签：add:{level_id}:{reason}（level_id >= 2）
            # 注意：加仓订单的reason可以是触发条件（可选），信号只在首次开仓时有效
            if level_id < 2:
                self.logger.error(f"DGL-ADAPTER|加仓层级ID必须 >= 2|层级ID={level_id}")
                return None, None
            order_tag = DynamicGridTagUtils.build_add_tag(level_id)  # reason可选，可以为None
            
            self.logger.debug("-" * 60)
            self.logger.debug(
                f"DGL-ADAPTER|映射加仓|层级={level_id}|金额={amount}|标签={order_tag}"
            )
            return amount, order_tag

        self.logger.debug("DGL-ADAPTER|无输出事件")
        return None

    # ===== order_filled 细分：可测试的小函数 =====
    def _get_order_filled_cursor_key(self, order, current_time: datetime | None) -> str:
        """为部分成交幂等处理生成游标 key。"""
        _oid = getattr(order, "order_id", None) or "unknown"
        if _oid == "unknown" and current_time is not None:
            ts = int(current_time.timestamp() if current_time else 0)
            return f"of:unknown:{ts}"
        return f"of:{_oid}"

    def _compute_filled_delta(self, trade: Trade, order, cursor_key: str) -> tuple[float, float]:
        """计算本次增量成交（base），返回 (delta_base, filled_now)。"""
        last_filled = float(trade.get_custom_data(cursor_key, 0.0))
        filled_now = float(getattr(order, "safe_filled", None) or 0.0)
        delta_base = max(0.0, filled_now - last_filled)
        return delta_base, filled_now

    def _compute_fee_quote(self, order, trade: Trade, executed_price: float) -> float | None:
        """尽力估算 quote 侧手续费（用于统计与回灌）。"""
        fee_base_val = getattr(order, "ft_fee_base", None)
        if fee_base_val is not None and executed_price:
            return float(fee_base_val) * float(executed_price)

        # 回退：用费率 * safe_cost 估算
        is_entry_side = getattr(order, "ft_order_side", None) == getattr(trade, "entry_side", None)
        fee_rate = trade.fee_open if is_entry_side else trade.fee_close
        safe_cost = getattr(order, "safe_cost", None)
        if fee_rate is not None and safe_cost is not None:
            return float(safe_cost) * float(fee_rate)

        return None

    def _is_entry_order(self, order, trade: Trade, tag: str) -> bool:
        """判断当前订单是否属于开仓侧（更优先使用 side 判断，标签兜底）。"""
        order_side = getattr(order, "ft_order_side", None)
        trade_entry_side = getattr(trade, "entry_side", None)
        if order_side is not None and trade_entry_side is not None:
            return order_side == trade_entry_side
        # 无 side 信息时回退到标签判断（entry/add 都视为开仓）
        return DynamicGridTagUtils.is_entry_tag(tag) or DynamicGridTagUtils.is_add_tag(tag)

    def _is_global_exit(
        self,
        trade: Trade,
        order,
        tag: str,
        reason: str,
        filled_now: float,
        trade_amount_before: float,
    ) -> bool:
        """判断是否为全局退出（全平）。

        规则顺序与原实现保持一致，便于回归：
        1) 开仓订单：永远不是全局退出
        2) reason=global_take_profit/global_stop_loss
        3) 标签为全局退出标签（exit:all / roi / stop_loss 等）
        4) Freqtrade 系统标签：partial_exit 需看成交比例，其余默认视为全局退出
        5) 成交数量接近 trade.amount（>=95%）视为全平
        6) trade.is_open=False 视为全平
        """
        if self._is_entry_order(order, trade, tag):
            return False

        if reason in ("global_take_profit", "global_stop_loss"):
            return True

        if tag and DynamicGridTagUtils.is_global_exit_tag(tag):
            return True

        if tag and DynamicGridTagUtils.is_freqtrade_system_tag(tag):
            tag_lower = tag.lower()
            if tag_lower == "partial_exit":
                if trade_amount_before > 0:
                    fill_ratio = (
                        filled_now / trade_amount_before
                        if filled_now > 0 and trade_amount_before > 0
                        else 0.0
                    )
                    return fill_ratio >= 0.95
                return False
            return True

        if trade_amount_before > 0:
            fill_ratio = (
                filled_now / trade_amount_before
                if filled_now > 0 and trade_amount_before > 0
                else 0.0
            )
            if fill_ratio >= 0.95:
                return True

        trade_is_open = getattr(trade, "is_open", True)
        if not trade_is_open:
            return True

        return False

    def on_order_filled(
        self, pair: str, trade: Trade, order, current_time: datetime | None = None
    ) -> None:
        """订单成交回调：根据真实成交生成 SuccessEvent 并回馈 Manager（支持部分成交幂等）"""
        # 优先从订单标签解析 level_id/reason
        tag = order.ft_order_tag or ""
        level_id = trade.get_custom_data("level_id", 1)
        reason = trade.get_custom_data("reason", "")

        # 部分成交增量处理：按 order_id 记录已处理 filled 游标
        cursor_key = self._get_order_filled_cursor_key(order, current_time)
        delta_base, filled_now = self._compute_filled_delta(trade, order, cursor_key)

        if delta_base == 0.0:
            return

        self.logger.info("=" * 80)
        self.logger.info(
            f"DGL-ADAPTER|订单成交|交易对={pair}|交易ID={trade.id}|订单ID={order.order_id}|"
            f"标签={tag}|层级={level_id}|原因={reason}"
        )
        # 详细检查订单标签
        order_tag_raw = getattr(order, 'ft_order_tag', None)
        # 注意：Freqtrade RPC消息中的 buy_tag/enter_tag 使用的是 trade.enter_tag（初始标签）
        # 但 order.ft_order_tag 是正确的当前订单标签
        self.logger.info(
            f"DGL-ADAPTER|订单标签详情|order.ft_order_tag={order_tag_raw}|"
            f"trade.enter_tag={trade.enter_tag}|tag变量={tag}|"
            f"注意：RPC消息中的buy_tag/enter_tag使用trade.enter_tag（初始标签），"
            f"但实际处理使用order.ft_order_tag（当前订单标签）"
        )

        # 用本次增量生成事件
        executed_price = float(order.safe_price or 0.0)
        
        # 数据验证：检查价格和数量是否为 NaN/Inf
        if math.isnan(executed_price) or math.isinf(executed_price) or executed_price <= 0:
            self.logger.warning(
                f"DGL-ADAPTER|订单成交价格无效|交易对={pair}|订单ID={order.order_id}|"
                f"executed_price={executed_price}|跳过"
            )
            return
        
        if math.isnan(delta_base) or math.isinf(delta_base) or delta_base <= 0:
            self.logger.warning(
                f"DGL-ADAPTER|订单成交数量无效|交易对={pair}|订单ID={order.order_id}|"
                f"delta_base={delta_base}|跳过"
            )
            return
        
        delta_quote = delta_base * executed_price
        
        # 验证计算结果
        if math.isnan(delta_quote) or math.isinf(delta_quote):
            self.logger.warning(
                f"DGL-ADAPTER|订单成交金额计算无效|交易对={pair}|订单ID={order.order_id}|"
                f"delta_base={delta_base}|executed_price={executed_price}|计算结果={delta_quote}|跳过"
            )
            return

        # 计算 quote 侧手续费
        fee_quote_val = self._compute_fee_quote(order, trade, executed_price)

        # 判断是否为全局退出（全平）
        # 1. 首先判断是否是开仓订单（如果是开仓订单，直接跳过全局退出判断）
        # 2. 检查 trade.custom_data 中的 reason
        # 3. 检查订单标签是否为全局退出类型（roi, trailing_stop_loss, stop_loss 等）
        # 4. 检查订单成交数量是否接近交易当前数量（全平）
        # 5. 检查交易是否已经关闭
        trade_amount_before = float(trade.amount or 0.0)
        is_entry_order = self._is_entry_order(order, trade, tag)
        
        order_side = getattr(order, 'ft_order_side', None) or getattr(order, 'side', None)
        trade_entry_side = getattr(trade, 'entry_side', None)
        
        # 调试日志：记录判断前的状态
        tag_lower = tag.lower() if tag else ""
        is_freqtrade_tag = DynamicGridTagUtils.is_freqtrade_system_tag(tag)
        self.logger.info(
            f"DGL-ADAPTER|全局退出判断开始|标签={tag}|"
            f"原因={reason}|交易数量={trade_amount_before}|"
            f"订单方向={order_side}|交易开仓方向={trade_entry_side}|是否开仓订单={is_entry_order}|"
            f"是否Freqtrade系统标签={is_freqtrade_tag}"
        )
        
        if is_entry_order:
            self.logger.info(
                f"DGL-ADAPTER|全局退出判断|跳过开仓订单|标签={tag}|"
                f"订单方向={order_side}|交易开仓方向={trade_entry_side}|"
                f"订单成交数量={filled_now}|交易当前数量={trade_amount_before}"
            )

        is_global_exit = self._is_global_exit(
            trade=trade,
            order=order,
            tag=tag,
            reason=reason,
            filled_now=filled_now,
            trade_amount_before=trade_amount_before,
        )

        # 最终判断结果日志
        if not is_global_exit:
            self.logger.warning(
                f"DGL-ADAPTER|全局退出判断|未匹配任何条件|"
                f"标签={tag}|原因={reason}|交易数量={trade_amount_before}|"
                f"交易是否开放={getattr(trade, 'is_open', 'unknown')}|"
                f"将使用回退逻辑"
            )
        else:
            tag_reason = DynamicGridTagUtils.get_reason_from_tag(tag)
            log_reason = tag_reason or reason or tag or "unknown"
            self.logger.info(
                f"DGL-ADAPTER|全局退出判断|最终结果=全局退出|"
                f"标签={tag}|原因={log_reason}"
            )

        # 统一收敛到一个待处理事件，末尾集中 dispatch 给 manager
        event_to_process: DynamicGridEvent | None = None

        # 根据标签创建对应的成功事件
        if is_global_exit:
            # 直接使用原始reason，不需要标准化（因为全局退出事件的reason只用于日志，没有实际逻辑作用）
            # 优先使用标签中的reason，其次使用trade.custom_data中的reason，最后使用标签本身
            tag_reason = DynamicGridTagUtils.get_reason_from_tag(tag)
            final_reason = tag_reason or reason or tag or "unknown"

            event = DynamicGridGlobalExitSuccessEvent.create(
                is_short=trade.is_short,
                total_base_amount=float(delta_base),
                total_quote_amount=float(delta_quote),
                executed_price=float(executed_price),
                reason=final_reason,  # 直接使用原始reason
                pair=pair,
                current_time=current_time,
            )
            # 将原始标签和原因存储在 custom_data 中（用于调试和后续分析）
            event.data["custom_data"] = {
                "original_tag": tag if tag else "unknown",
                "original_reason": reason if reason else "unknown",
                "exit_tag": tag if tag else "unknown",  # 兼容性：保留 exit_tag
                "exit_reason": reason if reason else "unknown",  # 兼容性：保留 exit_reason
            }
            self.logger.info(
                f"DGL-ADAPTER|全局退出成功|基础数量={delta_base}|价格={executed_price}|"
                f"原因={final_reason}|标签={tag}"
            )
            event_to_process = event
        elif DynamicGridTagUtils.is_entry_tag(tag):
            # entry:1:{reason} - 开仓（第一层），reason可以是信号摘要
            tag_type, level_id, reason = DynamicGridTagUtils.parse_tag(tag)
            if level_id != 1:
                self.logger.error(f"DGL-ADAPTER|开仓标签层级ID必须为1|标签={tag}|层级ID={level_id}")
                return
            # 存储reason到 custom_data（reason可以是信号摘要）
            if trade is not None:
                trade.set_custom_data("level_id", 1)
                if reason:
                    trade.set_custom_data("reason", reason)  # 统一使用reason字段
            # 开仓成功事件等同于第一层加仓成功
            event = DynamicGridAddSuccessEvent.create(
                level_id=1,
                is_short=trade.is_short,
                base_amount=float(delta_base),
                quote_amount=float(delta_quote),
                executed_price=float(executed_price),
                pair=pair,
                current_time=current_time,
            )
            event.data.update(
                {
                    "order_id": order.order_id,
                    "fee_base": order.ft_fee_base,
                    "fee_quote": fee_quote_val,
                    "reason": reason,  # 统一使用reason字段
                }
            )
            self.logger.info(
                f"DGL-ADAPTER|开仓成功|层级=1|基础数量={delta_base}|价格={executed_price}|原因={reason}"
            )
            event_to_process = event
        elif DynamicGridTagUtils.is_add_tag(tag):
            # add:l{level} - 加仓（level_id >= 2）
            # 注意：当前代码只处理单个加仓事件
            # 如果将来需要支持多个加仓事件合并，应按升序处理（前层→后层）
            # 原因：前层必须先开仓，后层才能从 WAITING_PREV_OP 激活到 WAITING_OP_ACTIVE
            tag_type, level_id, _ = DynamicGridTagUtils.parse_tag(tag)
            if level_id is None or level_id < 2:
                self.logger.error(f"DGL-ADAPTER|加仓标签层级ID必须 >= 2|标签={tag}|层级ID={level_id}")
                return
            if trade is not None:
                trade.set_custom_data("level_id", level_id)
            event = DynamicGridAddSuccessEvent.create(
                level_id=level_id,
                is_short=trade.is_short,
                base_amount=float(delta_base),
                quote_amount=float(delta_quote),
                executed_price=float(executed_price),
                pair=pair,
                current_time=current_time,
            )
            # 添加额外的订单信息到事件数据中
            event.data.update(
                {
                    "order_id": order.order_id,
                    "fee_base": order.ft_fee_base,
                    "fee_quote": fee_quote_val,
                }
            )
            self.logger.info(
                f"DGL-ADAPTER|加仓成功|层级={level_id}|基础数量={delta_base}|价格={executed_price}"
            )
            event_to_process = event
        elif DynamicGridTagUtils.is_exit_tag(tag):
            # exit:1:{reason} 或 exit:all:{reason} 或 Freqtrade系统标签（如"roi", "stop_loss"等）
            tag_type, level_id, exit_reason = DynamicGridTagUtils.parse_tag(tag)
            
            # 如果是Freqtrade系统标签，通常表示全局退出
            if DynamicGridTagUtils.is_freqtrade_system_tag(tag):
                # Freqtrade系统标签直接就是reason，level_id为None表示全局退出
                exit_reason = tag.lower()  # 使用标签本身作为reason
                level_id = None  # 全局退出
                
            if level_id is None:
                # exit:all:{reason} 或 Freqtrade系统标签 - 全局平仓
                if trade is not None:
                    trade.set_custom_data("is_global_exit", True)
                    trade.set_custom_data("reason", exit_reason or "global_exit")
                # 为所有层级创建减仓成功事件
                manager = self.grid_managers.get(pair)
                if manager:
                    # 获取所有活跃层级
                    active_levels = [l for l in manager.levels.values() if l.state != "CLOSED"]
                    if active_levels:
                        # 按比例分配减仓数量
                        quote_amount_per_level = abs(float(delta_quote)) / len(active_levels) if delta_quote != 0 else 0
                        base_amount_per_level = abs(float(delta_base)) / len(active_levels) if delta_base != 0 else 0
                        fee_quote_per_level = (fee_quote_val or 0.0) / len(active_levels)
                        
                        events_to_process = []
                        for level in active_levels:
                            event = DynamicGridReduceSuccessEvent.create(
                                level_id=level.level_id,
                                is_short=trade.is_short,
                                base_amount=float(base_amount_per_level),
                                quote_amount=float(quote_amount_per_level),
                                executed_price=float(executed_price),
                                reason=exit_reason or "global_exit",
                                pair=pair,
                                current_time=current_time,
                            )
                            event.data.update({
                                "order_id": order.order_id,
                                "fee_base": (order.ft_fee_base or 0.0) / len(active_levels),
                                "fee_quote": fee_quote_per_level,
                            })
                            events_to_process.append((level.level_id, event))
                        
                        # 处理所有层级的事件
                        for _, event in events_to_process:
                            manager.process_event(event)
                        
                        self.logger.info(
                            f"DGL-ADAPTER|全局平仓成功|层级数={len(active_levels)}|总基础数量={delta_base}|"
                            f"总计价数量={delta_quote}|价格={executed_price}|原因={exit_reason}"
                        )
                return
            elif level_id == 1:
                # exit:1:{reason} - 平第一层
                if trade is not None:
                    trade.set_custom_data("level_id", 1)
                    trade.set_custom_data("reason", exit_reason or "exit")
                event = DynamicGridReduceSuccessEvent.create(
                    level_id=1,
                    is_short=trade.is_short,
                    base_amount=float(delta_base),
                    quote_amount=float(delta_quote),
                    executed_price=float(executed_price),
                    reason=exit_reason or "exit",
                    pair=pair,
                    current_time=current_time,
                )
                event.data.update({
                    "order_id": order.order_id,
                    "fee_base": order.ft_fee_base,
                    "fee_quote": fee_quote_val,
                })
                self.logger.info(
                    f"DGL-ADAPTER|平第一层成功|基础数量={delta_base}|价格={executed_price}|原因={exit_reason}"
                )
                event_to_process = event
            else:
                self.logger.error(f"DGL-ADAPTER|平仓标签层级ID无效|标签={tag}|层级ID={level_id}")
                return
        elif DynamicGridTagUtils.is_reduce_tag(tag):
            # reduce:l{level} - 减仓（level_id >= 2）
            tag_type, first_level_id, _ = DynamicGridTagUtils.parse_tag(tag)
            if first_level_id is None or first_level_id < 2:
                self.logger.error(f"DGL-ADAPTER|减仓标签层级ID必须 >= 2|标签={tag}|层级ID={first_level_id}")
                return
            reason = reason or "reduce"
            
            # 检查是否有多个层级合并减仓（从 custom_data 中获取）
            level_ids_str = trade.get_custom_data("level_id", "")
            if level_ids_str and "," in str(level_ids_str):
                # 多个层级合并减仓：为每个层级发送成功事件
                level_ids = [int(x.strip()) for x in str(level_ids_str).split(",") if x.strip().isdigit()]
                if level_ids:
                    # 减仓顺序：先处理后层（高层级ID），再处理前层（低层级ID）
                    # 原因：后层必须先关闭，前层才能从 WAITING_NEXT_CLOSE 恢复到 WAITING_TP_ACTIVE
                    level_ids.sort(reverse=True)  # 降序：20, 19, 18, 17
                    
                    # 按比例分配减仓数量（基于 quote_amount）
                    # 假设每个层级的减仓金额相等（实际应该从事件中获取，但这里简化处理）
                    quote_amount_per_level = abs(float(delta_quote)) / len(level_ids) if delta_quote != 0 else 0
                    base_amount_per_level = abs(float(delta_base)) / len(level_ids) if delta_base != 0 else 0
                    fee_quote_per_level = (fee_quote_val or 0.0) / len(level_ids)
                    
                    self.logger.info(
                        f"DGL-ADAPTER|合并减仓成功|层级={level_ids}|总基础数量={delta_base}|总计价数量={delta_quote}|"
                        f"每个层级基础数量={base_amount_per_level}|每个层级计价数量={quote_amount_per_level}|价格={executed_price}|"
                        f"处理顺序=降序(后层→前层)"
                    )
                    
                    # 为每个层级创建事件（先创建，不立即处理）
                    manager = self.grid_managers.get(pair)
                    if manager:
                        events_to_process = []
                        for level_id in level_ids:
                            event = DynamicGridReduceSuccessEvent.create(
                                level_id=level_id,
                                is_short=trade.is_short,
                                base_amount=float(base_amount_per_level),
                                quote_amount=float(quote_amount_per_level),
                                executed_price=float(executed_price),
                                reason=reason or "reduce",
                                pair=pair,
                                current_time=current_time,
                            )
                            event.data.update(
                                {
                                    "order_id": order.order_id,
                                    "fee_base": order.ft_fee_base / len(level_ids) if order.ft_fee_base else None,
                                    "fee_quote": fee_quote_per_level,
                                }
                            )
                            events_to_process.append((level_id, event))
                            self.logger.info(
                                f"DGL-ADAPTER|减仓成功(合并)|层级={level_id}|基础数量={base_amount_per_level}|价格={executed_price}|原因={reason or '减仓'}"
                            )
                        
                        # 先处理所有层级的事件（但不触发全局状态检查）
                        # 注意：这里每个层级处理都会调用 _comprehensive_state_check()
                        # 但这是必要的，因为状态检查是基于当前状态的，多次检查会收敛到正确状态
                        for _, event in events_to_process:
                            manager.process_event(event)
                        
                        # 最后统一进行一次全局状态检查，确保所有层级关系正确
                        # 注意：由于每个层级处理时已经调用了 _comprehensive_state_check()，
                        # 这里再次调用是为了确保在多个层级同时更新后的最终一致性
                        final_events = manager._comprehensive_state_check()
                        if final_events:
                            self.logger.debug(
                                f"DGL-ADAPTER|合并减仓后全局状态检查|生成事件数={len(final_events)}"
                            )
                    # 清空合并的层级ID标记
                    trade.set_custom_data("level_id", first_level_id)
                    return
            else:
                # 单个层级减仓：正常处理
                event = DynamicGridReduceSuccessEvent.create(
                    level_id=first_level_id,
                    is_short=trade.is_short,
                    base_amount=float(delta_base),
                    quote_amount=float(delta_quote),
                    executed_price=float(executed_price),
                    reason=reason or "reduce",
                    pair=pair,
                    current_time=current_time,
                )
                # 添加额外的订单信息到事件数据中
                event.data.update(
                    {
                        "order_id": order.order_id,
                        "fee_base": order.ft_fee_base,
                        "fee_quote": fee_quote_val,
                    }
                )
                self.logger.info(
                    f"DGL-ADAPTER|减仓成功|层级={first_level_id}|基础数量={delta_base}|价格={executed_price}|原因={reason or '减仓'}"
                )
                event_to_process = event
        else:
            # 回退：根据订单方向和位置变化做最小假设
            if order.ft_order_side == trade.entry_side:
                event = DynamicGridAddSuccessEvent.create(
                    level_id=level_id,
                    is_short=trade.is_short,
                    base_amount=float(delta_base),
                    quote_amount=float(delta_quote),
                    executed_price=float(executed_price),
                    pair=pair,
                    current_time=current_time,
                )
                self.logger.info(
                    f"DGL-ADAPTER|回退加仓成功|层级={level_id}|基础数量={delta_base}|价格={executed_price}"
                )
            else:
                event = DynamicGridReduceSuccessEvent.create(
                    level_id=level_id,
                    is_short=trade.is_short,
                    base_amount=float(delta_base),
                    quote_amount=float(delta_quote),
                    executed_price=float(executed_price),
                    reason=reason or "unknown",
                    pair=pair,
                    current_time=current_time,
                )
                self.logger.info(
                    f"DGL-ADAPTER|回退减仓成功|层级={level_id}|基础数量={delta_base}|价格={executed_price}|原因={reason or '未知'}"
                )
            event_to_process = event

        # 将事件发送给对应的管理器（统一出口）
        if event_to_process is not None:
            manager = self.grid_managers.get(pair)
            if manager:
                manager.process_event(event_to_process)
        
        # 更新游标，防止重复累计
        trade.set_custom_data(cursor_key, filled_now)
