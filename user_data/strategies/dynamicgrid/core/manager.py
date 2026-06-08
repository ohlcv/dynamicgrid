import logging
from typing import Optional, Dict
from .config import DynamicGridConfig, DynamicGridLevelConfig
from .event import (
    EventDirection,
    DynamicGridStateChangeEvent,
    DynamicGridAddSuccessEvent,
    DynamicGridExitSignalEvent,
    DynamicGridDataUpdateEvent,
    DynamicGridEvent,
    DynamicGridGlobalExitSuccessEvent,
    DynamicGridGlobalExitTriggerEvent,
    DynamicGridEntrySignalEvent,
    DynamicGridReduceSuccessEvent,
)
from .level import DynamicGridLevel, DynamicGridLevelState


logger = logging.getLogger("dynamicgrid.manager")


class DynamicGridManager:
    """敏捷网格级别的管理器（同步实现）"""

    def __init__(self, config: DynamicGridConfig):
        self.config = config
        self.pair = config.pair
        self.is_short = config.is_short
        self.enabled = config.enabled
        self.logger = logging.getLogger(
            f"dynamicgrid.manager.{self.pair}.{'short' if self.is_short else 'long'}"
        )
        self.levels = [
            DynamicGridLevel(
                cfg,
                parent_config=self.config,
            )
            for cfg in config.get_level_configs()
        ]
        # 创建字典索引用于 O(1) 查找
        self._level_dict: Dict[int, DynamicGridLevel] = {level.level_id: level for level in self.levels}
        self._update_level_relations()  # 调用动态更新方法
        # 初始化投资统计（按各层配置金额汇总）
        self._initial_quote_investment = float(
            sum(float(level.config.amount) for level in self.levels)
        )
        # 取消单独维护的当前价格，统一从各层的 _last_price 快照获取
        # Manager 不再缓存可聚合数据，按需从 levels 计算
        # 运行启停交由上层 enabled 控制，Manager 不再维护本地停用标志
        self.logger.info(
            f"DGL-MANAGER|初始化|交易对={self.pair}|方向={'做空' if self.is_short else '做多'}|层级数={len(self.levels)}"
        )

    # -------------------------------
    # 属性访问器
    # -------------------------------

    @property
    def total_base_amount(self) -> float:
        return sum((level.base_amount or 0.0) for level in self.levels)

    @property
    def total_levels(self) -> int:
        """当前管理器内的总层级数（计算属性）。"""
        return len(self.levels)

    @property
    def total_quote_amount(self) -> float:
        return sum((level.quote_amount or 0.0) for level in self.levels)

    @property
    def avg_entry_price(self) -> Optional[float]:
        """计算平均开仓价格，优化为一次遍历"""
        total_base = 0.0
        total_quote = 0.0
        for level in self.levels:
            if level.op_price and level.base_amount:
                total_base += level.base_amount
                total_quote += level.op_price * level.base_amount
        if total_base <= 0:
            return None
        return total_quote / total_base

    @property
    def unrealized_pnl(self) -> float:
        current_price = self.current_price
        if not current_price or not self.avg_entry_price or self.total_base_amount <= 0:
            return 0.0
        result = (
            (current_price - self.avg_entry_price) * self.total_base_amount
            if not self.is_short
            else (self.avg_entry_price - current_price) * self.total_base_amount
        )
        return result

    @property
    def realized_profit(self) -> float:
        # 聚合各级别的已实现正收益
        return sum((level.realized_profit or 0.0) for level in self.levels)

    @property
    def realized_loss(self) -> float:
        # 聚合各级别的已实现负收益
        return sum((level.realized_loss or 0.0) for level in self.levels)

    @property
    def total_pnl(self) -> float:
        return self.unrealized_pnl + self.realized_profit - self.realized_loss

    @property
    def total_win_count(self) -> int:
        return sum(level.win_count for level in self.levels)

    @property
    def total_loss_count(self) -> int:
        return sum(level.loss_count for level in self.levels)

    @property
    def current_price(self) -> Optional[float]:
        """当前价格（计算属性）：从各层的最近价快照中回溯获取。"""
        for level in reversed(self.levels):
            if level._last_price is not None:
                return float(level._last_price)
        return None

    @property
    def current_time(self) -> Optional[str]:
        """当前时间（计算属性）：从各层级最近事件时间回溯获取时间字符串。"""
        for level in reversed(self.levels):
            if level.last_event_time:
                return level.last_event_time
        return None

    def start(self) -> None:
        self.logger.info("DGL-MANAGER|启动")

    def stop(self) -> None:
        self.logger.info("DGL-MANAGER|停止")

    def add_level(self, config: DynamicGridLevelConfig) -> None:
        """动态添加网格级别"""
        self.logger.debug(f"DGL-MANAGER|添加层级|层级={config.level_id}")
        new_level = DynamicGridLevel(
            config,
            parent_config=self.config,
        )
        self.levels.append(new_level)
        # 同步更新字典索引（O(1)）
        self._level_dict[new_level.level_id] = new_level
        self._update_level_relations()
        self._initial_quote_investment = float(self._initial_quote_investment) + float(
            config.amount
        )
        # 新层初始 WAITING_PREV_OP，如前一层已持仓则立刻就地激活到 WAITING_OP_ACTIVE（无需等待首根K线）
        if not new_level.is_first_level() and new_level.prev and new_level.prev.is_open:
            self.logger.info(f"DGL-MANAGER|新层即刻激活|层级={new_level.level_id}|因前层已持仓")
            new_level.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE)
        self.logger.info(f"DGL-MANAGER|添加层级完成|层级={config.level_id}|交易对={self.pair}")

    def remove_level(self, level_id: int) -> None:
        self.logger.debug(f"DGL-MANAGER|移除层级|层级={level_id}")
        # 先获取要删除的层级，用于计算差值
        removed_level = self._level_dict.get(level_id)
        # 从字典中删除（O(1)）
        if level_id in self._level_dict:
            del self._level_dict[level_id]
        # 从列表中删除（O(n)，但需要保持列表顺序）
        self.levels = [level for level in self.levels if level.level_id != level_id]
        self._update_level_relations()
        # 优化：减去被删除层级的金额，而不是重新计算所有层级
        if removed_level:
            self._initial_quote_investment = float(self._initial_quote_investment) - float(
                removed_level.config.amount
            )
        else:
            # 如果找不到层级，回退到重新计算（安全措施）
            self._initial_quote_investment = float(
                sum(float(level.config.amount) for level in self.levels)
            )
        self.logger.info(
            f"DGL-MANAGER|移除层级完成|层级={level_id}|交易对={self.pair}|初始投资={self._initial_quote_investment}"
        )

    def _update_level_relations(self) -> None:
        """动态更新网格级别的 prev 和 next 依赖关系"""
        self.logger.debug(f"DGL-MANAGER|关系更新|数量={len(self.levels)}")
        for i, level in enumerate(self.levels):
            level.prev = self.levels[i - 1] if i > 0 else None
            level.next = self.levels[i + 1] if i < len(self.levels) - 1 else None
            self.logger.debug(
                f"DGL-MANAGER|关系|层级={level.level_id}|前层={level.prev.level_id if level.prev else None}|后层={level.next.level_id if level.next else None}"
            )
        self.levels.sort(key=lambda x: x.level_id)
        # 同步更新字典索引（排序后重新构建，确保一致性）
        self._level_dict = {level.level_id: level for level in self.levels}
        self.logger.debug("DGL-MANAGER|关系更新完成")

    def _validate_event(self, event: DynamicGridEvent) -> bool:
        """验证事件有效性"""
        # 启停由上层适配器基于 enabled 控制，Manager 在此仅校验交易对一致性
        if event.pair != self.pair:
            self.logger.warning(
                f"DGL-MANAGER|交易对不匹配|事件交易对={event.pair}|管理器交易对={self.pair}"
            )
            return False
        return True

    def _validate_price_data(self, event: DynamicGridDataUpdateEvent) -> bool:
        """验证价格数据有效性"""
        price = event.data.get("close")
        if not price or price <= 0:
            self.logger.error(f"DGL-MANAGER|价格数据错误|数据={event.data}")
            return False
        return True

    # -------------------------------
    # 全局/层级一致性校验与级联更新
    # -------------------------------

    def _validate_prev_level_consistency(
        self, current_level: DynamicGridLevel, prev_level: DynamicGridLevel
    ) -> list[DynamicGridEvent]:
        """验证与前级的一致性。
        - 前级未持仓 → 当前级只能 WAITING_PREV_OP
        - 前级已持仓 → 当前级若仍在 WAITING_PREV_OP 且已收到价格，则应激活到 WAITING_OP_ACTIVE
        - COMPLETED 作为中间状态，应该已经自动重置，但如果检测到，立即修复
        """
        events: list[DynamicGridEvent] = []
        # 如果当前级是 COMPLETED 状态（异常情况，应该已经自动重置），立即修复
        if current_level.current_state == DynamicGridLevelState.COMPLETED:
            self.logger.warning(
                f"DGL-MANAGER|检测到COMPLETED状态（应已自动重置）|层级={current_level.level_id}|立即修复"
            )
            if not prev_level.is_open:
                # 前级未持仓，重置到 WAITING_PREV_OP
                events.append(current_level.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
            else:
                # 前级已持仓，重置到 WAITING_OP_ACTIVE
                events.append(current_level.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE))
            return events
        
        if not prev_level.is_open:
            if current_level.current_state != DynamicGridLevelState.WAITING_PREV_OP:
                self.logger.debug(
                    f"DGL-MANAGER|前层未开|前层={prev_level.level_id}|cur={current_level.level_id}|cur_state={current_level.current_state.value}"
                )
                events.append(current_level.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
        else:
            # 前级已持仓，当前级在 WAITING_PREV_OP 状态，应激活
            # 注意：不要求 _last_price is not None，因为重置后可能还没有收到新的价格更新
            # 但只要有前级持仓，就可以激活（会在数据更新时检查价格条件）
            if current_level.current_state == DynamicGridLevelState.WAITING_PREV_OP:
                self.logger.info(
                    f"DGL-MANAGER|优化激活|前层={prev_level.level_id}|cur={current_level.level_id}|最后价格={current_level._last_price}"
                )
                events.append(current_level.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE))
        return events

    def _validate_next_level_consistency(
        self, current_level: DynamicGridLevel, next_level: DynamicGridLevel
    ) -> list[DynamicGridEvent]:
        """验证与后级的一致性。
        - 后级已持仓 → 当前级应 WAITING_NEXT_CLOSE（若当前级自身已开仓且在止盈等待中）
        - 后级未持仓 → 当前级从 WAITING_NEXT_CLOSE 恢复到 WAITING_TP_ACTIVE
        """
        events: list[DynamicGridEvent] = []
        if next_level.is_open:
            if (
                current_level.current_state == DynamicGridLevelState.WAITING_TP_ACTIVE
                and current_level.is_open
            ):
                self.logger.info(
                    f"DGL-MANAGER|等待后层关闭|当前={current_level.level_id}|next={next_level.level_id}"
                )
                events.append(current_level.transition_to(DynamicGridLevelState.WAITING_NEXT_CLOSE))
        else:
            if (
                current_level.current_state == DynamicGridLevelState.WAITING_NEXT_CLOSE
                and current_level.is_open
            ):
                if current_level.level_id <= current_level.skip_levels_exit_number:
                    self.logger.debug(
                        f"DGL-MANAGER|跳过恢复止盈激活|层级={current_level.level_id}|limit={current_level.skip_levels_exit_number}"
                    )
                else:
                    self.logger.info(
                        f"DGL-MANAGER|恢复止盈激活|当前={current_level.level_id}|next={next_level.level_id}"
                    )
                    events.append(
                        current_level.transition_to(DynamicGridLevelState.WAITING_TP_ACTIVE)
                    )
        return events

    def _validate_single_level_state(self, level: DynamicGridLevel) -> list[DynamicGridEvent]:
        """验证单个级别的内部状态合理性。"""
        events: list[DynamicGridEvent] = []
        # COMPLETED 作为中间状态，应该已经自动重置，但如果检测到，立即修复
        if level.current_state == DynamicGridLevelState.COMPLETED:
            self.logger.warning(
                f"DGL-MANAGER|检测到COMPLETED状态（应已自动重置）|层级={level.level_id}|立即修复"
            )
            if level.is_first_level():
                # 第一层：重置到 WAITING_OP_ACTIVE
                events.append(level.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE))
            else:
                # 其他层：重置到 WAITING_PREV_OP
                events.append(level.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
            return events
        
        # 已开仓 → 必须具备完整持仓数据
        if level.is_open:
            if not level.op_price or not level.base_amount or not level.quote_amount:
                self.logger.error(f"DGL-MANAGER|层级数据不一致|层级={level.level_id}")
                events.append(level.transition_to(DynamicGridLevelState.ERROR))
        # 回调状态 → 必须具备极值追踪
        if level.current_state in [
            DynamicGridLevelState.WAITING_OP_REBOUND,
            DynamicGridLevelState.WAITING_TP_REBOUND,
        ]:
            if level._max_price is None or level._min_price is None:
                self.logger.warning(f"DGL-MANAGER|层级缺失极值|层级={level.level_id}")
                if level._last_price:
                    level._max_price = level._last_price
                    level._min_price = level._last_price
                    self.logger.info(f"DGL-MANAGER|初始化极值|层级={level.level_id}")
        # WAITING_OP_REBOUND → 必须前级已开仓
        if level.current_state == DynamicGridLevelState.WAITING_OP_REBOUND:
            if not level.prev or not level.prev.op_price:
                self.logger.error(f"DGL-MANAGER|开仓反弹前层未开|层级={level.level_id}")
                events.append(level.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
        # WAITING_TP_REBOUND → 必须自身已开仓
        if level.current_state == DynamicGridLevelState.WAITING_TP_REBOUND:
            if not level.op_price:
                self.logger.error(f"DGL-MANAGER|止盈反弹自身未开|层级={level.level_id}")
                events.append(level.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
        return events

    def _validate_global_state_consistency(self) -> list[DynamicGridEvent]:
        """验证所有级别间的全局状态一致性。"""
        events: list[DynamicGridEvent] = []
        if not self.levels:
            return events
        for i, level in enumerate(self.levels):
            # 与前级一致性
            if i > 0:
                events.extend(self._validate_prev_level_consistency(level, self.levels[i - 1]))
            # 与后级一致性
            if i < len(self.levels) - 1:
                events.extend(self._validate_next_level_consistency(level, self.levels[i + 1]))
            # 单级自检
            events.extend(self._validate_single_level_state(level))
        return events

    def _trigger_cascade_state_updates(self) -> list[DynamicGridEvent]:
        """触发级联状态更新：
        - 从前往后：满足条件则激活下一层
        - 从后往前：若下层未持仓而上层在 WAITING_NEXT_CLOSE，则恢复到 WAITING_TP_ACTIVE
        - COMPLETED 作为中间状态，应该已经自动重置，但如果检测到，立即修复
        """
        events: list[DynamicGridEvent] = []
        # 先处理 COMPLETED 状态的修复（如果存在）
        for level in self.levels:
            if level.current_state == DynamicGridLevelState.COMPLETED:
                self.logger.warning(
                    f"DGL-MANAGER|级联更新检测到COMPLETED状态|层级={level.level_id}|立即修复"
                )
                if level.is_first_level():
                    # 第一层：重置到 WAITING_OP_ACTIVE
                    events.append(level.transition_to(DynamicGridLevelState.WAITING_OP_ACTIVE))
                else:
                    # 其他层：重置到 WAITING_PREV_OP
                    events.append(level.transition_to(DynamicGridLevelState.WAITING_PREV_OP))
        
        # 从前往后：满足条件则激活下一层
        for level in self.levels:
            if level.current_state == DynamicGridLevelState.WAITING_PREV_OP:
                events.extend(level._check_and_activate_if_ready())
        # 从后往前：若下层未持仓而上层在 WAITING_NEXT_CLOSE，则恢复到 WAITING_TP_ACTIVE
        for level in reversed(self.levels):
            if level.next and not level.next.is_open:
                if level.current_state == DynamicGridLevelState.WAITING_NEXT_CLOSE:
                    if level.level_id <= level.skip_levels_exit_number:
                        self.logger.debug(
                            f"级别 {level.level_id}: 位于禁用内部止盈的层({level.skip_levels_exit_number})内，跳过恢复到WAITING_TP_ACTIVE"
                        )
                    else:
                        events.append(level.transition_to(DynamicGridLevelState.WAITING_TP_ACTIVE))
                        self.logger.info(f"级别 {level.level_id}: 后级已平仓，恢复等待止盈状态")
        return events

    def _comprehensive_state_check(self) -> list[DynamicGridEvent]:
        """综合状态检查，整合全局一致性与级联修复。"""
        events: list[DynamicGridEvent] = []
        events.extend(self._validate_global_state_consistency())
        events.extend(self._trigger_cascade_state_updates())
        # 二次验证，收敛状态
        events.extend(self._validate_global_state_consistency())
        return events

    def _handle_data_update_event(self, event: DynamicGridDataUpdateEvent) -> list[DynamicGridEvent]:
        """处理数据更新事件"""
        if not self._validate_price_data(event):
            self.logger.debug(f"DGL-MANAGER|数据验证失败|交易对={self.pair}|价格={event.price}")
            return []

        events = []
        for level in self.levels:
            level_events = level.process_event(event)
            if level_events:
                self.logger.debug(
                    f"DGL-MANAGER|层级事件|交易对={self.pair}|层级={level.level_id}|"
                    f"事件数={len(level_events)}|事件类型={[type(e).__name__ for e in level_events]}"
                )
            events.extend(level_events)
        
        # 全局一致性校验与级联修复
        check_events = self._comprehensive_state_check()
        if check_events:
            self.logger.debug(
                f"DGL-MANAGER|全局校验事件|交易对={self.pair}|事件数={len(check_events)}|"
                f"事件类型={[type(e).__name__ for e in check_events]}"
            )
        events.extend(check_events)
        
        # 统计输出事件类型
        outbound_events = [e for e in events if EventDirection.validate_event_direction(e, EventDirection.OUTBOUND)]
        if outbound_events:
            event_types = {}
            for e in outbound_events:
                event_type = type(e).__name__
                event_types[event_type] = event_types.get(event_type, 0) + 1
            self.logger.info(
                f"DGL-MANAGER|数据更新处理完成|交易对={self.pair}|价格={event.price}|"
                f"输出事件数={len(outbound_events)}|事件类型={event_types}"
            )
        
        return events

    def _handle_entry_signal_event(self, event: DynamicGridEntrySignalEvent) -> list[DynamicGridEvent]:
        """处理开仓信号事件"""
        target_price = event.data.get("target_price", event.data.get("price", "N/A"))
        self.logger.info(
            f"DGL-MANAGER|处理开仓信号|交易对={event.pair}|"
            f"方向={'空头' if event.is_short else '多头'}|"
            f"价格={target_price}|信号={event.data.get('signal_name', 'N/A')}"
        )
        level = self.levels[0] if self.levels else None  # 第一层
        if level:
            self.logger.debug(
                f"DGL-MANAGER|第一层状态|层级={level.level_id}|"
                f"状态={level.current_state.value}|"
                f"skip_first_level_rebound={level.skip_first_level_rebound}"
            )
            level_events = level.process_event(event)
            if level_events:
                self.logger.info(
                    f"DGL-MANAGER|第一层处理信号结果|层级={level.level_id}|"
                    f"输出事件数={len(level_events)}|"
                    f"事件类型={[type(e).__name__ for e in level_events]}"
                )
            else:
                self.logger.warning(
                    f"DGL-MANAGER|第一层处理信号无输出|层级={level.level_id}|"
                    f"状态={level.current_state.value}"
                )
            return level_events
        else:
            self.logger.warning("DGL-MANAGER|无首层")
        return []

    def _handle_exit_signal_event(self, event: DynamicGridExitSignalEvent) -> list[DynamicGridEvent]:
        level = self.levels[-1] if self.levels else None  # 最后一层
        if level:
            return level.process_event(event)
        else:
            self.logger.warning("DGL-MANAGER|无末层")
        return []

    def _handle_add_success_event(self, event: DynamicGridAddSuccessEvent) -> list[DynamicGridEvent]:
        """处理成功事件（开仓/平仓成功）"""
        # 使用字典索引进行 O(1) 查找
        level = self._level_dict.get(event.level_id)
        if not level:
            self.logger.warning(f"DGL-MANAGER|无层级对应加仓成功|层级={event.level_id}")
            return []

        level_events = level.process_event(event)
        self.logger.info("-" * 50)
        self.logger.info(f"DGL-MANAGER|加仓成功|层级={level.level_id}")
        # 全局一致性校验与级联修复
        level_events.extend(self._comprehensive_state_check())
        return level_events

    def _handle_reduce_success_event(
        self, event: DynamicGridReduceSuccessEvent
    ) -> list[DynamicGridEvent]:
        # 使用字典索引进行 O(1) 查找
        level = self._level_dict.get(event.level_id)
        if not level:
            self.logger.warning(f"DGL-MANAGER|无层级对应减仓成功|层级={event.level_id}")
            return []

        level_events = level.process_event(event)
        self.logger.info("-" * 50)
        self.logger.info(f"DGL-MANAGER|减仓成功|层级={level.level_id}")
        # 全局一致性校验与级联修复
        level_events.extend(self._comprehensive_state_check())
        return level_events

    def _handle_global_exit_success_event(
        self, event: DynamicGridGlobalExitSuccessEvent
    ) -> list[DynamicGridEvent]:
        events = []
        if event.is_short == self.is_short and event.pair == self.pair:
            # 重置所有级别
            for level in self.levels:
                level.reset()
            self.logger.info("=" * 60)
            self.logger.info(f"DGL-MANAGER|全局退出成功|原因={event.reason}")
            # 全局重置后立即进行状态一致性检查
            events.extend(self._comprehensive_state_check())
        return events

    def _handle_state_change_event(self, event: DynamicGridStateChangeEvent) -> list[DynamicGridEvent]:
        """消费内部状态变更事件：在 Manager 层进行邻接级别的一致性与级联更新。"""
        self.logger.debug(
            f"DGL-MANAGER|状态变更|层级={event.level_id if hasattr(event, 'level_id') else None}|从={event.from_state if hasattr(event, 'from_state') else None}|到={event.to_state if hasattr(event, 'to_state') else None}"
        )
        # 收到层内状态改变后，立即执行一次全局一致性检查与级联修复
        return self._comprehensive_state_check()

    def check_global_triggers(self) -> list[DynamicGridEvent]:
        """检查全局触发条件"""
        events = []
        current_price = self.current_price
        if not current_price:
            self.logger.warning("DGL-MANAGER|无当前价格")
            return events

        # 全局止损 - 金额模式（使用净盈亏 total_pnl，目标取绝对值比较负方向）
        if self.config.enable_global_sl_amount_check and (
            self.config.global_sl_amount_target is not None
        ):
            total_pnl = self.total_pnl
            sl_threshold = abs(self.config.global_sl_amount_target)
            if total_pnl <= -sl_threshold:
                self.logger.info(
                    f"DGL-MANAGER|全局止损金额触发|总盈亏={total_pnl}|阈值={-sl_threshold}"
                )
                events.append(
                    DynamicGridGlobalExitTriggerEvent.create(
                        is_short=self.is_short,
                        total_base_amount=self.total_base_amount,
                        total_quote_amount=self.total_quote_amount,
                        target_price=current_price,
                        reason="global_stop_loss",
                        pair=self.pair,
                        current_time=self.current_time
                    )
                )
                # 不进行非法状态迁移，由外部处理成交并回传 success 事件

        # 全局止盈 - 金额模式（使用净盈亏 total_pnl，目标取绝对值比较正方向）
        if self.config.enable_global_tp_amount_check and (
            self.config.global_tp_amount_target is not None
        ):
            total_pnl = self.total_pnl
            tp_threshold = abs(self.config.global_tp_amount_target)
            if total_pnl >= tp_threshold:
                self.logger.info(
                    f"DGL-MANAGER|全局止盈金额触发|总盈亏={total_pnl}|阈值={tp_threshold}"
                )
                events.append(
                    DynamicGridGlobalExitTriggerEvent.create(
                        is_short=self.is_short,
                        total_base_amount=self.total_base_amount,
                        total_quote_amount=self.total_quote_amount,
                        target_price=current_price,
                        reason="global_take_profit",
                        pair=self.pair,
                        current_time=self.current_time
                    )
                )
                # 不进行非法状态迁移，由外部处理成交并回传 success 事件

        # 全局止盈 - 价格比例模式（基于均价与当前价的相对收益）
        # 注意：只有在实际盈亏为正时才触发价格比例止盈，避免亏损时误触发
        if self.config.enable_global_tp_price_ratio_check and (
            self.config.global_tp_price_ratio is not None
        ):
            avg_price = self.avg_entry_price
            ratio_threshold = abs(float(self.config.global_tp_price_ratio))
            # 需要有持仓且均价可用
            if avg_price and self.total_base_amount > 0:
                if not self.is_short:
                    price_ratio = (current_price - float(avg_price)) / float(avg_price)
                else:
                    price_ratio = (float(avg_price) - current_price) / float(avg_price)
                # 修复：只有在实际盈亏为正时才触发价格比例止盈
                total_pnl = self.total_pnl
                if price_ratio >= ratio_threshold and total_pnl > 0:
                    self.logger.info(
                        f"DGL-MANAGER|全局止盈比例触发|均价={avg_price}|价格={current_price}|比例={price_ratio}|阈值={ratio_threshold}|总盈亏={total_pnl}"
                    )
                    events.append(
                        DynamicGridGlobalExitTriggerEvent.create(
                            is_short=self.is_short,
                            total_base_amount=self.total_base_amount,
                            total_quote_amount=self.total_quote_amount,
                            target_price=current_price,
                            reason="global_take_profit",
                            pair=self.pair,
                            current_time=self.current_time
                        )
                    )
                    # 不进行非法状态迁移，由外部处理成交并回传 success 事件
                elif price_ratio >= ratio_threshold and total_pnl <= 0:
                    # 价格比例达到但实际亏损，记录警告但不触发止盈
                    self.logger.warning(
                        f"DGL-MANAGER|价格比例达到但实际亏损|均价={avg_price}|价格={current_price}|比例={price_ratio}|阈值={ratio_threshold}|总盈亏={total_pnl}|不触发止盈"
                    )

        return events

    def process_event(self, event: DynamicGridEvent) -> list[DynamicGridEvent]:
        # 使用事件的中国时间
        event_time_str = event.china_time_str if hasattr(event, 'china_time_str') else "未知时间"

        self.logger.debug(
            f"DGL-MANAGER|输入事件|事件={event.event_type}|交易对={event.pair if hasattr(event, 'pair') else self.pair}|数据={event.data}|事件时间={event_time_str}"
        )
        if not self._validate_event(event):
            return []
        if not EventDirection.validate_event_direction(event, EventDirection.INBOUND):
            return []
        events: list[DynamicGridEvent] = []
        # 处理事件
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

        return EventDirection.filter_events(events, [EventDirection.OUTBOUND])

    def to_format_status(self) -> str:
        """格式化状态输出（中文）"""
        direction_str = "short" if self.is_short else "long"
        status = f"网格管理器 (交易对: {self.pair}, 方向: {direction_str})\n"
        status += f"  启用状态: {'是' if self.enabled else '否'}\n"
        status += f"  总持仓基础币: {self.total_base_amount}\n"
        status += f"  总持仓计价币: {self.total_quote_amount}\n"
        status += f"  平均开仓价格: {self.avg_entry_price if self.avg_entry_price else '无'}\n"
        status += f"  未实现损益: {self.unrealized_pnl}\n"
        status += f"  已实现正收益: {self.realized_profit}\n"
        status += f"  已实现负收益: {self.realized_loss}\n"
        status += f"  总损益: {self.total_pnl}\n"
        status += f"  总盈利次数: {self.total_win_count}\n"
        status += f"  总亏损次数: {self.total_loss_count}\n"
        for level in self.levels:
            status += level.to_format_status()
        self.logger.info("DGL-MANAGER|格式化状态")
        return status
