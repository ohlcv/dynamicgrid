import logging
import math
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .config import DynamicGridConfig, DynamicGridLevelConfig
from .event import (
    DynamicGridAddSuccessEvent,
    DynamicGridAddTriggerEvent,
    DynamicGridEntryTriggerEvent,
    DynamicGridExitTriggerEvent,
    DynamicGridExitSignalEvent,
    DynamicGridDataUpdateEvent,
    DynamicGridEvent,
    DynamicGridGlobalExitSuccessEvent,
    DynamicGridEntrySignalEvent,
    DynamicGridReduceSuccessEvent,
    DynamicGridReduceTriggerEvent,
    DynamicGridStateChangeEvent,
)


logger = logging.getLogger("dynamicgrid.level")


class DynamicGridLevelState(Enum):
    """网格级别状态"""

    WAITING_PREV_OP = "WAITING_PREV_OP"
    WAITING_OP_ACTIVE = "WAITING_OP_ACTIVE"
    WAITING_OP_REBOUND = "WAITING_OP_REBOUND"
    WAITING_TP_ACTIVE = "WAITING_TP_ACTIVE"
    WAITING_TP_REBOUND = "WAITING_TP_REBOUND"
    WAITING_NEXT_CLOSE = "WAITING_NEXT_CLOSE"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


VALID_TRANSITIONS = {
    DynamicGridLevelState.WAITING_PREV_OP: [
        DynamicGridLevelState.WAITING_OP_ACTIVE,
        DynamicGridLevelState.WAITING_TP_ACTIVE,
        DynamicGridLevelState.ERROR,
    ],
    DynamicGridLevelState.WAITING_OP_ACTIVE: [
        DynamicGridLevelState.WAITING_OP_REBOUND,
        DynamicGridLevelState.WAITING_PREV_OP,  # 前一层平仓后回退
        DynamicGridLevelState.WAITING_TP_ACTIVE,
        DynamicGridLevelState.ERROR,
    ],
    DynamicGridLevelState.WAITING_OP_REBOUND: [
        DynamicGridLevelState.WAITING_TP_ACTIVE,  # 成功事件确认后
        DynamicGridLevelState.WAITING_PREV_OP,
        DynamicGridLevelState.ERROR,  # 订单失败
    ],
    DynamicGridLevelState.WAITING_TP_ACTIVE: [
        DynamicGridLevelState.WAITING_TP_REBOUND,
        DynamicGridLevelState.WAITING_NEXT_CLOSE,
        DynamicGridLevelState.WAITING_PREV_OP,
        DynamicGridLevelState.ERROR,
    ],
    DynamicGridLevelState.WAITING_TP_REBOUND: [
        DynamicGridLevelState.COMPLETED,  # 成功事件确认后
        DynamicGridLevelState.WAITING_PREV_OP,
        DynamicGridLevelState.ERROR,  # 订单失败
    ],
    DynamicGridLevelState.WAITING_NEXT_CLOSE: [
        DynamicGridLevelState.WAITING_TP_ACTIVE,
        DynamicGridLevelState.WAITING_PREV_OP,
        DynamicGridLevelState.ERROR,
    ],
    DynamicGridLevelState.COMPLETED: [
        DynamicGridLevelState.WAITING_PREV_OP,  # 后续层重置
        DynamicGridLevelState.WAITING_OP_ACTIVE,  # 第一层收到新信号时重置
        DynamicGridLevelState.ERROR,
    ],
    DynamicGridLevelState.ERROR: [DynamicGridLevelState.WAITING_PREV_OP],
}


