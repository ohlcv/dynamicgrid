"""
DynamicGrid 策略入口文件

Freqtrade 通过 recursive_strategy_search 找到这个文件，
然后加载包版本的 DynamicGridStrategy 类。

重要说明：
- Freqtrade 只关心类名（DynamicGridStrategy），不关心文件名
- 文件名可以是任何名称（如 dynamicgrid_strategy.py, DynamicGridStrategy.py, foo.py）
- 只要文件中定义了 class DynamicGridStrategy，Freqtrade 就能找到它

配置说明：
- config.json 中设置 "strategy": "DynamicGridStrategy"（类名）
- config.json 中设置 "recursive_strategy_search": true
- strategy_config.json 通过 add_config_files 加载

目录结构：
user_data/
├── config.json                  # Freqtrade 主配置
├── strategy_config.json         # DynamicGrid 策略配置
└── strategies/
    ├── dynamicgrid_strategy.py  # 本文件（入口包装器，文件名可任意）
    └── dynamicgrid/             # 包版本策略代码
        ├── __init__.py
        ├── core/
        ├── adapters/
        └── utils/
"""
import sys
from pathlib import Path

# 将 dynamicgrid 包目录添加到 sys.path（如果尚未添加）
_current_dir = Path(__file__).parent.resolve()
_pkg_dir = _current_dir / "dynamicgrid"

# 添加包的父目录，使 `from dynamicgrid.xxx import yyy` 可以工作
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

# 从包版本导入并重新导出策略类
# 注意：这里使用绝对导入，因为我们已经把 _current_dir 加入了 sys.path
from dynamicgrid.adapters.freqtrade.strategy import DynamicGridStrategy

# 确保 Freqtrade 可以找到这个类
__all__ = ["DynamicGridStrategy"]
