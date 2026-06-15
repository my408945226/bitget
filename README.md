# Bitget USDT 本位合约 - 金字塔做空网格机器人

> **Maker 优先版震荡市做空策略**：常驻 postOnly 挂单，价格上涨时自动加仓，回落时自动平仓，循环套利。

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue)](https://www.python.org)
[![Bitget UTA v3 API](https://img.shields.io/badge/Bitget-UTA_v3_API-orange)](https://www.bitget.com/zh-CN/api-doc/uta/intro)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 📋 目录

- [核心特性](#-核心特性)
- [策略原理](#-策略原理)
- [安装配置](#-安装配置)
- [运行指南](#-运行指南)
- [参数详解](#-参数详解)
- [风控系统](#-风控系统)
- [Telegram 通知](#-telegram-通知)
- [常见问题](#-常见问题)
- [风险提示](#-风险提示)

---

## ✨ 核心特性

### 🎯 策略优势

✅ **Maker 手续费优惠** - postOnly 挂单享受 maker 费率 (0.02%)，降低交易成本  
✅ **智能网格管理** - 基于栈顶价格自动计算 SELL/BUY 梯队，diff 算法避免重复挂单  
✅ **双层风控保护** - 保证金率检查 + 单笔持仓限额，防止极端行情爆仓  
✅ **Watchdog 防误触发** - 价格穿过挂单需持续 10 秒才降级市价，减少震荡市滑点  
✅ **Telegram 实时推送** - 起仓、成交、平仓等关键事件即时通知  
✅ **多进程隔离** - 每币种独立进程 + 文件锁保护，状态安全不冲突  
✅ **断点续跑** - PKL 状态持久化，重启后自动接管持仓和挂单  

### 🛡️ 生产级稳定性

- ✅ **WebSocket 实时推送** - Order-Channel + Fill-Channel 毫秒级推送成交事件
- ✅ **双层防线** - WebSocket（实时）+ 定时对账（每 60s 防漏）
- ✅ **启动清理挂单** - 自动撤销交易所遗留订单，防止重复挂单
- ✅ **逻辑简洁** - 只保留核心逻辑，降低出错风险
- ✅ **跨平台文件锁** - Windows (msvcrt) / Linux/Mac (fcntl) 自动适配
- ✅ **异常容错** - 网络抖动、API 限流、行情中断时自动恢复

---

## 🧠 策略原理

### 核心思想

> **震荡市做空机器**：价格在区间内波动时，通过"高卖低买"循环套利。

```
价格上涨 → postOnly SELL 成交 → 加空一档（栈顶上移）
价格回落 → postOnly BUY 成交  → 平掉一档（LIFO 等价）
```

### 网格几何

设当前栈顶价为 `T`，网格幅度 `g`（如 2%）：

```
SELL 挂单（加仓）:  T × (1 + g)            ← 永远只有 1 个
BUY 挂单梯队（平仓）:
  BUY₁: T × (1 - g)¹
  BUY₂: T × (1 - g)²
  BUY₃: T × (1 - g)³
  ...
  BUYₙ: T × (1 - g)ⁿ   （n = min(持仓数, MAX_BUYS)）
```

**示例**（T=100, g=2%, 持仓 3 档）：
```
SELL @ 102.00  （加仓目标）
─────────────────────
当前栈顶: 100

BUY₁ @ 98.00   （第 1 档平仓）
BUY₂ @ 96.04   （第 2 档平仓）
BUY₃ @ 94.12   （第 3 档平仓）
```

### 工作流程

```
启动 (init)
  ├─ 获取合约信息
  ├─ 设置账户配置（杠杆、持仓模式）
  ├─ 清理交易所遗留挂单
  ├─ 接管现有持仓 OR 起仓（三种模式）
  │   ├─ 默认：市价起仓 1 张
  │   ├─ --initial-sell-px: 限价起仓
  │   └─ --adopt-sell-px: 基准价起仓
  └─ 挂初始网格（SELL + BUY 梯队）

主循环 (run) - 双层防线
  
  第 1 层：WebSocket（实时推送，后台线程）
    ├─ Order-Channel + Fill-Channel 订阅
    └─ 毫秒级推送成交事件
    
  第 2 层：定时对账（每 60s，防漏推送）
    ├─ 查询交易所实际持仓
    ├─ 对比本地 opens/closes
    └─ 不一致 → 自动修复 + 重挂网格
  
  成交处理流程
  ├─ SELL 成交 → stack_top 更新 → opens++ → 重挂网格
  └─ BUY 成交 → closes++ → 清除所有 BUY → 重挂梯队
```

---

## 📦 安装配置

### 1. 环境要求

- **Python**: 3.11+
- **操作系统**: Windows / Linux / macOS
- **网络**: 能稳定访问 Bitget API（REST + WebSocket）
- **依赖包**: 
  - `requests` - REST API 请求
  - `websockets` - WebSocket 实时推送（推荐）
  - `python-dotenv` - 环境变量配置

### 2. 安装依赖

```bash
# 克隆项目
git clone https://github.com/yourusername/bitget_short_pyramid.git
cd bitget_short_pyramid

# 安装 Python 依赖
pip install -r requirements.txt

# 复制环境变量模板
cp .env.example .env
```

### 3. 配置 API Key

编辑 `.env` 文件：

```env
# ===== Bitget API 配置 =====
BITGET_API_KEY=your_api_key_here
BITGET_SECRET_KEY=your_secret_key_here
BITGET_PASSPHRASE=your_passphrase_here

# ===== Telegram 通知（可选）=====
TG_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TG_CHAT_ID=-1001234567890
```

**获取 API Key 步骤**：
1. 登录 [Bitget 官网](https://www.bitget.com/)
2. 进入「个人中心」→「API 管理」
3. 创建新 API Key，权限勾选：**读取** + **交易**（⚠️ **不要开提现**）
4. 复制 API Key、Secret Key、Passphrase 到 `.env`

**Telegram Bot 创建方法**：
1. 搜索 [@BotFather](https://t.me/BotFather)，发送 `/newbot`
2. 按提示设置 bot 名称，获取 `TG_BOT_TOKEN`
3. 搜索 [@userinfobot](https://t.me/userinfobot)，获取你的 `TG_CHAT_ID`
4. 先给 bot 发送任意消息（激活对话）

---

## 🚀 运行指南

### Dry-Run 模式（模拟测试）

**不发真实订单**，适合验证策略逻辑：

```bash
python -m bitget_short_pyramid.strategy dry-run \
  --symbol WLDUSDT \
  --size 100 \
  --grid 0.02 \
  --interval 5
```

**观察输出**：
```
=== 初始化 WLDUSDT [DRY-RUN] ===
symbolStatus: normal
持仓模式已设置为: one_way_mode
杠杆已设置为 3x
size=100 验证通过
起仓: 市价空 100 张
起仓成功: stack_top=1.760000
[refresh|startup|r123456] SELL: desired=1.795200 cur=None needs_refresh=True
[123456] SELL postOnly 已挂 dry_run_order @ 1.795200
启动完成: 模式=DRY-RUN 网格=2.00% stack_top=1.760000 持仓=1 单
策略启动: [DRY-RUN] 轮询间隔=5s
```

### Live 模式（实盘交易）

⚠️ **风险提示**：实盘会产生真实盈亏，请先小额测试！

#### 模式 1：市价起仓（最简单）
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT \
  --size 4 \
  --grid 0.005
```

#### 模式 2：限价起仓（指定价格）
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT \
  --size 4 \
  --grid 0.005 \
  --initial-sell-px 1.80
```

#### 模式 3：基准价起仓（自动偏移）
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT \
  --size 4 \
  --grid 0.005 \
  --adopt-sell-px 1.75
```

**完整参数示例**：
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT \
  --size 4 \
  --grid 0.005 \
  --interval 5 \
  --leverage 3 \
  --max-notional 10000
```

### 后台运行（Screen）

Linux/macOS 使用 `screen` 保持后台运行：

```bash
# 创建名为 wld_bot 的会话
screen -S wld_bot

# 在 screen 内运行机器人
python -m bitget_short_pyramid.strategy live --symbol WLDUSDT --size 100 --grid 0.02

# 分离会话：按 Ctrl+A，然后按 D
# 重新进入：
screen -r wld_bot

# 查看所有会话：
screen -ls
```

Windows 推荐使用 **PowerShell** 或 **任务计划程序**。

### 多币种同时运行

每个币种开一个独立进程（状态文件按 symbol 隔离）：

```bash
# 终端 1：运行 WLDUSDT
python -m bitget_short_pyramid.strategy live --symbol WLDUSDT --size 100 --grid 0.02

# 终端 2：运行 BGBUSDT
python -m bitget_short_pyramid.strategy live --symbol BGBUSDT --size 4 --grid 0.005

# 终端 3：运行 TONUSDT
python -m bitget_short_pyramid.strategy live --symbol TONUSDT --size 50 --grid 0.015
```

---

## ⚙️ 参数详解

### 必填参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--symbol` | 交易对（大写） | `--symbol WLDUSDT` |
| `--size` | 每单张数（⚠️ **必须显式传入**） | `--size 100` |

### 核心策略参数

| 参数 | 默认值 | 说明 | 推荐范围 |
|------|--------|------|---------|
| `--grid` | 0.02 | 网格幅度（0.02=2%） | 0.005 ~ 0.03 |
| `--interval` | 5 | 轮询间隔（秒） | 3 ~ 10 |
| `--leverage` | 3 | 杠杆倍数 | 2 ~ 5 |

**网格幅度选择建议**：
- **高频震荡**（如 BGB）：`--grid 0.005`（0.5%）
- **中频震荡**（如 WLD）：`--grid 0.015`（1.5%）
- **低频震荡**（如 BTC）：`--grid 0.025`（2.5%）

### 起仓模式参数（三选一，可选）

| 参数 | 说明 | 何时使用 | 示例 |
|------|------|---------|------|
| **默认（无参数）** | 市价起仓 1 张 | 想立即入场 | （无） |
| `--initial-sell-px` | 限价起仓，立即启动网格 | 等待特定价格 | `--initial-sell-px 1.80` |
| `--adopt-sell-px` | 基准价起仓，自动往上偏移 | 用基准价做参考 | `--adopt-sell-px 1.75` |

**三种模式详解**：

#### 1️⃣ 默认（市价起仓）
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT --size 4 --grid 0.005
```
- **动作**：立即市价空 1 张
- **stack_top**：设置为成交价
- **网格启动**：成交后启动
- **优点**：最快入场
- **缺点**：可能遇到滑点

#### 2️⃣ 限价模式（--initial-sell-px）
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT --size 4 --grid 0.005 \
  --initial-sell-px 1.80
```
- **动作**：限价挂 1 张 @ 1.80，**网格立即启动**
- **stack_top**：设置为 1.80（不等成交）
- **BUY 梯队**：立即在 1.75, 1.73, 1.71... 挂单
- **SELL 挂单**：在 1.80 等待成交
- **优点**：控制入场价，网格无延迟
- **缺点**：如果价格没有达到 1.80，SELL 可能永不成交

#### 3️⃣ 基准价模式（--adopt-sell-px）
```bash
python -m bitget_short_pyramid.strategy live \
  --symbol BGBUSDT --size 4 --grid 0.005 \
  --adopt-sell-px 1.75
```
- **动作**：基准 1.75 自动往上偏移 0.5%（grid 幅度）→ SELL @ 1.7538
- **stack_top**：设置为基准价 1.75（不是成交价）
- **BUY 梯队**：立即在 1.726, 1.703, 1.681... 挂单
- **SELL 挂单**：在 1.7538 等待成交
- **优点**：基于基准价自动偏移，更灵活
- **缺点**：需要手动指定基准价

**模式优先级**（同时指定时）：
```
--adopt-sell-px > --initial-sell-px > 默认市价
```

### 风控参数（重要）

| 参数 | 默认值 | 说明 | 调整建议 |
|------|--------|------|---------|
| `--max-notional-usdt` | 10000 | 单笔持仓名义价值上限（USDT） | 根据资金量调整 |
| `--min-margin-ratio` | 5.0 | 最小保证金率（5.0=500%） | 保守可调至 8.0 |

**示例**：
```bash
# 保守配置：单笔不超过 5000 USDT，保证金率不低于 800%
python -m bitget_short_pyramid.strategy live \
  --symbol BTCUSDT \
  --size 0.1 \
  --grid 0.02 \
  --max-notional-usdt 5000 \
  --min-margin-ratio 8.0
```

### 遗留参数（未使用）

以下参数是旧版（层数组版）遗留，**当前 Maker 版策略未使用**，传了也不生效：

- `--multiplier` - 每层递增倍数
- `--layers` - 最大层数
- `--tp-pct` - 止盈比例
- `--min-liq-dist` - 最小强平距离
- `--max-pos-usdt` - 最大持仓价值
- `--max-orders` - 每日最大下单次数

---

## 🛡️ 风控系统

### 双层风控架构

#### 第一层：保证金率检查

**目的**：防止账户权益过低时继续加仓导致爆仓风险。

**实现逻辑**：
```python
def _check_margin_ratio(self) -> bool:
    """检查账户权益"""
    account = self.client.get_account()
    equity = float(account["data"][0].get("accountEquity", 0))
    
    if equity < 100:  # 账户权益 < 100 USDT
        self.log.error(f"[风控] 账户权益 {equity:.2f} < 100 USDT，禁止加仓")
        return False
    return True
```

**触发时机**：
- 每次挂 SELL postOnly 单前
- 每次市价加仓前

**注意**：Bitget UTA v3 API 无直接 `marginRatio` 字段，当前用账户权益作为代理指标。如需更精确的风控，可扩展解析账户资产详情。

#### 第二层：单笔持仓限额

**目的**：防止单次下单过大，分散风险。

**计算公式**：
```
单笔名义价值 = 每单张数 × 当前价格

例如：
  size = 100 张
  price = 50 USDT
  notional = 100 × 50 = 5000 USDT
  
  如果 max_notional_usdt = 10000，则允许下单
  如果 max_notional_usdt = 3000，则拒绝下单
```

**代码实现**：
```python
def _check_position_limit(self, current_price: float) -> bool:
    """检查单笔持仓名义价值"""
    notional = self.POSITION_SZ * current_price
    if notional > self.cfg.max_notional_usdt:
        self.log.error(
            f"[风控] 单笔价值 {notional:.2f} > "
            f"{self.cfg.max_notional_usdt} USDT，禁止加仓"
        )
        return False
    return True
```

### 其他安全机制

| 机制 | 说明 |
|------|------|
| **启动清理挂单** | 每次启动自动撤销交易所遗留订单，防止重复挂单 |
| **持仓接管** | state 文件丢失但交易所有持仓时，自动接管而非重复开仓 |
| **文件锁保护** | 多进程同时运行同一币种时，PKL 文件加独占锁 |
| **reduceOnly 强制** | 所有 BUY 平仓单强制带 `reduceOnly=yes`，防止反手开多 |
| **精度校验** | 下单前按合约 `pricePrecision` 取整价格，避免被拒单 |

---

## 📱 Telegram 通知

### 支持的通知类型

| 事件 | 图标 | 触发条件 |
|------|------|---------|
| 策略启动 | 🚀 | 成功完成初始化 |
| SELL 成交 | 🔻 | 加仓成功 |
| BUY 成交 | 🔺 | 平仓成功（含盈亏） |
| 策略停止 | 🛑 | 收到退出信号 |
| 风控告警 | ⚠️ | 保证金率不足 / 单笔超限 |
| 错误告警 | ❌ | API 失败 / 异常 |

### 消息示例

**策略启动**：
```
🚀 WLDUSDT
策略启动
模式=LIVE
网格=2.00%
每单=100张
持仓=1单
```

**SELL 成交（加仓）**：
```
🔻 WLDUSDT
SELL 成交（加仓）
成交价: 1.785000
opens: 5
```

**BUY 成交（平仓）**：
```
🔺 WLDUSDT
BUY 成交（平仓）
成交价: 1.750000
盈亏: +3.50 USDT
closes: 4
```

### 配置步骤

**1. 创建 Telegram Bot**
```
1. 搜索 @BotFather，发送 /newbot
2. 按提示设置 bot 名称（如 MyTradingBot）
3. 复制获得的 Token（格式：1234567890:ABCdef...）
```

**2. 获取 Chat ID**
```
1. 搜索 @userinfobot，发送任意消息
2. 获取你的 User ID（格式：123456789 或 -1001234567890）
```

**3. 激活 Bot**
```
给你的 bot 发送任意消息（如 "hello"）
```

**4. 配置 .env**
```env
TG_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TG_CHAT_ID=-1001234567890
```

**5. 测试**
```bash
# 运行机器人，观察是否收到启动通知
python -m bitget_short_pyramid.strategy live --symbol WLDUSDT --size 100 --grid 0.02
```

**注意**：
- Telegram 通知是**可选功能**，不配置也能正常运行
- `requests` 库未安装时自动禁用通知
- 发送失败不影响策略核心功能

---

## ❓ 常见问题

### Q1: 启动时报错 "40085 Parameter verification failed"

**原因**：使用了经典账户 API Key，但接口需要 UTA 账户。

**解决**：
1. 确认你的 Bitget 账户已升级为统一账户（UTA）
2. 检查 API Key 权限是否包含「UTA 交易（读写）」
3. 重新创建 API Key，确保勾选 UTA 权限

### Q2: 如何查看当前持仓和挂单？

**方法 1**：查看日志文件
```bash
tail -f logs/WLDUSDT_2026-06-14_10-30-00.log
```

**方法 2**：查看 state 文件（Python）
```python
import pickle
state = pickle.load(open("state_short_pyramid_WLDUSDT.pkl", "rb"))
print(f"stack_top: {state['stack_top']}")
print(f"opens: {state['opens']}, closes: {state['closes']}")
print(f"pending_buys: {len(state['pending_buys'])} 个")
```

**方法 3**：登录 Bitget 网页端查看

### Q3: 重启后会重复开仓吗？

**不会**。启动流程包含持仓接管逻辑：
1. 先查询交易所实际持仓
2. 如果有空仓且本地 `stack_top <= 0`，自动接管
3. 计算 `opens = floor(总持仓 / 每单张数)`
4. 直接挂网格，不再市价起仓

**日志示例**：
```
接管已有持仓: 500 张 @ 1.750000 -> opens=5
```

### Q4: 如何调整网格幅度？

**动态调整**（无需重启）：目前不支持运行时修改，需重启策略。

**重启时调整**：
```bash
# 原网格 2%，改为 1.5%
python -m bitget_short_pyramid.strategy live --symbol WLDUSDT --size 100 --grid 0.015
```

**建议**：小步调整（每次 ±0.5%），观察效果后再决定是否继续调整。

### Q5: 如何停止机器人？

**方法 1**：Ctrl+C（推荐）
```
在运行终端按 Ctrl+C，会保存 state 后安全退出
```

**方法 2**：发送信号
```bash
kill -INT <PID>
```

**方法 3**：强制终止（不推荐，可能丢失状态）
```bash
kill -9 <PID>
```

**退出日志**：
```
收到退出信号，保存 state 后退出...
🛑 WLDUSDT
策略停止
收到退出信号，正常退出
```

### Q6: 多进程运行会冲突吗？

**不会**。每个币种有独立的状态文件和日志：
```
state_short_pyramid_WLDUSDT.pkl
state_short_pyramid_BGBUSDT.pkl
state_short_pyramid_TONUSDT.pkl

logs/WLDUSDT_2026-06-14_10-30-00.log
logs/BGBUSDT_2026-06-14_10-31-00.log
logs/TONUSDT_2026-06-14_10-32-00.log
```

**文件锁保护**：即使意外启动两个相同币种的进程，PKL 文件也会加独占锁，防止数据覆盖。

### Q7: dry-run 和 live 的区别？

| 对比项 | dry-run | live |
|--------|---------|------|
| 真实下单 | ❌ 否 | ✅ 是 |
| API 调用 | 模拟返回 | 真实请求 |
| 价格来源 | 随机波动 | 实时行情 |
| 适用场景 | 测试逻辑 | 实盘交易 |
| 风险提示 | 无 | 需确认 |

**建议流程**：
1. 先用 dry-run 跑 10 分钟，观察日志是否正常
2. 再用小额资金 live 测试（如 `--size 1`）
3. 确认无误后逐步加大仓位

---

## 📊 日志管理

### 日志位置

```
logs/
├── WLDUSDT_2026-06-14_10-30-00.log
├── BGBUSDT_2026-06-14_10-31-00.log
└── TONUSDT_2026-06-14_10-32-00.log
```

**命名规则**：`{SYMBOL}_{YYYY-MM-DD_HH-mm-ss}.log`

每次运行生成新文件，不会覆盖历史日志。

### 日志级别

| 级别 | 说明 | 示例 |
|------|------|------|
| INFO | 常规信息 | 起仓成功、挂单更新 |
| WARNING | 警告 | 价格穿挂单、API 重试 |
| ERROR | 错误 | 风控拒绝、下单失败 |

### 查看日志

**实时跟踪**：
```bash
tail -f logs/WLDUSDT_*.log
```

**筛选关键事件**：
```bash
# 只看成交通知
grep "成交" logs/WLDUSDT_*.log

# 只看风控告警
grep "风控" logs/WLDUSDT_*.log

# 统计今日盈亏
grep "盈亏" logs/WLDUSDT_*.log | tail -20
```

---

## ⚠️ 风险提示

### 🚨 重要声明

**合约做空可能爆仓，本程序不保证盈利。**

- 请只用你能承受损失的资金
- API Key 请勿泄露给任何人
- 建议先用 dry-run 充分验证策略逻辑
- 建议先小额实盘测试，确认参数合适后再加大仓位

### 已知风险

| 风险类型 | 说明 | 缓解措施 |
|---------|------|---------|
| **单边上涨风险** | 价格持续上涨时不断加仓，累积大量空仓 | 设置 `--max-notional-usdt` 限制单笔规模 |
| **滑点风险** | Watchdog 市价降级时可能遇到较大滑点 | 增加 `WATCHDOG_CONFIRM_SECONDS` 延长确认时间 |
| **API 限流风险** | 频繁请求可能被 Bitget 限流 | 默认 5s 轮询间隔，不建议调至 <3s |
| **网络中断风险** | 断网时无法成交，可能错过最佳平仓时机 | 使用稳定网络环境，考虑 VPS 部署 |
| **技术故障风险** | Bug、内存溢出等可能导致异常 | 定期检查日志，及时更新版本 |

### 最佳实践

✅ **推荐做法**：
1. 先用 dry-run 测试至少 30 分钟
2. 实盘从小额开始（如 `--size 1`）
3. 设置合理的 `--max-notional-usdt`（建议 ≤ 账户资金的 10%）
4. 定期检查 Telegram 通知和日志
5. 保留足够的账户余额（建议 ≥ 500 USDT）

❌ **禁止做法**：
1. 直接用大资金实盘
2. 在不了解策略逻辑的情况下运行
3. 将 API Key 泄露给他人或上传到 GitHub
4. 同时运行过多币种（建议 ≤ 3 个）
5. 在极端行情（如暴涨暴跌）时运行

### 资金管理建议

**保守型**（适合新手）：
- 每单价值：≤ 100 USDT
- 最大持仓：≤ 5 单
- 总资金占用：≤ 500 USDT
- 示例：`--size 10 --grid 0.02 --max-notional-usdt 100`

**稳健型**（有经验）：
- 每单价值：100 ~ 500 USDT
- 最大持仓：≤ 10 单
- 总资金占用：≤ 3000 USDT
- 示例：`--size 50 --grid 0.015 --max-notional-usdt 500`

**激进型**（专业玩家）：
- 每单价值：500 ~ 2000 USDT
- 最大持仓：≤ 20 单
- 总资金占用：≤ 20000 USDT
- 示例：`--size 200 --grid 0.01 --max-notional-usdt 2000`

---

## 📚 技术细节

### 文件结构

```
bitget_short_pyramid/
├── strategy.py      # 策略主体（init / run / tick / refresh_orders）
├── client.py        # Bitget UTA v3 客户端（签名 + API 封装）
├── state.py         # 状态持久化（PKL 加载/保存 + 文件锁）
├── config.py        # CLI 参数解析 + .env 读取
├── precision.py     # 精度处理（quantize_price / validate_order_size）
├── logger.py        # 日志管理（独立文件 + 时间戳）
└── utils.py         # 工具函数（clientOid 生成 + Telegram 通知）

logs/                # 日志文件夹
state_short_pyramid_*.pkl  # 状态文件（每币种一个）
.env                 # 环境变量配置
requirements.txt     # Python 依赖
README.md            # 本文档
```

### State 文件结构

```python
{
    "symbol": "WLDUSDT",
    "stack_top": 1.760000,        # 栈顶入场价
    "opens": 5,                   # 累计加仓次数
    "closes": 3,                  # 累计平仓次数
    "total_realized_pnl": 12.50,  # 累计已实现盈亏
    "pending_sell_ord_id": "123456789",
    "pending_sell_px": 1.795200,
    "pending_buys": {
        "987654321": {"entry_px": 1.760000, "target_px": 1.724800},
        "987654322": {"entry_px": 1.724800, "target_px": 1.690304},
    },
    "watchdog_sell_start_ts": 0.0,
    "watchdog_buy_start_ts": 0.0,
    "last_action_time": 1718352000.0,
    "created_at": "2026-06-14 10:30:00",
    "updated_at": "2026-06-14 12:45:30",
}
```

### Bitget UTA v3 API 速查

#### REST API

| 操作 | 接口 | 文档链接 |
|------|------|---------|
| 下单 | `POST /api/v3/trade/place-order` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/trade/place-order) |
| 撤单 | `POST /api/v3/trade/cancel-order` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/trade/cancel-order) |
| 查挂单 | `GET /api/v3/trade/unfilled-orders` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/trade/unfilled-orders) |
| 查订单 | `GET /api/v3/trade/order-info` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/trade/order-info) |
| 行情 | `GET /api/v3/market/tickers` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/market/tickers) |
| 持仓 | `GET /api/v3/position/current-position` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/position/current-position) |
| 资产 | `GET /api/v3/account/assets` | [官方文档](https://www.bitget.com/zh-CN/api-doc/uta/account/assets) |

#### WebSocket（实时推送，优先级最高）

| 频道 | 用途 | 文档链接 |
|------|------|---------|
| Order-Channel | 订单状态更新 | [官方文档](https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Order-Channel) |
| Fill-Channel | 成交推送 | [官方文档](https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Fill-Channel) |
| Positions-Channel | 持仓更新 | [官方文档](https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Positions-Channel) |

**WebSocket 连接**：
- URL: `wss://ws.bitget.com/mix/v1/private/stream`
- 认证：签名方式（详见官方文档）
- 降级：WebSocket 连接失败时自动降级到 REST 轮询

---

## 🤝 贡献与支持

欢迎提交 Issue 和 Pull Request！

**报告 Bug**：请提供
- 完整的错误日志
- 运行命令和参数
- Python 版本和操作系统

**功能建议**：请说明
- 需求背景和使用场景
- 期望的行为和优先级

---

## 📄 许可证

MIT License

---

**最后更新时间**：2026-06-15  
**版本**：v2.3.0（WebSocket 实时推送 + 60s 定时对账 + 逻辑简洁）