class DynamicGridLevel:
    """单个网格级别的状态机"""

    def __init__(
        self,
        config: DynamicGridLevelConfig,
        parent_config: "DynamicGridConfig" = None,
    ):
        self.config = config
        self.parent_config = parent_config
        self.pair = config.pair
        self.is_short = config.is_short
        self.level_id = config.level_id
        # 第一层初始状态是 WAITING_OP_ACTIVE，后续层初始状态是 WAITING_PREV_OP
        if self.is_first_level():
            self.current_state = DynamicGridLevelState.WAITING_OP_ACTIVE
        else:
            self.current_state = DynamicGridLevelState.WAITING_PREV_OP
        self._op_price: Optional[float] = None
        self._last_price: Optional[float] = None
        self._max_price: Optional[float] = None
        self._min_price: Optional[float] = None
        self._base_amount: Optional[float] = None
        self._quote_amount: Optional[float] = None
        self._realized_profit: float = 0.0
        self._realized_loss: float = 0.0
        self.win_count: int = 0
        self.loss_count: int = 0
        self.last_event_time: str | None = None
        self.prev: Optional["DynamicGridLevel"] = None
        self.next: Optional["DynamicGridLevel"] = None
        # NOTE: 是否跳过第一层（level_id==1）的反弹等待：
        # - True: 收到 entry 信号后直接开仓，状态变为 WAITING_TP_ACTIVE（更"立即"）
        # - False: 收到 entry 信号后状态变为 WAITING_OP_REBOUND，等待价格反弹后再开仓（更"保守"）
        self.skip_first_level_rebound = (
            parent_config.skip_first_level_rebound if parent_config and self.is_first_level() else False
        )
        self.skip_levels_exit_number = (
            int(parent_config.skip_levels_exit_number) if parent_config else 0
        )
        # 是否启用性能指标计算
        self.enable_performance_metrics = (
            parent_config.enable_performance_metrics if parent_config else True
        )
        # 待成交标记，防止重复触发（同一时刻只会有一种动作）
        self._pending_action: str | None = None  # 'add' | 'reduce' | None
        self._pending_since: float | None = None
        # 累计统计与订单记录（仅在启用性能指标时维护）
        self._cum_fee_quote: float = 0.0
        self._cum_executed_quote: float = 0.0
        self._executed_orders: list[dict] = []
        self.logger = logging.getLogger(
            f"dynamicgrid.level.{self.pair}.{'short' if self.is_short else 'long'}.{self.level_id}"
        )
        self.logger.debug(
            f"DGL-LEVEL|初始化|层级={self.level_id}|交易对={self.pair}|方向={'做空' if self.is_short else '做多'}"
        )

    def is_first_level(self) -> bool:
        """判断是否是第一层（level_id == 1）"""
        return self.level_id == 1

    @property
    def op_price(self) -> Optional[float]:
        return self._op_price if self.is_open else None

    @property
    def base_amount(self) -> Optional[float]:
        return self._base_amount if self.is_open else None

    @property
    def quote_amount(self) -> Optional[float]:
        return self._quote_amount if self.is_open else None

    @property
    def unrealized_pnl(self) -> float:
        """计算未实现损益，确保不会产生 NaN 或 Inf 值"""
        if not self.is_open:
            return 0.0
        if not self._last_price or not self._op_price or not self._base_amount:
            return 0.0
        
        try:
            last_price = float(self._last_price)
            op_price = float(self._op_price)
            base_amount = float(self._base_amount)
            
            # 检查是否有 NaN 或 Inf
            if math.isnan(last_price) or math.isinf(last_price) or \
               math.isnan(op_price) or math.isinf(op_price) or \
               math.isnan(base_amount) or math.isinf(base_amount):
                self.logger.warning(
                    f"DGL-LEVEL|未实现损益计算：数据包含NaN/Inf|层级={self.level_id}|"
                    f"last_price={last_price}|op_price={op_price}|base_amount={base_amount}"
                )
                return 0.0
            
            # 计算未实现损益
            if not self.is_short:
                pnl = (last_price - op_price) * base_amount
            else:
                pnl = (op_price - last_price) * base_amount
            
            # 检查计算结果
            if math.isnan(pnl) or math.isinf(pnl):
                self.logger.warning(
                    f"DGL-LEVEL|未实现损益计算结果异常|层级={self.level_id}|"
                    f"pnl={pnl}|last_price={last_price}|op_price={op_price}|base_amount={base_amount}"
                )
                return 0.0
            
            return pnl
        except (ValueError, TypeError) as e:
            self.logger.warning(
                f"DGL-LEVEL|未实现损益计算失败|层级={self.level_id}|错误={e}|"
                f"last_price={self._last_price}|op_price={self._op_price}|base_amount={self._base_amount}"
            )
            return 0.0

    @property
    def realized_profit(self) -> float:
        return self._realized_profit

    @property
    def realized_loss(self) -> float:
        return self._realized_loss

    @property
    def total_fee_quote(self) -> float:
        """累计的 quote 侧手续费（订单成功时累加）"""
        return self._cum_fee_quote

    @property
    def total_executed_quote(self) -> float:
        """累计成交的 quote 金额（订单成功时累加）"""
        return self._cum_executed_quote

    @property
    def executed_orders(self) -> list[dict]:
        """已记录的订单字典列表（来源于 Success 事件解析）"""
        return self._executed_orders

    @property
    def max_price(self) -> Optional[float]:
        return (
            self._max_price
            if self.current_state
            in [
                DynamicGridLevelState.WAITING_OP_REBOUND,
                DynamicGridLevelState.WAITING_TP_REBOUND,
            ]
            else None
        )

    @property
    def min_price(self) -> Optional[float]:
        return (
            self._min_price
            if self.current_state
            in [
                DynamicGridLevelState.WAITING_OP_REBOUND,
                DynamicGridLevelState.WAITING_TP_REBOUND,
            ]
            else None
        )

    @property
    def op_active_price(self) -> Optional[float]:
        """计算激活开仓价格"""
        if self.current_state == DynamicGridLevelState.WAITING_OP_ACTIVE:
            op_ratio = abs(self.config.op_ratio)
            
            # 第一层：基于当前价格或最后价格计算（如果没有 prev）
            if self.is_first_level():
                reference_price = self._last_price
                if reference_price is None:
                    return None
                
                if not self.is_short:
                    # 多头：价格下跌 op_ratio 后激活
                    return reference_price * (1 - op_ratio)
                else:
                    # 空头：价格上涨 op_ratio 后激活
                    return reference_price * (1 + op_ratio)
            
            # 后续层：基于前一层开仓价计算
            if self.prev and self.prev.op_price:
                if not self.is_short:
                    return self.prev.op_price * (1 - op_ratio)
                else:
                    return self.prev.op_price * (1 + op_ratio)
        
        return None

    @property
    def op_rebound_price(self) -> Optional[float]:
        """计算反弹开仓价格 - 基于价格极值动态调整"""
        if (
            self.current_state == DynamicGridLevelState.WAITING_OP_REBOUND
            and self._max_price is not None
            and self._min_price is not None
        ):
            op_rebound_ratio = abs(self.config.op_rebound_ratio)
            if not self.is_short:
                # 做多：基于最低价向上反弹
                rebound_price = self._min_price * (1 + op_rebound_ratio)
                self.logger.debug(
                    f"DGL-LEVEL|开仓反弹计算|层级={self.level_id}|方向=做多|最低={self._min_price}|比例={op_rebound_ratio}|价格={rebound_price}"
                )
                return rebound_price
            else:
                # 做空：基于最高价向下反弹
                rebound_price = self._max_price * (1 - op_rebound_ratio)
                self.logger.debug(
                    f"DGL-LEVEL|开仓反弹计算|层级={self.level_id}|方向=做空|最高={self._max_price}|比例={op_rebound_ratio}|价格={rebound_price}"
                )
                return rebound_price
        # else:
        # self.logger.debug(
        #     f"DGL-LEVEL|开仓反弹不可用|层级={self.level_id}|状态={self.current_state}|最高={self._max_price}|最低={self._min_price}"
        # )
        return None

    @property
    def tp_active_price(self) -> Optional[float]:
        """计算激活止盈价格"""
        if self.current_state == DynamicGridLevelState.WAITING_TP_ACTIVE and self.op_price:
            tp_ratio = abs(self.config.tp_ratio)
            if not self.is_short:
                return self.op_price * (1 + tp_ratio)
            else:
                return self.op_price * (1 - tp_ratio)
        return None

    @property
    def tp_rebound_price(self) -> Optional[float]:
        """计算反弹止盈价格"""
        if (
            self.current_state == DynamicGridLevelState.WAITING_TP_REBOUND
            and self._max_price
            and self._min_price
        ):
            tp_rebound_ratio = abs(self.config.tp_rebound_ratio)
            if not self.is_short:
                rebound_price = self._max_price * (1 - tp_rebound_ratio)
                self.logger.debug(
                    f"DGL-LEVEL|止盈反弹计算|层级={self.level_id}|方向=做多|最高={self._max_price}|比例={tp_rebound_ratio}|价格={rebound_price}"
                )
                return rebound_price
            else:
                rebound_price = self._min_price * (1 + tp_rebound_ratio)
                self.logger.debug(
                    f"DGL-LEVEL|止盈反弹计算|层级={self.level_id}|方向=做空|最低={self._min_price}|比例={tp_rebound_ratio}|价格={rebound_price}"
                )
                return rebound_price
        # else:
        #     self.logger.debug(
        #         f"DGL-LEVEL|反弹不可用|层级={self.level_id}|状态={self.current_state}|最高={self._max_price}|最低={self._min_price}"
        #     )
        return None

    @property
    def is_open(self) -> bool:
        """是否已开仓（计算属性）。"""
        return self.current_state in [
            DynamicGridLevelState.WAITING_TP_ACTIVE,
            DynamicGridLevelState.WAITING_TP_REBOUND,
            DynamicGridLevelState.WAITING_NEXT_CLOSE,
        ]

    def _check_and_activate_if_ready(self) -> list[DynamicGridEvent]:
        """检查前置条件并在满足时立即激活"""
        events = []
        if self.current_state == DynamicGridLevelState.WAITING_PREV_OP:
            # 第一层不需要这个逻辑（第一层初始状态就是 WAITING_OP_ACTIVE）
            # 检查前一个级别是否已开仓，如果是则激活当前级别
            if not self.is_first_level() and self.prev and self.prev.is_open:
                events.append(self.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE))
                self.logger.info(
                    f"DGL-LEVEL|激活就绪|层级={self.level_id}|动作=to_WAITING_OP_ACTIVE"
                )
        return events

    def _maintain_price_extremes(self, current_price: float) -> None:
        """统一维护最高/最低价：
        - 仅在回调状态(WAITING_OP_REBOUND/WAITING_TP_REBOUND)追踪极值
        - 其他状态将极值置为None
        """
        if self.current_state in [
            DynamicGridLevelState.WAITING_OP_REBOUND,
            DynamicGridLevelState.WAITING_TP_REBOUND,
        ]:
            old_max = self._max_price
            old_min = self._min_price

            if self._max_price is None or current_price > self._max_price:
                self._max_price = current_price
            if self._min_price is None or current_price < self._min_price:
                self._min_price = current_price

            # 记录价格极值变化
            if old_max != self._max_price or old_min != self._min_price:
                self.logger.debug(
                    f"DGL-LEVEL|极值|层级={self.level_id}|价格={current_price}|最高={self._max_price}|最低={self._min_price}"
                )
        else:
            if self._max_price is not None or self._min_price is not None:
                self.logger.debug(
                    f"DGL-LEVEL|重置极值|层级={self.level_id}|状态={self.current_state}"
                )
                self._max_price = None
                self._min_price = None

    def _success_event_to_order_record(self, event: DynamicGridEvent) -> dict:
        """将加/减仓成功事件解析为订单字典，便于存档与排查。"""
        base = float(event.base_amount if hasattr(event, "base_amount") else 0.0)
        quote = float(event.quote_amount if hasattr(event, "quote_amount") else 0.0)
        price = float(event.executed_price if hasattr(event, "executed_price") else 0.0)
        record: dict = {
            "ts": float(event.timestamp if hasattr(event, "timestamp") else 0.0),
            "pair": event.pair if hasattr(event, "pair") else self.pair,
            "level_id": event.level_id if hasattr(event, "level_id") else self.level_id,
            "is_short": event.is_short if hasattr(event, "is_short") else self.is_short,
            "base_amount": base,
            "quote_amount": quote,
            "executed_price": price,
            "type": event.event_type if hasattr(event, "event_type") else "success",
        }
        # 可选字段
        oid = event.order_id if hasattr(event, "order_id") else None
        if oid is not None:
            record["order_id"] = oid
        fee_b = event.fee_base if hasattr(event, "fee_base") else None
        if fee_b is not None:
            try:
                record["fee_base"] = float(fee_b)
            except Exception:
                record["fee_base"] = None
        fee_q = event.fee_quote if hasattr(event, "fee_quote") else None
        if fee_q is not None:
            try:
                record["fee_quote"] = float(fee_q)
            except Exception:
                record["fee_quote"] = None
        reason = event.reason if hasattr(event, "reason") else None
        if reason is not None:
            record["reason"] = reason
        return record

    def _handle_error_state_recovery(self) -> list[DynamicGridEvent]:
        """处理错误状态自动恢复"""
        events = []
        if self.current_state == DynamicGridLevelState.ERROR:
            self.logger.info(
                f"级别 {self.level_id}: 从错误状态恢复，保留最后价格={self._last_price}"
            )
            events.append(self.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
        return events

    def _handle_data_update_event(self, event: DynamicGridDataUpdateEvent) -> list[DynamicGridEvent]:
        """处理数据更新事件"""
        events = []
        current_price = float(event.data["close"])
        self._last_price = current_price

        # 直接使用事件时间
        current_time = event.data.get("current_time")

        # 统一维护价格极值（仅在回调状态追踪，其余状态置空）
        self._maintain_price_extremes(current_price)

        self.logger.debug(
            f"DGL-LEVEL|价格|层级={self.level_id}|状态={self.current_state.value}|价格={current_price}|K线时间={event.china_time_str}"
        )
        self.logger.debug(
            f"DGL-LEVEL|状态|层级={self.level_id}|状态={self.current_state.value}|开仓价={self.op_price}|基础数量={self.base_amount}|计价数量={self.quote_amount}|K线时间={event.china_time_str}"
        )

        # 根据当前状态处理价格更新
        if self.current_state == DynamicGridLevelState.WAITING_PREV_OP:
            # 使用统一的激活检查方法
            events.extend(self._check_and_activate_if_ready())

        elif self.current_state == DynamicGridLevelState.WAITING_OP_ACTIVE:
            # 第一层在 WAITING_OP_ACTIVE 状态时，需要检查是否达到激活价格
            # 但第一层不应该通过 DataUpdateEvent 触发状态转换到 WAITING_OP_REBOUND
            # 第一层只能通过 EntrySignalEvent 触发状态转换
            if self.is_first_level():
                # 第一层在 WAITING_OP_ACTIVE 状态时，只更新价格，不触发状态转换
                # 状态转换由 EntrySignalEvent 处理
                self.logger.debug(
                    f"DGL-LEVEL|第一层价格更新|层级={self.level_id}|"
                    f"价格={current_price}|状态=WAITING_OP_ACTIVE|等待信号"
                )
                return events
            
            # 后续层：检查前一层是否还持仓，如果前一层平仓了，则回退到等待前置条件
            if self.prev and not self.prev.is_open:
                events.append(self.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
                self.logger.info(f"DGL-LEVEL|前层未开|层级={self.level_id}|动作=to_WAITING_PREV_OP")
                return events

            target_op = self.op_active_price
            if not target_op:
                self.logger.debug(f"DGL-LEVEL|无开仓激活|层级={self.level_id}")
            else:
                # 检查是否达到开仓激活条件
                price_triggered = (not self.is_short and current_price <= target_op) or (
                    self.is_short and current_price >= target_op
                )
                try:
                    diff_pct = abs((current_price - target_op) / current_price * 100)
                except Exception:
                    diff_pct = 0.0
                self.logger.debug(
                    f"DGL-LEVEL|开仓激活|层级={self.level_id}|前层开仓价={self.prev.op_price if self.prev else 'None'}|价格={current_price}|目标={target_op}|剩余百分比={diff_pct:.4f}%"
                )

                if price_triggered:
                    self.logger.info(
                        f"DGL-LEVEL|开仓激活命中|层级={self.level_id}|目标={target_op}|前层开仓价={self.prev.op_price if self.prev else 'None'}"
                    )
                    events.append(self.transition_to(DynamicGridLevelState.WAITING_OP_REBOUND))

        elif self.current_state == DynamicGridLevelState.WAITING_OP_REBOUND:
            # 第一层在 WAITING_OP_REBOUND 状态时，需要检查是否达到反弹价格
            # 第一层也可以进入这个状态（当 skip_first_level_rebound=false 时）
            
            # 若存在加仓挂单，优先打印等待时长（使用K线时间而非系统时间）
            if self._pending_action == "add":
                current_time = event.data.get("current_time")
                if isinstance(current_time, datetime):
                    current_ts = current_time.timestamp()
                else:
                    current_ts = float(current_time or 0.0)
                since_ts = float(self._pending_since or 0.0)
                age = (current_ts - since_ts) if (current_ts and since_ts) else 0.0
                self.logger.debug(f"DGL-LEVEL|等待加仓|层级={self.level_id}|等待时间={age:.1f}s")
            # 检查前置条件：
            # - 第一层：不需要前一层（第一层没有前一层）
            # - 后续层：必须有前一层且前一层已开仓
            if not self.is_first_level():
                if not self.prev or not self.prev.op_price:
                    self.logger.warning(
                        f"DGL-LEVEL|开仓反弹前层未开|层级={self.level_id}|动作=to_WAITING_PREV_OP"
                    )
                    events.append(self.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
                    self._pending_action = None
                    self._pending_since = None
                    return events

            target_rebound = self.op_rebound_price
            if target_rebound:
                # 检查是否达到回调开仓触发条件
                price_triggered = (not self.is_short and current_price >= target_rebound) or (
                    self.is_short and current_price <= target_rebound
                )

                try:
                    diff_pct = abs((current_price - target_rebound) / current_price * 100)
                except Exception:
                    diff_pct = 0.0
                base_ref = self._min_price if not self.is_short else self._max_price
                self.logger.debug(
                    f"DGL-LEVEL|开仓反弹|层级={self.level_id}|base_ref={base_ref}|价格={current_price}|目标={target_rebound}|剩余百分比={diff_pct:.4f}%|待处理={self._pending_action}"
                )

                if price_triggered:
                    if self._pending_action == "add":
                        self.logger.debug(f"DGL-LEVEL|跳过加仓待处理|层级={self.level_id}")
                        return events

                    # 根据 is_base_mode 计算 base_amount 和 quote_amount
                    # is_base_mode=True（币本位）: config.amount 是 base 货币数量（如 ETH）
                    # is_base_mode=False（U本位，默认）: config.amount 是 quote 货币金额（如 USDT）
                    is_base_mode = getattr(self.config, 'is_base_mode', False)
                    config_amount = float(self.config.amount)
                    
                    # 安全检查：避免除零或产生异常值
                    if target_rebound is None or target_rebound <= 0:
                        self.logger.error(
                            f"DGL-LEVEL|开仓反弹价格无效|层级={self.level_id}|target_rebound={target_rebound}"
                        )
                        return events
                    
                    try:
                        if is_base_mode:
                            # 币本位：config.amount 是 base 数量，需要计算 quote_amount
                            base_amount = config_amount
                            quote_amount = base_amount * float(target_rebound)
                        else:
                            # U本位（默认）：config.amount 是 quote 金额，需要计算 base_amount
                            quote_amount = config_amount
                            base_amount = quote_amount / float(target_rebound)
                        
                        if math.isnan(base_amount) or math.isinf(base_amount) or math.isnan(quote_amount) or math.isinf(quote_amount):
                            self.logger.error(
                                f"DGL-LEVEL|开仓金额计算异常|层级={self.level_id}|"
                                f"config_amount={config_amount}|target_rebound={target_rebound}|"
                                f"base_amount={base_amount}|quote_amount={quote_amount}|币本位={is_base_mode}"
                            )
                            return events
                    except (ValueError, ZeroDivisionError) as e:
                        self.logger.error(
                            f"DGL-LEVEL|开仓金额计算失败|层级={self.level_id}|错误={e}|"
                            f"config_amount={config_amount}|target_rebound={target_rebound}|币本位={is_base_mode}"
                        )
                        return events

                    # 第一层使用 EntryTriggerEvent，后续层使用 AddTriggerEvent
                    if self.is_first_level():
                        trigger_event = DynamicGridEntryTriggerEvent.create(
                            is_short=self.is_short,
                            base_amount=base_amount,
                            quote_amount=quote_amount,
                            target_price=target_rebound,
                            pair=self.pair,
                            current_time=event.data.get("current_time"),
                        )
                    else:
                        trigger_event = DynamicGridAddTriggerEvent.create(
                            level_id=self.level_id,
                            is_short=self.is_short,
                            base_amount=base_amount,
                            quote_amount=quote_amount,
                            target_price=target_rebound,
                            pair=self.pair,
                            current_time=event.data.get("current_time"),
                        )
                    events.append(trigger_event)
                    self._pending_action = "add"
                    current_time = event.data.get("current_time")
                    if isinstance(current_time, datetime):
                        self._pending_since = current_time.timestamp()
                    else:
                        self._pending_since = current_time
                    self.logger.info("-" * 40)
                    self.logger.info(
                        f"DGL-LEVEL|加仓触发|层级={self.level_id}|目标={target_rebound}|前层开仓价={self.prev.op_price if self.prev else 'None'}|"
                        f"价格={current_price}|币本位={is_base_mode}|配置数量={config_amount}|quote={quote_amount}|base={base_amount}"
                    )
                    # 不转换状态，等待成功事件确认
            # 其余情况由下一次数据更新继续等待
            else:
                self.logger.warning(
                    f"DGL-LEVEL|开仓反弹无|层级={self.level_id}|前层开仓价={self.prev.op_price if self.prev else 'None'}"
                )

        elif self.current_state == DynamicGridLevelState.WAITING_TP_ACTIVE:
            # 检查下一层是否已开仓，如果开仓了，应该转换到 WAITING_NEXT_CLOSE
            if self.next and self.next.is_open:
                events.append(self.transition_to(DynamicGridLevelState.WAITING_NEXT_CLOSE))
                self.logger.info(
                    f"DGL-LEVEL|下一层已开仓|层级={self.level_id}|下一层={self.next.level_id}|"
                    f"转换到WAITING_NEXT_CLOSE"
                )
                return events
            
            # 检查是否禁用前若干层内部止盈
            if self.level_id <= self.skip_levels_exit_number:
                self.logger.debug(
                    f"DGL-LEVEL|跳过内部止盈|层级={self.level_id}|限制={self.skip_levels_exit_number}"
                )
                return events

            target_tp = self.tp_active_price
            if not target_tp:
                self.logger.debug(f"DGL-LEVEL|无止盈激活|层级={self.level_id}")
            else:
                # 检查是否达到止盈条件
                price_triggered = (not self.is_short and current_price >= target_tp) or (
                    self.is_short and current_price <= target_tp
                )
                try:
                    diff_pct = abs((current_price - target_tp) / current_price * 100)
                except Exception:
                    diff_pct = 0.0
                self.logger.debug(
                    f"DGL-LEVEL|止盈激活|层级={self.level_id}|开仓价={self.op_price}|价格={current_price}|目标={target_tp}|剩余百分比={diff_pct:.4f}%"
                )

                if price_triggered:
                    self.logger.info(
                        f"DGL-LEVEL|止盈激活命中|层级={self.level_id}|目标={target_tp}|开仓价={self.op_price}"
                    )
                    events.append(self.transition_to(DynamicGridLevelState.WAITING_TP_REBOUND))

        elif self.current_state == DynamicGridLevelState.WAITING_TP_REBOUND:
            # WAITING_TP_REBOUND 状态是即将止盈的状态，此时价格肯定在当前层开仓价格之上
            # 而下一层开仓需要价格在当前层开仓价格之下，这两个条件不可能同时满足
            # 因此，WAITING_TP_REBOUND 状态不应该转换到 WAITING_NEXT_CLOSE
            # 第一层在 WAITING_TP_REBOUND 状态时，只处理减仓触发，不进行状态转换
            # 状态转换由 ReduceSuccessEvent 处理（转换到 COMPLETED）
            if self.is_first_level():
                # 第一层在 WAITING_TP_REBOUND 状态时，只更新价格，不触发状态转换
                # 状态转换由 ExitSuccessEvent 处理（转换到 COMPLETED）
                self.logger.debug(
                    f"DGL-LEVEL|第一层止盈反弹等待|层级={self.level_id}|"
                    f"价格={current_price}|状态=WAITING_TP_REBOUND|等待减仓成功"
                )
                # 继续处理减仓触发逻辑，但不进行状态转换
            # 检查是否禁用前若干层内部止盈
            if self.level_id <= self.skip_levels_exit_number:
                self.logger.debug(
                    f"DGL-LEVEL|跳过止盈反弹|层级={self.level_id}|限制={self.skip_levels_exit_number}"
                )
                return events

            # 若存在减仓挂单，优先打印等待时长（使用K线时间而非系统时间）
            if self._pending_action == "reduce":
                current_time = event.data.get("current_time")
                if isinstance(current_time, datetime):
                    current_ts = current_time.timestamp()
                else:
                    current_ts = float(current_time or 0.0)
                since_ts = float(self._pending_since or 0.0)
                age = (current_ts - since_ts) if (current_ts and since_ts) else 0.0
                self.logger.debug(f"DGL-LEVEL|等待减仓|层级={self.level_id}|等待时间={age:.1f}s")
            target_rebound = self.tp_rebound_price
            if target_rebound:
                # 检查是否达到反弹完成条件
                price_triggered = (not self.is_short and current_price <= target_rebound) or (
                    self.is_short and current_price >= target_rebound
                )

                if price_triggered:
                    if self._pending_action == "reduce":
                        self.logger.debug(f"DGL-LEVEL|跳过减仓待处理|层级={self.level_id}")
                        return events
                    
                    # 减仓时直接使用开仓时存储的 quote_amount
                    # Freqtrade 使用比例计算：要减少的 base = |stake_amount| × 持仓量 / 持仓金额
                    # 所以传入开仓时的 quote_amount，Freqtrade 会自动计算正确的 base 数量
                    base_amount_to_reduce = float(self.base_amount or 0.0)
                    quote_amount_to_reduce = float(self.quote_amount or 0.0)  # 直接使用开仓时存储的金额
                    
                    # 安全检查：确保金额有效
                    if quote_amount_to_reduce <= 0 or math.isnan(quote_amount_to_reduce) or math.isinf(quote_amount_to_reduce):
                        self.logger.error(
                            f"DGL-LEVEL|减仓金额无效|层级={self.level_id}|quote_amount={quote_amount_to_reduce}"
                        )
                        return events
                    
                    self.logger.debug(
                        f"DGL-LEVEL|减仓金额|层级={self.level_id}|"
                        f"基础数量={base_amount_to_reduce}|计价金额={quote_amount_to_reduce}"
                    )
                    
                    # 发送减仓触发事件给外部系统
                    # 第一层使用 ExitTriggerEvent，后续层使用 ReduceTriggerEvent
                    if self.is_first_level():
                        trigger_event = DynamicGridExitTriggerEvent.create(
                            is_short=self.is_short,
                            base_amount=base_amount_to_reduce,
                            quote_amount=quote_amount_to_reduce,
                            target_price=target_rebound,
                            reason="take_profit",
                            pair=self.pair,
                            current_time=event.data.get("current_time"),
                        )
                    else:
                        trigger_event = DynamicGridReduceTriggerEvent.create(
                            level_id=self.level_id,
                            is_short=self.is_short,
                            base_amount=base_amount_to_reduce,
                            quote_amount=quote_amount_to_reduce,
                            target_price=target_rebound,
                            reason="take_profit",
                            pair=self.pair,
                            current_time=event.data.get("current_time"),
                        )
                    events.append(trigger_event)
                    self._pending_action = "reduce"
                    current_time = event.data.get("current_time")
                    if isinstance(current_time, datetime):
                        self._pending_since = current_time.timestamp()
                    else:
                        self._pending_since = current_time
                    self.logger.info("-" * 40)
                    self.logger.info(
                        f"DGL-LEVEL|减仓触发|层级={self.level_id}|目标={target_rebound}|开仓价={self.op_price}|价格={current_price}|"
                        f"基础数量={base_amount_to_reduce}|计价数量={quote_amount_to_reduce}"
                    )
                    # 不转换状态，等待成功事件确认
                else:
                    try:
                        diff_pct = abs((current_price - target_rebound) / current_price * 100)
                    except Exception:
                        diff_pct = 0.0
                    self.logger.debug(
                        f"DGL-LEVEL|止盈反弹|层级={self.level_id}|价格={current_price}|目标={target_rebound}|剩余百分比={diff_pct:.4f}%|待处理={self._pending_action}"
                    )
            # 其余情况由下一次数据更新继续等待
            else:
                self.logger.warning(
                    f"DGL-LEVEL|止盈反弹无|层级={self.level_id}|最高={self._max_price}|最低={self._min_price}"
                )

        elif self.current_state == DynamicGridLevelState.WAITING_NEXT_CLOSE:
            # 检查下一个级别是否已完成
            if self.next and self.next.current_state == DynamicGridLevelState.COMPLETED:
                events.append(self.transition_to(DynamicGridLevelState.WAITING_TP_ACTIVE))
                self.logger.info(f"DGL-LEVEL|后层完成转止盈激活|层级={self.level_id}")

        elif self.current_state == DynamicGridLevelState.COMPLETED:
            # COMPLETED 作为中间状态，应该在减仓成功时已经自动重置
            # 如果到达这里，说明状态转换有问题，记录警告
            self.logger.warning(
                f"DGL-LEVEL|异常：COMPLETED状态未自动重置|层级={self.level_id}|"
                f"COMPLETED应该作为中间状态，在减仓成功时已自动重置"
            )

        # 未实现损益改为属性即时计算
        return events

    def _handle_entry_signal_event(self, event: DynamicGridEntrySignalEvent) -> list[DynamicGridEvent]:
        """处理开仓信号事件
        
        当收到开仓信号时：
        - 如果 skip_first_level_rebound=true：直接生成 AddTriggerEvent 触发开仓
        - 如果 skip_first_level_rebound=false：状态变为 WAITING_OP_REBOUND，等待价格反弹
        """
        events = []
        
        target_price = event.data.get("target_price", event.data.get("price", "N/A"))
        self.logger.info(
            f"DGL-LEVEL|收到开仓信号|层级={self.level_id}|"
            f"当前状态={self.current_state.value}|"
            f"信号价格={target_price}|"
            f"信号方向={'空头' if event.is_short else '多头'}|"
            f"层级方向={'空头' if self.is_short else '多头'}"
        )
        
        # 只有第一层才能响应开仓信号
        if not self.is_first_level():
            self.logger.debug(
                f"DGL-LEVEL|开仓信号忽略|层级={self.level_id}|原因=非第一层"
            )
            return events
        
        # 第一层必须在 WAITING_OP_ACTIVE 状态才能响应信号
        # COMPLETED 作为中间状态，在减仓成功时已经自动重置，不应该到达这里
        if self.current_state != DynamicGridLevelState.WAITING_OP_ACTIVE:
            self.logger.debug(
                f"DGL-LEVEL|开仓信号忽略|层级={self.level_id}|"
                f"当前状态={self.current_state.value}|原因=状态不匹配（需要WAITING_OP_ACTIVE）"
            )
            return events
        
        # 如果已有待处理的加仓挂单，忽略新信号
        if self._pending_action == "add":
            self.logger.debug(
                f"DGL-LEVEL|开仓信号忽略|层级={self.level_id}|"
                f"当前状态={self.current_state.value}|原因=已有待处理加仓挂单"
            )
            return events
        
        # 检查方向是否匹配
        if event.is_short != self.is_short:
            self.logger.warning(
                f"DGL-LEVEL|开仓信号方向不匹配|层级={self.level_id}|"
                f"信号方向={'空头' if event.is_short else '多头'}|"
                f"层级方向={'空头' if self.is_short else '多头'}"
            )
            return events
        
        # 从信号事件中获取开仓金额和价格
        # 注意：EntrySignalEvent 的 target_price 存储在 data 字典中，不是属性
        base_amount = event.data.get("base_amount", 0)
        quote_amount = event.data.get("quote_amount", 0)
        target_price = event.data.get("target_price", event.data.get("price", 0))
        
        if not base_amount or not quote_amount or not target_price:
            self.logger.warning(
                f"DGL-LEVEL|开仓信号数据不完整|层级={self.level_id}|"
                f"base_amount={base_amount}|quote_amount={quote_amount}|target_price={target_price}"
            )
            return events
        
        # 更新最后价格（用于后续计算）
        self._last_price = target_price
        
        # 根据 skip_first_level_rebound 决定行为
        if self.skip_first_level_rebound:
            # 直接开仓：生成 EntryTriggerEvent（第一层开仓）
            trigger_event = DynamicGridEntryTriggerEvent.create(
                is_short=self.is_short,
                base_amount=base_amount,
                quote_amount=quote_amount,
                target_price=target_price,
                pair=event.pair,
                current_time=event.datetime,  # 使用 event.datetime 而不是 event.current_time
            )
            
            # 添加信号信息到事件数据中
            trigger_event.data.update({
                "signal_name": event.data.get("signal_name", ""),
                "custom_data": event.data.get("custom_data", {}),
            })
            
            events.append(trigger_event)
            # 记录挂单状态，避免重复触发
            self._pending_action = "add"
            # 使用 event.datetime 或 event.timestamp
            if event.datetime:
                self._pending_since = event.datetime.timestamp()
            elif event.timestamp:
                self._pending_since = event.timestamp
            else:
                self._pending_since = datetime.now(timezone.utc).timestamp()
            
            self.logger.info(
                f"DGL-LEVEL|开仓信号处理(直接开仓)|层级={self.level_id}|"
                f"目标价格={target_price}|基础数量={base_amount}|计价数量={quote_amount}|"
                f"信号={event.data.get('signal_name', 'N/A')}"
            )
        else:
            # 等待反弹：状态变为 WAITING_OP_REBOUND
            events.append(self.transition_to(DynamicGridLevelState.WAITING_OP_REBOUND))
            # 初始化价格极值（用于反弹计算）
            self._maintain_price_extremes(target_price)
            
            self.logger.info(
                f"DGL-LEVEL|开仓信号处理(等待反弹)|层级={self.level_id}|"
                f"信号价格={target_price}|信号={event.data.get('signal_name', 'N/A')}|"
                f"状态=WAITING_OP_REBOUND"
            )
        
        return events

    def _handle_exit_signal_event(self, event: DynamicGridExitSignalEvent) -> list[DynamicGridEvent]:
        """处理平仓信号事件"""
        pass

    def _handle_add_success_event(self, event: DynamicGridAddSuccessEvent) -> list[DynamicGridEvent]:
        """处理加仓成功事件"""
        events = []
        if event.level_id == self.level_id and event.is_short == self.is_short:
            # 检查是否已经有开仓价格
            # 如果已经有 op_price，说明之前已经开仓了，这个事件可能是延迟的订单成交
            # 在 WAITING_TP_REBOUND 状态下，应该已经有 op_price（因为只有开仓后才能进入止盈状态）
            if self._op_price is not None:
                # 检查事件价格是否与当前开仓价格匹配（允许小的误差）
                price_diff = abs(float(event.executed_price) - float(self._op_price))
                price_diff_pct = (price_diff / float(self._op_price)) * 100 if self._op_price > 0 else 0
                
                # 如果价格差异很小（< 0.1%），可能是同一个订单的重复事件，忽略
                if price_diff_pct < 0.1:
                    self.logger.warning(
                        f"DGL-LEVEL|加仓成功事件已处理(重复或延迟)|层级={self.level_id}|"
                        f"当前状态={self.current_state}|已有开仓价={self._op_price}|"
                        f"事件价格={event.executed_price}|价格差异={price_diff_pct:.4f}%|"
                        f"忽略此事件"
                    )
                    return events
                else:
                    # 价格差异较大，可能是新的开仓，但状态不允许
                    # 记录事件时间戳以便分析事件顺序
                    event_time_str = event.china_time_str if hasattr(event, 'china_time_str') else (
                        event.datetime.strftime('%Y-%m-%d %H:%M:%S') if event.datetime else 'N/A'
                    )
                    self.logger.error(
                        f"DGL-LEVEL|加仓成功但状态不允许转换|层级={self.level_id}|"
                        f"当前状态={self.current_state}|已有开仓价={self._op_price}|"
                        f"事件价格={event.executed_price}|价格差异={price_diff_pct:.4f}%|"
                        f"事件时间={event_time_str}|"
                        f"可能原因：订单延迟成交或事件处理顺序问题|忽略此事件"
                    )
                    # 不更新开仓数据，保持当前状态
                    return events
            
            # 确认开仓数据（只有在没有 op_price 的情况下才更新）
            self._op_price = event.executed_price
            self._base_amount = event.base_amount
            self._quote_amount = event.quote_amount
            # 累计统计与存档（仅在启用性能指标时）
            if self.enable_performance_metrics:
                self._cum_fee_quote += float(event.fee_quote or 0.0)
                self._cum_executed_quote += float(self._quote_amount or 0.0)
                self._executed_orders.append(self._success_event_to_order_record(event))
            
            # 只有在允许的状态下才转换到 WAITING_TP_ACTIVE
            if self.current_state in [
                DynamicGridLevelState.WAITING_OP_ACTIVE,
                DynamicGridLevelState.WAITING_OP_REBOUND,
                DynamicGridLevelState.WAITING_PREV_OP,
            ]:
                events.append(self.transition_to(DynamicGridLevelState.WAITING_TP_ACTIVE))
                self.logger.info(f"DGL-LEVEL|加仓成功|层级={self.level_id}|开仓价={self._op_price}")
            else:
                # 使用 error 级别确保日志能够输出
                self.logger.error(
                    f"DGL-LEVEL|加仓成功但状态不允许转换|层级={self.level_id}|"
                    f"当前状态={self.current_state}|开仓价={self._op_price}|"
                    f"事件level_id={event.level_id}|事件价格={event.executed_price}|"
                    f"可能原因：订单延迟成交或事件处理顺序问题|不更新状态"
                )
                # 不进行状态转换，保持当前状态
                # 这种情况下，可能是之前的开仓订单延迟成交，应该忽略
            # 清除加仓挂单标记
            self._pending_action = None
            self._pending_since = None
        return events

    def _handle_reduce_success_event(
        self, event: DynamicGridReduceSuccessEvent
    ) -> list[DynamicGridEvent]:
        """处理减仓成功事件"""
        events = []
        if event.level_id == self.level_id and event.is_short == self.is_short:
            if not self.op_price:
                self.logger.warning(f"DGL-LEVEL|减仓成功无开仓|层级={self.level_id}")
                return events
            
            # 安全获取 base_amount，避免 None 值导致计算异常
            base_amount = self.base_amount
            if base_amount is None or base_amount <= 0:
                # 如果 base_amount 无效，尝试从事件中获取
                base_amount = getattr(event, 'base_amount', None)
                if base_amount is None or base_amount <= 0:
                    self.logger.warning(
                        f"DGL-LEVEL|减仓成功但base_amount无效|层级={self.level_id}|"
                        f"self.base_amount={self.base_amount}|event.base_amount={getattr(event, 'base_amount', None)}"
                    )
                    # 使用一个很小的值避免除零，但不影响实际计算
                    base_amount = 0.0
            
            # 安全计算已实现损益，确保所有值都是有效的浮点数
            try:
                executed_price = float(event.executed_price) if event.executed_price is not None else 0.0
                op_price = float(self.op_price) if self.op_price is not None else 0.0
                base_amount_float = float(base_amount) if base_amount is not None else 0.0
                
                # 检查是否有无效值（NaN 或 Inf）
                if not (isinstance(executed_price, (int, float)) and isinstance(op_price, (int, float)) and isinstance(base_amount_float, (int, float))):
                    self.logger.error(
                        f"DGL-LEVEL|减仓成功但价格数据无效|层级={self.level_id}|"
                        f"executed_price={executed_price}|op_price={op_price}|base_amount={base_amount_float}"
                    )
                    pnl = 0.0
                else:
                    if math.isnan(executed_price) or math.isinf(executed_price) or \
                       math.isnan(op_price) or math.isinf(op_price) or \
                       math.isnan(base_amount_float) or math.isinf(base_amount_float):
                        self.logger.error(
                            f"DGL-LEVEL|减仓成功但价格数据包含NaN/Inf|层级={self.level_id}|"
                            f"executed_price={executed_price}|op_price={op_price}|base_amount={base_amount_float}"
                        )
                        pnl = 0.0
                    else:
                        # 计算已实现损益
                        if not self.is_short:
                            pnl = (executed_price - op_price) * base_amount_float
                        else:
                            pnl = (op_price - executed_price) * base_amount_float
                        
                        # 检查计算结果是否异常
                        if math.isnan(pnl) or math.isinf(pnl):
                            self.logger.error(
                                f"DGL-LEVEL|减仓成功但PnL计算结果异常|层级={self.level_id}|"
                                f"pnl={pnl}|executed_price={executed_price}|op_price={op_price}|base_amount={base_amount_float}"
                            )
                            pnl = 0.0
            except (ValueError, TypeError) as e:
                self.logger.error(
                    f"DGL-LEVEL|减仓成功但PnL计算失败|层级={self.level_id}|错误={e}|"
                    f"executed_price={event.executed_price}|op_price={self.op_price}|base_amount={base_amount}"
                )
                pnl = 0.0
            # 判断是否是止盈：检查reason中是否包含止盈相关的关键词
            # 支持：take_profit, global_take_profit, roi, exit_signal, force_exit, custom_exit 等
            reason_lower = (event.reason or "").lower()
            is_take_profit = any(keyword in reason_lower for keyword in [
                "take_profit", "roi", "exit_signal", "force_exit", "custom_exit", 
                "sold_on_exchange", "emergency_exit"
            ]) and not any(keyword in reason_lower for keyword in [
                "stop_loss", "stoploss", "liquidation", "trailing_stop_loss"
            ])
            
            # 仅在启用性能指标时计算和累加已实现损益
            if self.enable_performance_metrics:
                # 确保 pnl 是有效数值后再累加
                if math.isnan(pnl) or math.isinf(pnl):
                    self.logger.error(
                        f"DGL-LEVEL|减仓成功但PnL无效，跳过累加|层级={self.level_id}|pnl={pnl}"
                    )
                    pnl = 0.0
                
                if is_take_profit and pnl > 0:
                    # 确保累加后不会产生异常值
                    new_profit = self._realized_profit + pnl
                    if not (math.isnan(new_profit) or math.isinf(new_profit)):
                        self._realized_profit = new_profit
                        self.win_count += 1
                        self.logger.info(
                            f"DGL-LEVEL|减仓盈亏正|层级={self.level_id}|盈亏={pnl}|总正={self._realized_profit}|盈利次数={self.win_count}"
                        )
                    else:
                        self.logger.error(
                            f"DGL-LEVEL|减仓盈亏累加后异常，跳过|层级={self.level_id}|"
                            f"原值={self._realized_profit}|增量={pnl}|新值={new_profit}"
                        )
                elif pnl < 0:
                    # 确保累加后不会产生异常值
                    new_loss = self._realized_loss + abs(pnl)
                    if not (math.isnan(new_loss) or math.isinf(new_loss)):
                        self._realized_loss = new_loss
                        self.loss_count += 1
                        self.logger.info(
                            f"DGL-LEVEL|减仓盈亏负|层级={self.level_id}|盈亏={pnl}|总负={self._realized_loss}|亏损次数={self.loss_count}"
                        )
                    else:
                        self.logger.error(
                            f"DGL-LEVEL|减仓亏损累加后异常，跳过|层级={self.level_id}|"
                            f"原值={self._realized_loss}|增量={abs(pnl)}|新值={new_loss}"
                        )
                else:
                    if pnl > 0:
                        # 确保累加后不会产生异常值
                        new_profit = self._realized_profit + pnl
                        if not (math.isnan(new_profit) or math.isinf(new_profit)):
                            self._realized_profit = new_profit
                            self.win_count += 1
                            self.logger.info(
                                f"DGL-LEVEL|减仓盈亏正非网格|层级={self.level_id}|盈亏={pnl}|总正={self._realized_profit}|盈利次数={self.win_count}"
                            )
                        else:
                            self.logger.error(
                                f"DGL-LEVEL|减仓盈亏累加后异常，跳过|层级={self.level_id}|"
                                f"原值={self._realized_profit}|增量={pnl}|新值={new_profit}"
                            )
                # 修复：正确处理 fee_quote 为 None 的情况
                fee_quote = event.fee_quote if hasattr(event, "fee_quote") else None
                self._cum_fee_quote += float(fee_quote or 0.0)
                self._cum_executed_quote += float(
                    event.quote_amount if hasattr(event, "quote_amount") else 0.0
                )
                self._executed_orders.append(self._success_event_to_order_record(event))
            # 与加仓成功路径对齐：显式清除挂单标记
            self._pending_action = None
            self._pending_since = None
            
            # 如果当前状态是 WAITING_TP_REBOUND，减仓成功后应该转换到 COMPLETED
            # COMPLETED 作为中间状态，立即重置到初始状态
            if self.current_state == DynamicGridLevelState.WAITING_TP_REBOUND:
                # 减仓成功，先转换到 COMPLETED 状态（中间状态）
                events.append(self.transition_to(DynamicGridLevelState.COMPLETED))
                self.logger.info(
                    f"DGL-LEVEL|减仓成功完成|层级={self.level_id}|状态=COMPLETED"
                )
                
                # COMPLETED 作为中间状态，立即重置到初始状态
                # 第一层：重置到 WAITING_OP_ACTIVE
                # 其他层：重置到 WAITING_PREV_OP
                if self.is_first_level():
                    # 第一层：清空持仓数据，重置到 WAITING_OP_ACTIVE
                    self._op_price = None
                    self._max_price = None
                    self._min_price = None
                    self._base_amount = None
                    self._quote_amount = None
                    self._pending_action = None
                    self._pending_since = None
                    # 保留 _last_price，以便后续计算
                    events.append(self.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE))
                    self.logger.info(
                        f"DGL-LEVEL|第一层完成重置|层级={self.level_id}|"
                        f"新状态=WAITING_OP_ACTIVE|最后价格={self._last_price}"
                    )
                else:
                    # 其他层：重置到 WAITING_PREV_OP
                    self.reset()
                    events.extend(self._check_and_activate_if_ready())  # 立即检查是否可以激活
            else:
                # 其他状态（如 WAITING_OP_REBOUND）的减仓成功，重置状态
                self.reset()
                events.extend(self._check_and_activate_if_ready())  # 立即检查是否可以激活
        # DGL-LEVEL|REDUCE_PNL line intentionally removed (duplicate)
        return events

    def _handle_global_exit_success_event(
        self, event: DynamicGridGlobalExitSuccessEvent
    ) -> list[DynamicGridEvent]:
        """处理全局平仓成功事件"""
        events = []
        if event.is_short == self.is_short and event.pair == self.pair:
            # 累计统计与存档（仅在启用性能指标时）
            if self.enable_performance_metrics:
                # 修复：正确处理 fee_quote 为 None 的情况
                fee_quote = event.fee_quote if hasattr(event, "fee_quote") else None
                self._cum_fee_quote += float(fee_quote or 0.0)
                self._cum_executed_quote += float(self._quote_amount or 0.0)
                self._executed_orders.append(self._success_event_to_order_record(event))
            self.reset()
            self.logger.info(f"DGL-LEVEL|全局退出成功|层级={self.level_id}")
        return events

    def handle_order_failure(self, reason: str) -> list[DynamicGridEvent]:
        """处理订单失败"""
        events = []
        if self.current_state in [
            DynamicGridLevelState.WAITING_OP_REBOUND,
            DynamicGridLevelState.WAITING_TP_REBOUND,
        ]:
            events.append(self.transition_to(DynamicGridLevelState.ERROR))
            self.logger.error(
                f"DGL-LEVEL|订单失败|层级={self.level_id}|原因={reason}|动作=to_ERROR"
            )
            # ERROR状态自动恢复到WAITING_PREV_OP
            events.append(self.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
            self.logger.info(f"DGL-LEVEL|错误恢复|层级={self.level_id}|动作=to_WAITING_PREV_OP")
            # 立即检查是否可以激活
            events.extend(self._check_and_activate_if_ready())
            # 清除挂单标记
            self._pending_action = None
            self._pending_since = None
        return events

    # 取消未实现损益缓存，使用属性即时计算

    def process_event(self, event: DynamicGridEvent) -> list[DynamicGridEvent]:
        """处理事件并返回新事件列表"""
        # self.last_event_time = event.timestamp or datetime.now(timezone.utc).timestamp()
        self.last_event_time = event.china_time_str
        events: list[DynamicGridEvent] = []
        # self.logger.debug(f"处理事件: 类型={event.event_type}, 数据={event.data}")
        events.extend(self._handle_error_state_recovery())
        if isinstance(event, DynamicGridDataUpdateEvent):
            events.extend(self._handle_data_update_event(event))
        elif isinstance(event, DynamicGridAddSuccessEvent):
            events.extend(self._handle_add_success_event(event))
        elif isinstance(event, DynamicGridReduceSuccessEvent):
            events.extend(self._handle_reduce_success_event(event))
        elif isinstance(event, DynamicGridGlobalExitSuccessEvent):
            events.extend(self._handle_global_exit_success_event(event))
        elif isinstance(event, DynamicGridEntrySignalEvent):
            events.extend(self._handle_entry_signal_event(event))
        elif isinstance(event, DynamicGridExitSignalEvent):
            events.extend(self._handle_exit_signal_event(event))
        return events

    def transition_to(self, to_state: DynamicGridLevelState) -> DynamicGridStateChangeEvent:
        if to_state not in VALID_TRANSITIONS.get(self.current_state, []):
            self.logger.error(
                f"级别 {self.level_id}: 无效状态转换 {self.current_state} -> {to_state}"
            )
            to_state = DynamicGridLevelState.ERROR
        # 不再强制要求转换到 WAITING_OP_REBOUND 前已有反弹价格
        from_state_value = self.current_state.value
        self.current_state = to_state
        self.logger.info("-" * 30)
        self.logger.info(f"DGL-LEVEL|状态变更|层级={self.level_id}|到={to_state}")
        # 当进入回调等待状态时，立即用当前价格初始化极值，避免管理器提示缺失
        if to_state in (
            DynamicGridLevelState.WAITING_OP_REBOUND,
            DynamicGridLevelState.WAITING_TP_REBOUND,
        ):
            if self._last_price is not None:
                self._maintain_price_extremes(float(self._last_price))
        return DynamicGridStateChangeEvent.create(
            level_id=self.level_id,
            from_state=from_state_value,
            to_state=to_state.value,
            pair=self.pair,
            current_time=None,  # 状态变更事件不需要时间
        )

    def reset(self) -> None:
        """重置级别状态
        
        注意：不清空 _last_price，以便重置后可以立即检查激活条件
        """
        self.current_state = DynamicGridLevelState.WAITING_PREV_OP
        self._op_price = None
        # 不清空 _last_price，保留最后价格以便重置后可以立即检查激活条件
        # self._last_price = None
        self._max_price = None
        self._min_price = None
        self._base_amount = None
        self._quote_amount = None
        # 清除挂单标记，确保 pending 在任何复位路径下都被清空
        self._pending_action = None
        self._pending_since = None
        # 未实现损益为属性，无需重置
        self.logger.debug(f"DGL-LEVEL|重置|层级={self.level_id}|保留最后价格={self._last_price}")

    def to_format_status(self) -> str:
        """格式化状态输出（中文）"""
        direction_str = "short" if self.is_short else "long"
        status = f"网格级别 {self.level_id} (方向: {direction_str})\n"
        status += f"  当前状态: {self.current_state.value}\n"
        status += f"  是否开仓: {'是' if self.is_open else '否'}\n"
        status += f"  开仓价格: {self.op_price if self.op_price else '无'}\n"
        status += f"  最新价格: {self._last_price if self._last_price else '无'}\n"
        status += f"  目标开仓价格: {self.op_active_price or self.op_rebound_price if self.op_active_price or self.op_rebound_price else '无'}\n"
        status += f"  目标止盈价格: {self.tp_active_price or self.tp_rebound_price if self.tp_active_price or self.tp_rebound_price else '无'}\n"
        status += f"  历史最高价: {self.max_price if self.max_price else '无'}\n"
        status += f"  历史最低价: {self.min_price if self.min_price else '无'}\n"
        status += f"  基础币数量: {self.base_amount if self.base_amount else '无'}\n"
        status += f"  计价币数量: {self.quote_amount if self.quote_amount else '无'}\n"
        status += f"  未实现损益: {self.unrealized_pnl}\n"
        status += f"  已实现正收益: {self.realized_profit}\n"
        status += f"  已实现负收益: {self.realized_loss}\n"
        status += f"  累计手续费: {self.total_fee_quote}\n"
        status += f"  累计成交金额: {self.total_executed_quote}\n"
        status += f"  盈利次数: {self.win_count}\n"
        status += f"  亏损次数: {self.loss_count}\n"
        status += f"  最后事件时间: {self.last_event_time if self.last_event_time else '无'}\n"
        status += f"  待处理动作: {self._pending_action if self._pending_action else '无'}\n"
        status += f"  待处理开始时间: {self._pending_since if self._pending_since else '无'}\n"
        status += f"  跳过第一层反弹: {'是' if self.skip_first_level_rebound else '否'}\n"
        status += f"  跳过层级退出数量: {self.skip_levels_exit_number}\n"
        status += f"  执行订单数: {len(self.executed_orders)}\n"
        self.logger.info(f"DGL-LEVEL|格式化状态|层级={self.level_id}")
        return status

    def level_performance_info(self) -> dict:
        """返回级别性能指标"""
        return {
            # 基本信息
            "level_id": self.level_id,
            "pair": self.pair,
            "is_short": self.is_short,
            "current_state": self.current_state.value,
            "is_open": self.is_open,
            # 价格信息
            "last_price": str(self._last_price) if self._last_price else None,
            "op_price": str(self.op_price) if self.op_price else None,
            "op_active_price": str(self.op_active_price) if self.op_active_price else None,
            "op_rebound_price": str(self.op_rebound_price) if self.op_rebound_price else None,
            "tp_active_price": str(self.tp_active_price) if self.tp_active_price else None,
            "tp_rebound_price": str(self.tp_rebound_price) if self.tp_rebound_price else None,
            "max_price": str(self.max_price) if self.max_price else None,
            "min_price": str(self.min_price) if self.min_price else None,
            # 持仓信息
            "base_amount": str(self.base_amount) if self.base_amount else None,
            "quote_amount": str(self.quote_amount) if self.quote_amount else None,
            # 损益信息
            "unrealized_pnl": str(self.unrealized_pnl),
            "realized_profit": str(self.realized_profit),
            "realized_loss": str(self.realized_loss),
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            # 统计信息
            "total_fee_quote": str(self.total_fee_quote),
            "total_executed_quote": str(self.total_executed_quote),
            "executed_orders_count": len(self.executed_orders),
            # 时间信息
            "last_event_time": self.last_event_time if self.last_event_time else None,
            # 待处理信息
            "pending_action": self._pending_action,
            "pending_since": float(self._pending_since) if self._pending_since else None,
            # 配置信息
            "skip_first_level_rebound": self.skip_first_level_rebound,
            "skip_levels_exit_number": self.skip_levels_exit_number,
        }
