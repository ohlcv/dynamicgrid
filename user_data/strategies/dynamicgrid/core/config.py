from typing import List, Optional
from pydantic import BaseModel, field_validator, model_validator


class DynamicGridLevelConfig(BaseModel):
    pair: str
    is_short: bool
    level_id: int
    amount: float
    op_ratio: float
    tp_ratio: float
    op_rebound_ratio: float
    tp_rebound_ratio: float
    # is_base_mode: True 表示币本位（amount 是 base 货币数量），False 表示 U 本位（amount 是 quote 货币金额）
    is_base_mode: bool = False

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError("amount 必须为正数")
        return v

    @model_validator(mode="after")
    def log_validation(self):
        return self


class DynamicGridConfig(BaseModel):
    pair: str
    is_short: bool
    enabled: bool = True
    n_levels: int
    amount: List[float]
    op_ratios: List[float]
    tp_ratios: List[float]
    op_rebound_ratios: List[float]
    tp_rebound_ratios: List[float]
    enable_global_tp_price_ratio_check: bool = True
    global_tp_price_ratio: Optional[float] = None
    enable_global_tp_amount_check: bool = False
    enable_global_sl_amount_check: bool = False
    global_tp_amount_target: Optional[float] = None
    global_sl_amount_target: Optional[float] = None
    # 是否跳过第一层（level_id == 1）的"反弹等待"：
    # - True: 收到 entry 信号后直接开仓，状态变为 WAITING_TP_ACTIVE
    # - False: 收到 entry 信号后状态变为 WAITING_OP_REBOUND，等待价格反弹后再开仓
    skip_first_level_rebound: bool = True
    skip_levels_exit_number: int = 0  # 控制允许跳过多少层级退出触发事件，0则允许所有层级退出触发，1则不许退出第1层，2则不许退出前两层（level_id从1开始），以此类推
    # 是否启用性能指标计算（包括累计手续费、成交金额、订单记录、已实现损益等）
    # - True: 启用所有性能指标计算（默认，适合回测和分析）
    # - False: 禁用性能指标计算（提高性能，适合实盘交易）
    enable_performance_metrics: bool = False
    # is_base_mode: True 表示币本位（amount 是 base 货币数量），False 表示 U 本位（amount 是 quote 货币金额）
    is_base_mode: bool = False

    @field_validator("n_levels", mode="before")
    @classmethod
    def cast_n_levels(cls, v):
        v = int(v)
        if v <= 0:
            raise ValueError("n_levels 必须为正整数")
        return v

    @model_validator(mode="after")
    def validate_lengths(self):
        fields = ["amount", "op_ratios", "tp_ratios", "op_rebound_ratios", "tp_rebound_ratios"]
        for f in fields:
            values = getattr(self, f)
            if len(values) != self.n_levels:
                raise ValueError(f"{f} 列表长度必须与 n_levels ({self.n_levels}) 匹配")
        return self

    @model_validator(mode="after")
    def normalize_values(self):
        """将用户配置规范化：
        - amount 取绝对值（防御负数）
        - 各 ratio 取绝对值（逻辑上应为正比例）
        - 全局 TP/SL 若提供为数字，规范化为绝对值 float（输入正负均可）
        """

        def to_abs_float_list(vals):
            return [abs(float(v)) for v in vals]

        self.amount = to_abs_float_list(self.amount)
        self.op_ratios = to_abs_float_list(self.op_ratios)
        self.tp_ratios = to_abs_float_list(self.tp_ratios)
        self.op_rebound_ratios = to_abs_float_list(self.op_rebound_ratios)
        self.tp_rebound_ratios = to_abs_float_list(self.tp_rebound_ratios)

        if self.global_tp_amount_target is not None:
            self.global_tp_amount_target = abs(float(self.global_tp_amount_target))
        if self.global_sl_amount_target is not None:
            self.global_sl_amount_target = abs(float(self.global_sl_amount_target))
        if self.global_tp_price_ratio is not None:
            self.global_tp_price_ratio = abs(float(self.global_tp_price_ratio))
        return self

    def get_level_configs(self) -> List[DynamicGridLevelConfig]:
        """生成层级配置列表
        level_id 从 1 开始，对应数组索引从 0 开始
        例如：n_levels=40 时，生成 level_id=1 到 40 的配置
        """
        configs = [
            DynamicGridLevelConfig(
                pair=self.pair,
                is_short=self.is_short,
                level_id=i + 1,  # level_id 从 1 开始（1 到 n_levels）
                amount=self.amount[i],  # 数组索引从 0 开始（0 到 n_levels-1）
                op_ratio=self.op_ratios[i],
                tp_ratio=self.tp_ratios[i],
                op_rebound_ratio=self.op_rebound_ratios[i],
                tp_rebound_ratio=self.tp_rebound_ratios[i],
                is_base_mode=self.is_base_mode,  # 传递 is_base_mode
            )
            for i in range(self.n_levels)
        ]
        return configs
