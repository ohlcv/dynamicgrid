"""
DynamicGrid 订单标签工具类

提供统一的标签解析、构建和判断方法，避免代码重复。

标签格式设计（统一格式：动作:层级:原因）：
- entry:1:{reason} - 开仓（第一层），reason可以是信号摘要如"CCI_1m_RSI_5m"
- exit:1:{reason} - 平第一层，reason如"take_profit"
- exit:all:{reason} - 平全部，reason如"global_take_profit"
- add:{level_id}:{reason} - 加仓（level_id >= 2），reason可以是触发条件（可选）
- reduce:{level_id}:{reason} - 减仓（level_id >= 2），reason如"take_profit"

说明：
- "reason"是动作的原因，对于entry可以是信号摘要，对于exit/reduce是退出原因
- freqtrade官方的标签直接就是reason，我们在此基础上增加了动作和层级信息
"""

from typing import Optional, Tuple


class DynamicGridTagUtils:
    """DynamicGrid 订单标签工具类"""
    
    # 标签格式常量
    ENTRY_TAG_PREFIX = "entry:"
    EXIT_TAG_PREFIX = "exit:"
    ADD_TAG_PREFIX = "add:"
    REDUCE_TAG_PREFIX = "reduce:"
    
    # Freqtrade 系统标签（直接就是reason，没有动作和层级信息）
    # 这些标签通常表示全局退出
    FREQTRADE_SYSTEM_TAGS = {
        # 止盈类
        "roi",
        "take_profit",
        # 止损类
        "trailing_stop_loss",
        "stop_loss",
        "stoploss_on_exchange",
        "liquidation",
        # 其他退出标签
        "exit_signal",
        "force_exit",
        "emergency_exit",
        "sold_on_exchange",
        "custom_exit",
        "partial_exit",
    }
    
    # 全局退出标签（通常是全平）
    GLOBAL_EXIT_TAGS = {
        "roi",
        "trailing_stop_loss",
        "stop_loss",
        "stoploss_on_exchange",
        "liquidation",
        "exit_signal",
        "force_exit",
        "emergency_exit",
        "sold_on_exchange",
        "custom_exit",
    }
    
    # 部分退出标签（可能是部分平仓）
    PARTIAL_EXIT_TAGS = {
        "partial_exit",
    }
    
    @staticmethod
    def parse_tag(tag: Optional[str]) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """解析订单标签，返回 (类型, 层级ID, reason)
        
        Args:
            tag: 订单标签字符串，统一格式为：动作:层级:原因
                - entry:1:{reason} - 开仓
                - exit:1:{reason} - 平第一层
                - exit:all:{reason} - 平全部
                - add:{level_id}:{reason} - 加仓（level_id >= 2）
                - reduce:{level_id}:{reason} - 减仓（level_id >= 2）
            
            注意：reason 不应包含冒号（:），否则解析可能不准确。
        
        Returns:
            (类型, 层级ID, reason) 元组
            - 类型: 'entry', 'exit', 'add', 'reduce' 或 None
            - 层级ID: 整数或 None（对于 exit:all 返回 None）
            - reason: 字符串（原因）或 None
        """
        if not tag:
            return None, None, None
        
        tag = tag.strip()
        
        # 解析 entry 标签: entry:1:{reason}
        if tag.startswith(DynamicGridTagUtils.ENTRY_TAG_PREFIX):
            try:
                parts = tag.split(":")
                if len(parts) >= 2 and parts[1] == "1":
                    reason = ":".join(parts[2:]) if len(parts) > 2 else None
                    return "entry", 1, reason
            except (ValueError, IndexError):
                return "entry", None, None
        
        # 解析 exit 标签: exit:1:{reason} 或 exit:all:{reason}
        elif tag.startswith(DynamicGridTagUtils.EXIT_TAG_PREFIX):
            try:
                parts = tag.split(":")
                if len(parts) >= 2:
                    if parts[1] == "1":
                        # exit:1:{reason}
                        reason = ":".join(parts[2:]) if len(parts) > 2 else None
                        return "exit", 1, reason
                    elif parts[1] == "all":
                        # exit:all:{reason}
                        reason = ":".join(parts[2:]) if len(parts) > 2 else None
                        return "exit", None, reason  # level_id 为 None 表示全部
            except (ValueError, IndexError):
                return "exit", None, None
        
        # 解析 add 标签: add:{level_id}:{reason}
        elif tag.startswith(DynamicGridTagUtils.ADD_TAG_PREFIX):
            try:
                parts = tag.split(":")
                if len(parts) >= 2:
                    level_id_str = parts[1]
                    if level_id_str and level_id_str.isdigit():
                        level_id = int(level_id_str)
                        reason = ":".join(parts[2:]) if len(parts) > 2 else None
                        return "add", level_id, reason
            except (ValueError, IndexError):
                return "add", None, None
        
        # 解析 reduce 标签: reduce:{level_id}:{reason}
        elif tag.startswith(DynamicGridTagUtils.REDUCE_TAG_PREFIX):
            try:
                parts = tag.split(":")
                if len(parts) >= 2:
                    level_id_str = parts[1]
                    if level_id_str and level_id_str.isdigit():
                        level_id = int(level_id_str)
                        reason = ":".join(parts[2:]) if len(parts) > 2 else None
                        return "reduce", level_id, reason
            except (ValueError, IndexError):
                return "reduce", None, None
        
        # 检查是否是 Freqtrade 系统标签（直接就是reason，没有动作和层级信息）
        # 这些标签通常表示全局退出，但也可以是部分退出
        tag_lower = tag.lower()
        if tag_lower in DynamicGridTagUtils.FREQTRADE_SYSTEM_TAGS:
            # Freqtrade系统标签直接就是reason
            reason = tag_lower
            # 判断是全局退出还是部分退出
            if tag_lower in DynamicGridTagUtils.GLOBAL_EXIT_TAGS:
                # 全局退出，level_id为None表示全部
                return "exit", None, reason
            elif tag_lower in DynamicGridTagUtils.PARTIAL_EXIT_TAGS:
                # 部分退出，可能是单层退出，但无法确定层级，返回None
                # 实际使用时需要根据订单成交数量判断
                return "exit", None, reason
        
        return None, None, None
    
    @staticmethod
    def extract_level_id(tag: Optional[str]) -> Optional[int]:
        """快速提取层级ID（不解析完整标签）
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            层级ID（整数）或 None（对于 exit:all 返回 None）
        """
        tag_type, level_id, _ = DynamicGridTagUtils.parse_tag(tag)
        return level_id
    
    @staticmethod
    def is_entry_tag(tag: Optional[str]) -> bool:
        """判断标签是否为开仓标签
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            True 如果是开仓标签，否则 False
        """
        if not tag:
            return False
        return tag.strip().startswith(DynamicGridTagUtils.ENTRY_TAG_PREFIX)
    
    @staticmethod
    def is_exit_tag(tag: Optional[str]) -> bool:
        """判断标签是否为平仓标签
        
        包括：
        - exit:1:{reason} 或 exit:all:{reason}（自定义格式）
        - Freqtrade系统标签（如"roi", "stop_loss"等，直接就是reason）
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            True 如果是平仓标签，否则 False
        """
        if not tag:
            return False
        tag = tag.strip()
        # 检查自定义exit标签
        if tag.startswith(DynamicGridTagUtils.EXIT_TAG_PREFIX):
            return True
        # 检查Freqtrade系统标签
        tag_lower = tag.lower()
        return tag_lower in DynamicGridTagUtils.FREQTRADE_SYSTEM_TAGS
    
    @staticmethod
    def is_global_exit_tag(tag: Optional[str]) -> bool:
        """判断标签是否为全局平仓标签
        
        包括：
        - exit:all:{reason}（自定义格式）
        - Freqtrade全局退出标签（如"roi", "stop_loss"等）
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            True 如果是全局平仓标签，否则 False
        """
        if not tag:
            return False
        tag = tag.strip()
        # 检查自定义exit:all标签
        if tag.startswith(DynamicGridTagUtils.EXIT_TAG_PREFIX) and ":all:" in tag:
            return True
        # 检查Freqtrade全局退出标签
        tag_lower = tag.lower()
        return tag_lower in DynamicGridTagUtils.GLOBAL_EXIT_TAGS
    
    @staticmethod
    def is_freqtrade_system_tag(tag: Optional[str]) -> bool:
        """判断标签是否为Freqtrade系统标签
        
        Freqtrade系统标签直接就是reason，没有动作和层级信息。
        例如："roi", "stop_loss", "trailing_stop_loss"等。
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            True 如果是Freqtrade系统标签，否则 False
        """
        if not tag:
            return False
        tag_lower = tag.strip().lower()
        return tag_lower in DynamicGridTagUtils.FREQTRADE_SYSTEM_TAGS
    
    @staticmethod
    def get_reason_from_tag(tag: Optional[str]) -> Optional[str]:
        """从标签中提取reason
        
        支持两种格式：
        1. 自定义格式：动作:层级:原因，返回原因部分
        2. Freqtrade系统标签：直接就是reason，返回整个标签
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            reason字符串或None
        """
        if not tag:
            return None
        
        tag = tag.strip()
        
        # 如果是Freqtrade系统标签，直接返回
        if DynamicGridTagUtils.is_freqtrade_system_tag(tag):
            return tag.lower()
        
        # 否则解析自定义格式
        _, _, reason = DynamicGridTagUtils.parse_tag(tag)
        return reason
    
    @staticmethod
    def is_add_tag(tag: Optional[str]) -> bool:
        """判断标签是否为加仓标签（level_id >= 2）
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            True 如果是加仓标签，否则 False
        """
        if not tag:
            return False
        return tag.strip().startswith(DynamicGridTagUtils.ADD_TAG_PREFIX)
    
    @staticmethod
    def is_reduce_tag(tag: Optional[str]) -> bool:
        """判断标签是否为减仓标签（level_id >= 2）
        
        Args:
            tag: 订单标签字符串
        
        Returns:
            True 如果是减仓标签，否则 False
        """
        if not tag:
            return False
        return tag.strip().startswith(DynamicGridTagUtils.REDUCE_TAG_PREFIX)
    
    @staticmethod
    def build_entry_tag(reason: Optional[str] = None) -> str:
        """构建开仓标签（第一层）
        
        Args:
            reason: 可选的reason（可以是信号摘要，如"CCI_1m_RSI_5m"）
        
        Returns:
            标签字符串，格式: entry:1:{reason} 或 entry:1
        """
        if reason:
            return f"entry:1:{reason}"
        return "entry:1"
    
    @staticmethod
    def build_exit_tag(level_id: Optional[int] = None, reason: Optional[str] = None) -> str:
        """构建平仓标签
        
        Args:
            level_id: 层级ID，1 表示平第一层，None 表示平全部
            reason: 平仓原因（如"take_profit", "global_take_profit"）
        
        Returns:
            标签字符串，格式: 
            - level_id=1: exit:1:{reason} 或 exit:1
            - level_id=None: exit:all:{reason} 或 exit:all
        """
        if level_id is None:
            if reason:
                return f"exit:all:{reason}"
            return "exit:all"
        else:
            if reason:
                return f"exit:{level_id}:{reason}"
            return f"exit:{level_id}"
    
    @staticmethod
    def build_add_tag(level_id: int, reason: Optional[str] = None) -> str:
        """构建加仓标签（level_id >= 2）
        
        Args:
            level_id: 层级ID（必须 >= 2）
            reason: 可选的reason（可以是触发条件，如"price_drop", "signal_trigger"）
        
        Returns:
            标签字符串，格式: add:{level_id}:{reason} 或 add:{level_id}
        """
        if level_id < 2:
            raise ValueError(f"加仓标签的层级ID必须 >= 2，当前值: {level_id}")
        if reason:
            return f"add:{level_id}:{reason}"
        return f"add:{level_id}"
    
    @staticmethod
    def build_reduce_tag(level_id: int, reason: Optional[str] = None) -> str:
        """构建减仓标签（level_id >= 2）
        
        Args:
            level_id: 层级ID（必须 >= 2）
            reason: 减仓原因（如"take_profit", "stop_loss"）
        
        Returns:
            标签字符串，格式: reduce:{level_id}:{reason} 或 reduce:{level_id}
        """
        if level_id < 2:
            raise ValueError(f"减仓标签的层级ID必须 >= 2，当前值: {level_id}")
        if reason:
            return f"reduce:{level_id}:{reason}"
        return f"reduce:{level_id}"
    
    @staticmethod
    def get_add_tag_prefix(level_id: int) -> str:
        """获取加仓标签前缀（用于匹配同一层级的所有加仓订单）
        
        Args:
            level_id: 层级ID
        
        Returns:
            标签前缀，格式: add:{level_id}:
        """
        return f"add:{level_id}:"
    
    @staticmethod
    def get_reduce_tag_prefix(level_id: int) -> str:
        """获取减仓标签前缀（用于匹配同一层级的所有减仓订单）
        
        Args:
            level_id: 层级ID
        
        Returns:
            标签前缀，格式: reduce:{level_id}:
        """
        return f"reduce:{level_id}:"

