# DynamicGrid 动态网格策略

一个**通用的、框架无关**的动态网格交易策略核心实现，支持多种量化框架。

## 📋 目录

- [概述](#概述)
- [架构设计](#架构设计)
- [核心特性](#核心特性)
- [快速开始](#快速开始)
  - [Freqtrade 使用](#freqtrade-使用)
  - [vnpy 使用](#vnpy-使用)
- [配置说明](#配置说明)
- [项目结构](#项目结构)
- [开发指南](#开发指南)

## 概述

DynamicGrid 是一个智能的多层级网格交易策略，专为高波动性市场设计。它采用**事件驱动架构**，核心逻辑完全**框架无关**，可以轻松适配不同的量化交易框架。

### 核心优势

1. **框架无关的核心逻辑**：核心业务逻辑（事件系统、层级管理、状态机）完全独立于任何交易框架
2. **适配器模式**：通过适配器层轻松支持多种框架（Freqtrade、vnpy等）
3. **事件驱动设计**：基于事件系统实现高度解耦的模块化设计
4. **智能网格系统**：多层级网格、智能回调、动态间距调整
5. **完善的风险控制**：全局止盈止损、层级状态一致性校验

## 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                    应用层 (Application)                   │
│  ┌──────────────┐              ┌──────────────┐         │
│  │  Freqtrade   │              │    vnpy      │         │
│  │   Strategy   │              │  Strategy   │         │
│  └──────┬───────┘              └──────┬───────┘         │
│         │                              │                 │
└─────────┼──────────────────────────────┼─────────────────┘
          │                              │
          ▼                              ▼
┌─────────────────────────────────────────────────────────┐
│                   适配器层 (Adapters)                     │
│  ┌──────────────────┐          ┌──────────────────┐    │
│  │ FreqtradeAdapter │          │  VnpyAdapter     │    │
│  │                  │          │                  │    │
│  │ • 数据转换       │          │ • 数据转换       │    │
│  │ • 事件映射       │          │ • 事件映射       │    │
│  │ • 订单管理       │          │ • 订单管理       │    │
│  └────────┬─────────┘          └────────┬─────────┘    │
└───────────┼─────────────────────────────┼──────────────┘
            │                             │
            └──────────────┬───────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│                   核心层 (Core)                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │    Event     │  │    Level     │  │   Manager    │ │
│  │    System    │  │  State       │  │   Grid      │ │
│  │              │  │  Machine     │  │   Control   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐                                     │
│  │    Config    │                                     │
│  │  Management  │                                     │
│  └──────────────┘                                     │
└─────────────────────────────────────────────────────────┘
```

### 设计原则

1. **关注点分离**：核心逻辑与框架特定代码完全分离
2. **事件驱动**：所有交互通过事件系统进行，实现松耦合
3. **适配器模式**：每个框架只需实现适配器接口
4. **可扩展性**：易于添加新的框架支持

## 核心特性

### 1. 多层级网格系统
- 支持可配置的网格层级数量
- 每层独立的投资金额、开仓比例、止盈比例
- 层级间依赖关系管理

### 2. 智能回调机制
- 基于价格极值（最高/最低价）动态计算反弹价格
- 支持做多/做空双向策略
- 防止重复触发机制

### 3. 状态机管理
- 8种状态：WAITING_PREV_OP, WAITING_OP_ACTIVE, WAITING_OP_REBOUND, 
  WAITING_TP_ACTIVE, WAITING_TP_REBOUND, WAITING_NEXT_CLOSE, COMPLETED, ERROR
- 严格的状态转换验证
- 状态一致性校验

### 4. 全局风险控制
- 价格比例止盈（global_tp_price_ratio）
- 金额止盈（global_tp_amount_target）
- 金额止损（global_sl_amount_target）

### 5. 事件驱动架构
- INBOUND 事件：外部数据/信号输入
- OUTBOUND 事件：系统输出交易指令
- INTERNAL 事件：内部状态变更

## 快速开始

### Freqtrade 使用

#### 1. 安装依赖

```bash
pip install freqtrade
```

#### 2. 配置策略

在 Freqtrade 配置文件中添加：

```json
{
  "strategy": "DynamicGridFreqtradeStrategy",
  "strategy_path": "path/to/dynamicgrid/adapters/freqtrade",
  "dynamicgrid": {
    "pairs": [
      {
        "pair": "BTC/USDT",
        "is_short": false,
        "enabled": true,
        "n_levels": 5,
        "amount": [100, 100, 100, 100, 100],
        "op_ratios": [0.02, 0.02, 0.02, 0.02, 0.02],
        "tp_ratios": [0.01, 0.01, 0.01, 0.01, 0.01],
        "op_rebound_ratios": [0.005, 0.005, 0.005, 0.005, 0.005],
        "tp_rebound_ratios": [0.005, 0.005, 0.005, 0.005, 0.005],
        "enable_global_tp_price_ratio_check": true,
        "global_tp_price_ratio": 0.05
      }
    ],
    "signals": {
      "rsi": {
        "enabled": true,
        "configs": [...]
      }
    }
  }
}
```

#### 3. 运行策略

```bash
freqtrade trade --config config.json
```

### vnpy 使用

#### 1. 安装依赖

```bash
pip install vnpy
```

#### 2. 导入策略

```python
from dynamicgrid.adapters.vnpy.strategy import DynamicGridStrategy
```

#### 3. 配置策略

```python
# 在 vnpy 策略配置中
setting = {
    "dynamicgrid": {
        "pairs": [
            {
                "pair": "BTCUSDT",
                "is_short": False,
                "enabled": True,
                "n_levels": 5,
                "amount": [100, 100, 100, 100, 100],
                "op_ratios": [0.02, 0.02, 0.02, 0.02, 0.02],
                "tp_ratios": [0.01, 0.01, 0.01, 0.01, 0.01],
                "op_rebound_ratios": [0.005, 0.005, 0.005, 0.005, 0.005],
                "tp_rebound_ratios": [0.005, 0.005, 0.005, 0.005, 0.005],
                "enable_global_tp_price_ratio_check": True,
                "global_tp_price_ratio": 0.05
            }
        ]
    }
}
```

#### 4. 运行策略

通过 vnpy 的图形界面或 API 加载策略并运行。

## 配置说明

### 基础配置

```json
{
  "pair": "BTC/USDT",           // 交易对
  "is_short": false,            // 是否做空
  "enabled": true,              // 是否启用
  "n_levels": 5                  // 网格层级数
}
```

### 网格配置

```json
{
  "amount": [100, 100, 100, 100, 100],              // 每层投资金额（计价币）
  "op_ratios": [0.02, 0.02, 0.02, 0.02, 0.02],     // 每层开仓比例
  "tp_ratios": [0.01, 0.01, 0.01, 0.01, 0.01],     // 每层止盈比例
  "op_rebound_ratios": [0.005, ...],                // 每层开仓反弹比例
  "tp_rebound_ratios": [0.005, ...]                 // 每层止盈反弹比例
}
```

### 全局风险控制

```json
{
  "enable_global_tp_price_ratio_check": true,  // 启用价格比例止盈
  "global_tp_price_ratio": 0.05,              // 全局止盈价格比例（5%）
  "enable_global_tp_amount_check": false,      // 启用金额止盈
  "global_tp_amount_target": 1000,             // 全局止盈金额目标
  "enable_global_sl_amount_check": false,      // 启用金额止损
  "global_sl_amount_target": -500             // 全局止损金额目标
}
```

详细配置说明请参考 [DynamicGrid配置说明.md](./DynamicGrid配置说明.md)

## 项目结构

```
dynamicgrid/
├── core/                    # 核心模块（框架无关）
│   ├── __init__.py
│   ├── event.py            # 事件系统
│   ├── config.py           # 配置管理
│   ├── level.py            # 层级状态机
│   └── manager.py          # 网格管理器
│
├── adapters/                # 适配器层
│   ├── __init__.py
│   ├── freqtrade/          # Freqtrade 适配器
│   │   ├── __init__.py
│   │   ├── adapter.py     # Freqtrade 适配器实现
│   │   └── strategy.py    # Freqtrade 策略实现
│   │
│   └── vnpy/               # vnpy 适配器
│       ├── __init__.py
│       ├── adapter.py     # vnpy 适配器实现
│       └── strategy.py    # vnpy 策略实现
│
├── utils/                   # 通用工具
│   ├── __init__.py
│   └── tag_utils.py        # 订单标签工具
│
├── __init__.py
└── README.md
```

## 开发指南

### 添加新框架支持

1. **创建适配器目录**
   ```
   adapters/your_framework/
   ├── __init__.py
   ├── adapter.py
   └── strategy.py
   ```

2. **实现适配器接口**
   - `handle_data_update()`: 处理K线数据更新
   - `handle_trade()`: 处理成交数据
   - `_map_events_to_orders()`: 将事件映射为订单操作

3. **实现策略类**
   - 继承框架的策略基类
   - 在数据更新回调中调用适配器
   - 在成交回调中调用适配器

### 核心模块使用

```python
from dynamicgrid.core import (
    DynamicGridConfig,
    DynamicGridManager,
    DynamicGridDataUpdateEvent,
    DynamicGridAddTriggerEvent,
)

# 创建配置
config = DynamicGridConfig(
    pair="BTC/USDT",
    is_short=False,
    n_levels=5,
    amount=[100, 100, 100, 100, 100],
    op_ratios=[0.02, 0.02, 0.02, 0.02, 0.02],
    tp_ratios=[0.01, 0.01, 0.01, 0.01, 0.01],
    op_rebound_ratios=[0.005, 0.005, 0.005, 0.005, 0.005],
    tp_rebound_ratios=[0.005, 0.005, 0.005, 0.005, 0.005],
)

# 创建管理器
manager = DynamicGridManager(config)

# 处理数据更新
event = DynamicGridDataUpdateEvent.create(
    pair="BTC/USDT",
    open_price=50000,
    high=51000,
    low=49000,
    close=50500,
    volume=1000,
)

# 获取输出事件
outbound_events = manager.process_event(event)

# 处理输出事件
for event in outbound_events:
    if isinstance(event, DynamicGridAddTriggerEvent):
        # 执行加仓操作
        pass
    elif isinstance(event, DynamicGridReduceTriggerEvent):
        # 执行减仓操作
        pass
```

## 许可证

[MIT]

## 贡献

欢迎提交 Issue 和 Pull Request！

## 相关文档

- [DynamicGrid策略说明.md](./DynamicGrid策略说明.md) - 策略详细说明
- [DynamicGrid设计说明.md](./DynamicGrid设计说明.md) - 技术设计文档
- [DynamicGrid配置说明.md](./DynamicGrid配置说明.md) - 配置参数说明

同行是战友不是敌人，代码开源我的GitHub上了，同道中人砥砺前行，苟富贵莫相忘。
相关文章链接：https://www.meowsite.cn/zh/blog/%e9%a1%b9%e7%9b%ae%e5%ae%9e%e8%b7%b5/%e7%bc%a0%e8%ae%ba%e9%87%8f%e5%8c%96%e4%ba%a4%e6%98%93%e7%b3%bb%e7%bb%9f%e8%bf%9b%e5%ba%a6%e6%8a%a5%e5%91%8a/

