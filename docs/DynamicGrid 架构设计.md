# DynamicGrid 架构设计

## 概述

DynamicGrid 采用**分层架构**和**适配器模式**，实现了核心逻辑与框架的完全解耦。

## 架构层次

### 1. 核心层 (Core Layer)

**位置**: `core/`

**职责**: 提供框架无关的核心业务逻辑

**模块**:

- `event.py`: 事件系统定义
- `config.py`: 配置管理和验证
- `level.py`: 单个网格层级的状态机
- `manager.py`: 网格管理器，协调多个层级

**特点**:

- ✅ 完全框架无关
- ✅ 纯 Python，无外部依赖（除标准库和 Pydantic）
- ✅ 事件驱动设计
- ✅ 状态机管理

### 2. 适配器层 (Adapter Layer)

**位置**: `adapters/`

**职责**: 将不同框架的数据和接口转换为核心事件系统

**适配器**:

- `freqtrade/`: Freqtrade 框架适配器
- `vnpy/`: vnpy 框架适配器

**适配器接口**:

每个适配器需要实现：

```python
class DynamicGridAdapter:
    def handle_data_update(self, ...) -> List[Order]:
        """处理K线数据更新，返回订单操作列表"""
        pass

    def handle_trade(self, ...) -> None:
        """处理成交数据，更新网格状态"""
        pass

    def initialize_from_config(self, config: dict) -> None:
        """从配置初始化网格系统"""
        pass
```

### 3. 应用层 (Application Layer)

**位置**: `adapters/{framework}/strategy.py`

**职责**: 框架特定的策略实现

**实现**:

- Freqtrade: 继承 `IStrategy`
- vnpy: 继承 `CtaTemplate`

## 数据流

### 数据更新流程

```
外部框架 (Freqtrade/vnpy)
    ↓
适配器 (Adapter)
    ↓ 转换为 DynamicGridDataUpdateEvent
核心层 (Core)
    ↓ 处理事件，生成 OUTBOUND 事件
适配器 (Adapter)
    ↓ 映射为框架订单操作
外部框架 (Freqtrade/vnpy)
    ↓ 执行订单
```

### 成交反馈流程

```
外部框架 (Freqtrade/vnpy)
    ↓ 订单成交
适配器 (Adapter)
    ↓ 转换为 DynamicGridAddSuccessEvent/ReduceSuccessEvent
核心层 (Core)
    ↓ 更新层级状态
```

## 事件系统

### 事件方向

- **INBOUND**: 外部 → 系统
  
  - `DynamicGridDataUpdateEvent`: K线数据更新
  - `DynamicGridAddSuccessEvent`: 加仓成功
  - `DynamicGridReduceSuccessEvent`: 减仓成功
  - `DynamicGridGlobalExitSuccessEvent`: 全局退出成功

- **OUTBOUND**: 系统 → 外部
  
  - `DynamicGridAddTriggerEvent`: 加仓触发
  - `DynamicGridReduceTriggerEvent`: 减仓触发
  - `DynamicGridGlobalExitTriggerEvent`: 全局退出触发

- **INTERNAL**: 系统内部
  
  - `DynamicGridStateChangeEvent`: 状态变更

### 事件处理流程

```
1. 适配器接收外部数据
   ↓
2. 创建 INBOUND 事件
   ↓
3. Manager.process_event(event)
   ↓
4. Level 处理事件，更新状态
   ↓
5. 生成 OUTBOUND 事件
   ↓
6. 适配器映射为订单操作
```

## 状态机

### 层级状态

每个网格层级有 8 种状态：

1. **WAITING_PREV_OP**: 等待前一层开仓
2. **WAITING_OP_ACTIVE**: 等待开仓激活（价格达到目标）
3. **WAITING_OP_REBOUND**: 等待开仓反弹（价格回调）
4. **WAITING_TP_ACTIVE**: 等待止盈激活（价格达到止盈目标）
5. **WAITING_TP_REBOUND**: 等待止盈反弹（价格回调）
6. **WAITING_NEXT_CLOSE**: 等待后一层关闭
7. **COMPLETED**: 已完成（已平仓）
8. **ERROR**: 错误状态

### 状态转换规则

状态转换由 `VALID_TRANSITIONS` 定义，确保状态转换的合法性。

## 配置管理

### 配置结构

```python
DynamicGridConfig
├── pair: str                    # 交易对
├── is_short: bool               # 是否做空
├── enabled: bool                # 是否启用
├── n_levels: int                # 层级数
├── amount: List[float]          # 每层投资金额
├── op_ratios: List[float]      # 每层开仓比例
├── tp_ratios: List[float]      # 每层止盈比例
├── op_rebound_ratios: List[float]  # 每层开仓反弹比例
├── tp_rebound_ratios: List[float]  # 每层止盈反弹比例
└── 全局风险控制参数...
```

### 配置验证

使用 Pydantic 进行配置验证，确保：

- 类型正确
- 数值范围合理
- 层级数量匹配

## 扩展性

### 添加新框架支持

1. **创建适配器目录**
   
   ```
   adapters/new_framework/
   ├── __init__.py
   ├── adapter.py
   └── strategy.py
   ```

2. **实现适配器**
   
   - 实现数据转换（框架数据 → 核心事件）
   - 实现事件映射（核心事件 → 框架订单）
   - 实现状态恢复（可选）

3. **实现策略类**
   
   - 继承框架的策略基类
   - 在数据回调中调用适配器
   - 在成交回调中调用适配器

### 扩展核心功能

核心模块设计为可扩展的：

- 可以添加新的事件类型
- 可以扩展状态机
- 可以添加新的配置选项

## 设计原则

1. **单一职责**: 每个模块只负责一个功能
2. **开闭原则**: 对扩展开放，对修改关闭
3. **依赖倒置**: 核心层不依赖适配器层
4. **接口隔离**: 适配器接口简洁明确
5. **事件驱动**: 通过事件实现松耦合

## 性能考虑

- 事件处理是同步的，适合单线程环境
- 状态机转换是 O(1) 复杂度
- 层级查找是 O(1) 复杂度（使用字典索引优化）
- 层级遍历是 O(n)，但层级数通常较小（< 20）

## 测试策略

- **单元测试**: 测试核心模块的独立功能
- **集成测试**: 测试适配器与核心模块的集成
- **端到端测试**: 测试完整的数据流

## 未来改进

1. 异步事件处理支持
2. 更多框架适配器（Backtrader, Zipline等）
3. 可视化工具
4. 进一步性能优化（如批量事件处理）
