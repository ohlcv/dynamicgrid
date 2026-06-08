"""
DynamicGrid 包初始化

注意：
- 本包仅提供核心算法和工具模块（core、utils、adapters 等），本身不定义任何 vn.py 策略类。
- 这样做的目的是防止诸如 SpreadTrading 等引擎在扫描策略模块时，
  误把整个 dynamicgrid 包当作“单个策略文件”去加载。

真正给 vn.py CTA 引擎使用的策略入口在：
- `strategies/dynamicgrid_strategy.py` （导出 DynamicGridStrategy）
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
