"""
DynamicGrid vnpy 策略实现

基于 vnpy CtaTemplate 的动态网格策略
"""

import json
import math
from typing import Dict, Any, Optional

try:
    from vnpy_ctastrategy import (
        CtaTemplate,
        StopOrder,
        TickData,
        BarData,
        TradeData,
        OrderData,
        BarGenerator,
    )
except ImportError:
    # 如果 vnpy 未安装，提供占位符
    CtaTemplate = object
    StopOrder = None
    TickData = None
    BarData = None
    TradeData = None
    OrderData = None
    BarGenerator = None

# 导入适配器（包内相对导入）
from .adapter import DynamicGridVnpyAdapter


class DynamicGridStrategy(CtaTemplate):
    """DynamicGrid vnpy 策略实现"""
    
    author = "DynamicGrid"
    
    # 策略参数
    # 使用 vn.py 的参数机制，仅暴露一个配置文件路径，具体内容在 JSON 里写
    dynamicgrid_config_path: str = ""  # DynamicGrid 配置文件路径（JSON）
    parameters = ["dynamicgrid_config_path"]
    variables = []
    
    def __init__(
        self,
        cta_engine: Any,
        strategy_name: str,
        vt_symbol: str,
        setting: dict,
    ) -> None:
        """初始化策略"""
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        
        # 初始化适配器（并把 vnpy 的 write_log 和 get_position 注入给适配器）
        self.adapter = DynamicGridVnpyAdapter(strategy_name)
        self.adapter.set_write_log(self.write_log)
        # 注入获取实际持仓量的函数（用于平仓时使用实际持仓量，避免精度问题）
        self.adapter.set_get_position(lambda: self.pos)
        
        # 从配置中初始化网格系统
        # 优先使用 GUI/配置里传入的 dynamicgrid_config_path（类参数），
        # 兼容脚本模式直接传入 setting["dynamicgrid"] / setting["dynamicgrid_config_path"]
        # 1）GUI/实例参数：dynamicgrid_config_path
        config_path = getattr(self, "dynamicgrid_config_path", "") or setting.get(
            "dynamicgrid_config_path"
        )
        dynamicgrid_config = setting.get("dynamicgrid", {})

        if dynamicgrid_config:
            # 直接通过字典初始化（脚本模式）
            self.adapter.initialize_from_config(dynamicgrid_config)
            self.write_log("DynamicGrid 配置已从 setting['dynamicgrid'] 加载")
            # 打印配置内容
            self._print_config(dynamicgrid_config)
        elif config_path:
            # 从 JSON 文件加载（GUI 推荐方式）
            self.adapter.initialize_from_file(config_path)
            self.write_log(f"DynamicGrid 配置已从文件加载: {config_path}")
            # 读取并打印配置内容
            from pathlib import Path
            config_file = Path(config_path)
            if not config_file.is_absolute():
                config_file = (Path(__file__).parent.parent.parent.parent / config_file).resolve()
            if config_file.exists():
                with config_file.open("r", encoding="utf-8") as f:
                    file_data = json.load(f)
                # 兼容两种格式
                if "pairs" in file_data:
                    config_to_print = file_data
                elif "dynamicgrid" in file_data and isinstance(file_data["dynamicgrid"], dict):
                    config_to_print = file_data["dynamicgrid"]
                else:
                    config_to_print = file_data
                self._print_config(config_to_print)
        else:
            self.write_log("DynamicGrid 未提供配置（dynamicgrid/dynamicgrid_config_path），网格将不会启用")
        
        # K线生成器
        self.bg: Optional[BarGenerator] = None
        
        # 订单标签映射（用于跟踪订单）
        self._order_tags: Dict[str, str] = {}
    
    def _print_config(self, config: Dict[str, Any]) -> None:
        """打印配置内容（格式化输出）"""
        self.write_log("=" * 60)
        self.write_log("DynamicGrid 配置详情:")
        self.write_log("=" * 60)
        
        # 打印 signals 配置
        signals = config.get("signals", {})
        if signals:
            self.write_log("信号配置 (signals):")
            for signal_name, signal_config in signals.items():
                self.write_log(f"  {signal_name}: {json.dumps(signal_config, ensure_ascii=False, indent=4)}")
        
        # 打印 pairs 配置（每个交易对的详细信息）
        pairs = config.get("pairs", [])
        self.write_log(f"\n交易对配置 (pairs): 共 {len(pairs)} 个")
        for idx, pair_config in enumerate(pairs, 1):
            pair_name = pair_config.get("pair", "未知")
            is_short = pair_config.get("is_short", False)
            enabled = pair_config.get("enabled", True)
            n_levels = pair_config.get("n_levels", 0)
            self.write_log(f"\n  [{idx}] {pair_name} ({'空头' if is_short else '多头'}, {'启用' if enabled else '禁用'})")
            
            # 打印层级配置（从配置中读取，因为 levels 是动态生成的）
            self.write_log(f"      层级数 (n_levels): {n_levels}")
            
            # 通过适配器获取层级配置信息（不直接访问核心层）
            try:
                level_configs = self.adapter.get_level_configs_for_display(pair_config)
                
                # 打印前5层的详细信息
                for level_config in level_configs[:5]:
                    self.write_log(
                        f"        层级 {level_config['level_id']}: "
                        f"amount={level_config['amount']}, "
                        f"op_ratio={level_config['op_ratio']}, "
                        f"tp_ratio={level_config['tp_ratio']}"
                    )
                if len(level_configs) > 5:
                    self.write_log(f"        ... (还有 {len(level_configs) - 5} 个层级未显示)")
            except Exception as e:
                # 如果解析失败，至少打印原始配置的关键字段
                amount_list = pair_config.get("amount", [])
                self.write_log(f"      配置解析失败: {e}")
                self.write_log(f"      amount 数组长度: {len(amount_list)}")
                if amount_list:
                    self.write_log(f"      前5个 amount 值: {amount_list[:5]}")
        
        self.write_log("=" * 60)
    
    def on_init(self) -> None:
        """策略初始化回调"""
        self.write_log("DynamicGrid 策略初始化")
        
        # 创建K线生成器
        self.bg = BarGenerator(self.on_bar)
        
        # 加载历史数据
        # 注意：需要加载足够的历史数据以确保指标（如 RSI）能够初始化
        # ArrayManager 需要至少 size 根 K 线才能初始化（默认 size=200）
        # 同时考虑 RSI 需要至少 14 根 K 线，所以至少加载 20 根
        self.load_bar(20)
    
    def on_start(self) -> None:
        """策略启动回调"""
        self.write_log("DynamicGrid 策略启动")
        self.put_event()
    
    def on_stop(self) -> None:
        """策略停止回调"""
        self.write_log("DynamicGrid 策略停止")
        self.put_event()
    
    def on_tick(self, tick: TickData) -> None:
        """Tick数据更新回调"""
        if self.bg:
            self.bg.update_tick(tick)
    
    def on_bar(self, bar: BarData) -> None:
        """K线数据更新回调
        
        工作流程：
        1. 如果没有持仓，检查开仓信号，如果满足条件则开仓（第一层）
        2. 如果有持仓，调用 handle_data_update，由网格框架计算是否加仓
        """
        # 安全检查：确保 K 线数据有效
        if bar.close_price is None or math.isnan(bar.close_price) or math.isinf(bar.close_price) or bar.close_price <= 0:
            self.write_log(
                f"DGL-STRATEGY|警告：K线收盘价无效，跳过此K线|"
                f"close_price={bar.close_price}|datetime={bar.datetime}"
            )
            return
        
        self.write_log(
            f"收到K线|时间={bar.datetime}|"
            f"O={bar.open_price}|H={bar.high_price}|L={bar.low_price}|C={bar.close_price}|V={bar.volume}"
        )
        
        # 取消所有未成交订单
        self.cancel_all()
        
        # 检查是否有持仓
        has_position = self.pos != 0
        
        if not has_position:
            # 没有持仓：检查开仓信号（第一层开仓）
            self.write_log("DGL-STRATEGY|无持仓，检查开仓信号")
            signal_event = self.adapter.check_entry_signal(bar, self.vt_symbol)
            
            if signal_event:
                # 信号满足，生成开仓触发事件
                pair = self.adapter._normalize_pair_key(self.vt_symbol)
                manager = self.adapter.grid_managers.get(pair)
                if manager:
                    # 将信号事件发送给网格管理器，生成开仓触发事件
                    trigger_events = manager.process_event(signal_event)
                    
                    if trigger_events:
                        # 只处理第一个触发事件（通常是第一层的开仓触发）
                        trigger_event = trigger_events[0]
                        # 通过适配器检查事件类型（不直接访问核心层）
                        if self.adapter.is_entry_or_add_event(trigger_event):
                            # 映射为订单操作
                            orders = self.adapter._map_events_to_orders(pair, [trigger_event], manager.is_short)
                            
                            # 执行开仓订单
                            for order_info in orders:
                                if len(order_info) == 4:
                                    action, price, volume, tag = order_info
                                elif len(order_info) == 3:
                                    action, price, tag = order_info
                                    volume = self._calculate_volume(price, tag)
                                else:
                                    self.write_log(f"订单格式错误: {order_info}")
                                    continue
                                
                                # 使用限价单（价格=收盘价）：使用当前 K 线收盘价作为限价，通常能立即成交
                                market_price = bar.close_price
                                
                                # 安全检查：确保 volume 和 price 有效
                                # 检查 NaN 和 Inf
                                if math.isnan(volume) or math.isinf(volume):
                                    self.write_log(
                                        f"DGL-STRATEGY|警告：开仓订单volume为NaN/Inf，跳过|"
                                        f"action={action}|volume={volume}|price={price}|tag={tag}"
                                    )
                                    continue
                                if math.isnan(market_price) or math.isinf(market_price):
                                    self.write_log(
                                        f"DGL-STRATEGY|警告：开仓订单价格为NaN/Inf，跳过|"
                                        f"action={action}|market_price={market_price}|price={price}|tag={tag}"
                                    )
                                    continue
                                
                                # 检查合理的数值范围（防止异常大或异常小的值）
                                # volume 应该在合理范围内（比如 0.0001 到 1000000）
                                if volume <= 0 or volume < 0.0001 or volume > 1000000:
                                    self.write_log(
                                        f"DGL-STRATEGY|警告：开仓订单volume超出合理范围，跳过|"
                                        f"action={action}|volume={volume}|price={price}|tag={tag}|范围=[0.0001, 1000000]"
                                    )
                                    continue
                                
                                # price 应该在合理范围内（比如 0.01 到 100000000）
                                if market_price <= 0 or market_price < 0.01 or market_price > 100000000:
                                    self.write_log(
                                        f"DGL-STRATEGY|警告：开仓订单价格超出合理范围，跳过|"
                                        f"action={action}|market_price={market_price}|price={price}|tag={tag}|范围=[0.01, 100000000]"
                                    )
                                    continue
                                
                                self.write_log(f"准备开仓(限价单，价格=收盘价)|action={action}|限价={market_price}|参考价格={price}|volume={volume}|tag={tag}")
                                
                                order_ids = []
                                if action == "buy":
                                    order_ids = self.buy(market_price, volume, lock=False)
                                    self._register_order_tags(order_ids, tag)
                                    self.write_log(f"买入限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                                elif action == "sell":
                                    order_ids = self.sell(market_price, volume, lock=False)
                                    self._register_order_tags(order_ids, tag)
                                    self.write_log(f"卖出限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                                elif action == "short":
                                    order_ids = self.short(market_price, volume, lock=False)
                                    self._register_order_tags(order_ids, tag)
                                    self.write_log(f"做空限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                                elif action == "cover":
                                    order_ids = self.cover(market_price, volume, lock=False)
                                    self._register_order_tags(order_ids, tag)
                                    self.write_log(f"平仓限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                                
                                # 如果订单提交失败（返回空列表），清除层级挂单标记，允许重新触发
                                if not order_ids:
                                    self.write_log(
                                        f"DGL-STRATEGY|警告：订单提交失败（返回空列表），清除挂单标记|"
                                        f"action={action}|tag={tag}"
                                    )
                                    # 从标签中解析层级ID并清除挂单标记
                                    from ...utils.tag_utils import DynamicGridTagUtils
                                    _, level_id, _ = DynamicGridTagUtils.parse_tag(tag)
                                    if level_id and manager and 1 <= level_id <= len(manager.levels):
                                        level = manager.levels[level_id - 1]
                                        if hasattr(level, '_pending_action'):
                                            old_pending = level._pending_action
                                            level._pending_action = None
                                            level._pending_since = None
                                            self.write_log(
                                                f"DGL-STRATEGY|已清除挂单标记|层级={level_id}|"
                                                f"原pending_action={old_pending}"
                                            )
                else:
                    self.write_log(f"DGL-STRATEGY|未找到网格管理器|pair={pair}")
            else:
                self.write_log("DGL-STRATEGY|信号未满足，不开仓")
        else:
            # 有持仓：只处理数据更新，由网格框架计算是否加仓
            self.write_log(f"DGL-STRATEGY|有持仓(pos={self.pos})，处理数据更新，由网格框架决定是否加仓")
            orders = self.adapter.handle_data_update(bar, self.vt_symbol)
            
            self.write_log(f"DGL-STRATEGY|handle_data_update返回订单数={len(orders)}")
            
            # 执行订单操作（加仓/减仓）
            for order_info in orders:
                if len(order_info) == 4:
                    action, price, volume, tag = order_info
                elif len(order_info) == 3:
                    action, price, tag = order_info
                    volume = self._calculate_volume(price, tag)
                else:
                    self.write_log(f"订单格式错误: {order_info}")
                    continue
                
                # 使用限价单（价格=收盘价）：使用当前 K 线收盘价作为限价，通常能立即成交
                market_price = bar.close_price
                
                # 安全检查：确保 volume 和 price 有效
                # 检查 NaN 和 Inf
                if math.isnan(volume) or math.isinf(volume):
                    self.write_log(
                        f"DGL-STRATEGY|警告：订单volume为NaN/Inf，跳过|"
                        f"action={action}|volume={volume}|price={price}|tag={tag}"
                    )
                    continue
                if math.isnan(market_price) or math.isinf(market_price):
                    self.write_log(
                        f"DGL-STRATEGY|警告：订单价格为NaN/Inf，跳过|"
                        f"action={action}|market_price={market_price}|price={price}|tag={tag}"
                    )
                    continue
                
                # 检查合理的数值范围（防止异常大或异常小的值）
                # volume 应该在合理范围内（比如 0.0001 到 1000000）
                if volume <= 0 or volume < 0.0001 or volume > 1000000:
                    self.write_log(
                        f"DGL-STRATEGY|警告：订单volume超出合理范围，跳过|"
                        f"action={action}|volume={volume}|price={price}|tag={tag}|范围=[0.0001, 1000000]"
                    )
                    continue
                
                # price 应该在合理范围内（比如 0.01 到 100000000）
                if market_price <= 0 or market_price < 0.01 or market_price > 100000000:
                    self.write_log(
                        f"DGL-STRATEGY|警告：订单价格超出合理范围，跳过|"
                        f"action={action}|market_price={market_price}|price={price}|tag={tag}|范围=[0.01, 100000000]"
                    )
                    continue
                
                self.write_log(f"准备下单(限价单，价格=收盘价)|action={action}|限价={market_price}|参考价格={price}|volume={volume}|tag={tag}")
                
                # 注意：price 参数仍然保留用于计算数量，但实际下单使用当前收盘价（bar.close_price）作为限价
                if action == "buy":
                    order_ids = self.buy(market_price, volume, lock=False)
                    self._register_order_tags(order_ids, tag)
                    self.write_log(f"买入限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                elif action == "sell":
                    order_ids = self.sell(market_price, volume, lock=False)
                    self._register_order_tags(order_ids, tag)
                    self.write_log(f"卖出限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                elif action == "short":
                    order_ids = self.short(market_price, volume, lock=False)
                    self._register_order_tags(order_ids, tag)
                    self.write_log(f"做空限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
                elif action == "cover":
                    order_ids = self.cover(market_price, volume, lock=False)
                    self._register_order_tags(order_ids, tag)
                    self.write_log(f"平仓限价单已提交|order_ids={order_ids}|限价={market_price}|参考价格={price}")
        
        self.put_event()
    
    def on_trade(self, trade: TradeData) -> None:
        """成交数据更新回调"""
        # 获取订单标签
        # vnpy 回测引擎可能返回的订单ID格式不同，需要尝试多种格式
        order_tag = self._order_tags.get(trade.orderid)
        
        # 如果直接查找失败，尝试添加 BACKTESTING. 前缀
        if order_tag is None and not trade.orderid.startswith("BACKTESTING."):
            order_tag = self._order_tags.get(f"BACKTESTING.{trade.orderid}")
            if order_tag:
                self.write_log(
                    f"DGL-STRATEGY|on_trade|订单ID格式转换|"
                    f"原始ID={trade.orderid}|转换后ID=BACKTESTING.{trade.orderid}|找到标签={order_tag}"
                )
        
        # 如果还是找不到，尝试去掉 BACKTESTING. 前缀
        if order_tag is None and trade.orderid.startswith("BACKTESTING."):
            order_id_without_prefix = trade.orderid.replace("BACKTESTING.", "")
            order_tag = self._order_tags.get(order_id_without_prefix)
            if order_tag:
                self.write_log(
                    f"DGL-STRATEGY|on_trade|订单ID格式转换|"
                    f"原始ID={trade.orderid}|转换后ID={order_id_without_prefix}|找到标签={order_tag}"
                )
        
        # 存储标签到 TradeData.extra（最小入侵方案：将标签持久化到成交记录中）
        if order_tag:
            if not trade.extra:
                trade.extra = {}
            trade.extra['tag'] = order_tag
        
        self.write_log(
            f"DGL-STRATEGY|on_trade|订单ID={trade.orderid}|"
            f"价格={trade.price}|数量={trade.volume}|标签={order_tag}"
        )
        
        # 处理成交
        self.adapter.handle_trade(trade, self.vt_symbol, order_tag)
        
        self.put_event()
    
    def on_order(self, order: OrderData) -> None:
        """订单数据更新回调"""
        # 处理订单状态变化，特别是订单撤销的情况
        # 注意：vnpy 的订单状态可能是字符串或枚举，需要兼容处理
        
        # 获取订单标签
        order_tag = self._order_tags.get(order.orderid)
        if not order_tag:
            # 尝试其他格式
            if not order.orderid.startswith("BACKTESTING."):
                order_tag = self._order_tags.get(f"BACKTESTING.{order.orderid}")
            elif order.orderid.startswith("BACKTESTING."):
                order_tag = self._order_tags.get(order.orderid.replace("BACKTESTING.", ""))
        
        # 如果订单被撤销或拒绝，需要通知适配器清除挂单标记
        # 兼容字符串和枚举两种状态格式
        order_status = str(order.status).upper() if hasattr(order, 'status') else ""
        is_cancelled_or_rejected = (
            order_status in ("CANCELLED", "CANCELED", "REJECTED") or
            "CANCEL" in order_status or
            "REJECT" in order_status
        )
        
        if is_cancelled_or_rejected:
            self.write_log(
                f"DGL-STRATEGY|订单被撤销/拒绝|订单ID={order.orderid}|"
                f"状态={order.status}|标签={order_tag}"
            )
            
            # 通知适配器处理订单撤销
            if order_tag:
                from ...utils.tag_utils import DynamicGridTagUtils
                tag_type, level_id, reason = DynamicGridTagUtils.parse_tag(order_tag)
                self.write_log(
                    f"DGL-STRATEGY|订单撤销处理|标签={order_tag}|"
                    f"解析结果: type={tag_type}|level_id={level_id}|reason={reason}"
                )
                if level_id:
                    # 通知管理器清除该层级的挂单标记
                    # 使用规范化后的pair名称，与adapter中的命名保持一致
                    pair = self.adapter._normalize_pair_key(self.vt_symbol)
                    manager = self.adapter.grid_managers.get(pair)
                    self.write_log(
                        f"DGL-STRATEGY|订单撤销处理|查找管理器|pair={pair}|"
                        f"manager存在={manager is not None}"
                    )
                    if manager:
                        self.write_log(
                            f"DGL-STRATEGY|订单撤销处理|管理器层级数={len(manager.levels)}|"
                            f"目标level_id={level_id}|有效范围={1 <= level_id <= len(manager.levels)}"
                        )
                    if manager and 1 <= level_id <= len(manager.levels):
                        level = manager.levels[level_id - 1]
                        self.write_log(
                            f"DGL-STRATEGY|订单撤销处理|找到层级|level_id={level_id}|"
                            f"当前状态={level.current_state}|"
                            f"当前pending_action={getattr(level, '_pending_action', None)}"
                        )
                        # 清除挂单标记，允许重新触发
                        if hasattr(level, '_pending_action'):
                            old_pending = level._pending_action
                            level._pending_action = None
                            level._pending_since = None
                            self.write_log(
                                f"DGL-STRATEGY|已清除挂单标记|层级={level_id}|"
                                f"订单ID={order.orderid}|原pending_action={old_pending}"
                            )
                        else:
                            self.write_log(
                                f"DGL-STRATEGY|警告：层级无_pending_action属性|层级={level_id}"
                            )
                    else:
                        self.write_log(
                            f"DGL-STRATEGY|警告：无法找到层级|pair={pair}|"
                            f"manager存在={manager is not None}|"
                            f"level_id={level_id}|层级数={len(manager.levels) if manager else 0}"
                        )
                else:
                    self.write_log(
                        f"DGL-STRATEGY|警告：无法解析level_id|标签={order_tag}"
                    )
            else:
                self.write_log(
                    f"DGL-STRATEGY|警告：订单无标签|订单ID={order.orderid}"
                )
    
    def on_stop_order(self, stop_order: StopOrder) -> None:
        """停止单更新回调"""
        pass
    
    def _calculate_volume(self, price: float, tag: Optional[str]) -> float:
        """计算订单数量
        
        Args:
            price: 目标价格
            tag: 订单标签
        
        Returns:
            订单数量（基础币数量）
        """
        from ...utils.tag_utils import DynamicGridTagUtils
        
        # 从标签中解析层级ID
        if tag:
            _, level_id, _ = DynamicGridTagUtils.parse_tag(tag)
            if level_id:
                # 从管理器获取层级配置
                # 使用规范化后的pair名称，与adapter中的命名保持一致
                pair = self.adapter._normalize_pair_key(self.vt_symbol)
                manager = self.adapter.grid_managers.get(pair)
                if manager and 1 <= level_id <= len(manager.levels):
                    level = manager.levels[level_id - 1]
                    # 根据配置计算数量
                    quote_amount = level.config.amount
                    # 安全检查：确保价格有效
                    if price > 0 and not (math.isnan(price) or math.isinf(price)):
                        base_amount = quote_amount / price
                        # 安全检查：确保计算结果有效
                        if math.isnan(base_amount) or math.isinf(base_amount) or base_amount <= 0:
                            self.write_log(
                                f"DGL-STRATEGY|警告：_calculate_volume计算结果无效|"
                                f"level_id={level_id}|quote_amount={quote_amount}|price={price}|base_amount={base_amount}"
                            )
                            return 0.01  # 返回默认值
                        return base_amount
                    else:
                        self.write_log(
                            f"DGL-STRATEGY|警告：_calculate_volume价格无效|"
                            f"level_id={level_id}|price={price}"
                        )
                        return 0.01  # 返回默认值
        
        # 默认数量
        return 0.01
    
    def _register_order_tags(self, order_ids: list, tag: Optional[str]) -> None:
        """注册订单标签
        
        Args:
            order_ids: 订单ID列表
            tag: 订单标签
        """
        if tag:
            for order_id in order_ids:
                self._order_tags[order_id] = tag
                # 解析层级ID并注册到适配器
                from ...utils.tag_utils import DynamicGridTagUtils
                _, level_id, reason = DynamicGridTagUtils.parse_tag(tag)
                self.write_log(
                    f"DGL-STRATEGY|注册订单标签|订单ID={order_id}|标签={tag}|"
                    f"解析level_id={level_id}|reason={reason}"
                )
                if level_id:
                    self.adapter.register_order(order_id, level_id, reason or "")
                    self.write_log(
                        f"DGL-STRATEGY|订单已注册到适配器|订单ID={order_id}|level_id={level_id}"
                    )
                else:
                    self.write_log(
                        f"DGL-STRATEGY|警告：订单标签无法解析level_id|订单ID={order_id}|标签={tag}"
                    )

