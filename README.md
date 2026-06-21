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
  │   ├─ --limit: 限价起仓
  │   └─ --adopt: 基准价起仓
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

> **仅实盘**：项目已移除模拟盘（dry-run）。如需测试，请用小额资金（如 `--sz 1`）在实盘验证。

### 实盘交易

⚠️ **风险提示**：实盘会产生真实盈亏，请先小额测试！

#### 模式 1：市价起仓（最简单）
```bash
python -m strategy \
  --symbol BGBUSDT \
  --sz 4 \
  --grid 0.005
```

#### 模式 2：限价起仓（指定价格）
```bash
python -m strategy \
  --symbol BGBUSDT \
  --sz 4 \
  --grid 0.005 \
  --limit 1.80
```

#### 模式 3：基准价起仓（自动偏移）
```bash
python -m strategy \
  --symbol BGBUSDT \
  --sz 4 \
  --grid 0.005 \
  --adopt 1.75
```

**完整参数示例**：
```bash
python -m strategy \
  --symbol BGBUSDT \
  --sz 4 \
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
python -m strategy --symbol WLDUSDT --sz 100 --grid 0.02

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
python -m strategy --symbol WLDUSDT --sz 100 --grid 0.02

# 终端 2：运行 BGBUSDT
python -m strategy --symbol BGBUSDT --sz 4 --grid 0.005

# 终端 3：运行 TONUSDT
python -m strategy --symbol TONUSDT --sz 50 --grid 0.015
```

---

## ⚙️ 参数详解

### 必填参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--symbol` | 交易对（大写） | `--symbol WLDUSDT` |
| `--sz` | 每单张数（⚠️ **必须显式传入**） | `--sz 100` |

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
| `--limit` | 限价起仓，立即启动网格 | 等待特定价格 | `--limit 1.80` |
| `--adopt` | 基准价起仓，自动往上偏移 | 用基准价做参考 | `--adopt 1.75` |

**三种模式详解**：

#### 1️⃣ 默认（市价起仓）
```bash
python -m strategy \
  --symbol BGBUSDT --sz 4 --grid 0.005
```
- **动作**：立即市价空 1 张
- **stack_top**：设置为成交价
- **网格启动**：成交后启动
- **优点**：最快入场
- **缺点**：可能遇到滑点

#### 2️⃣ 限价模式（--limit）
```bash
python -m strategy \
  --symbol BGBUSDT --sz 4 --grid 0.005 \
  --limit 1.80
```
- **动作**：限价挂 1 张 @ 1.80，**网格立即启动**
- **stack_top**：设置为 1.80（不等成交）
- **BUY 梯队**：立即在 1.75, 1.73, 1.71... 挂单
- **SELL 挂单**：在 1.80 等待成交
- **优点**：控制入场价，网格无延迟
- **缺点**：如果价格没有达到 1.80，SELL 可能永不成交

#### 3️⃣ 基准价模式（--adopt）
```bash
python -m strategy \
  --symbol BGBUSDT --sz 4 --grid 0.005 \
  --adopt 1.75
```
- **动作**：基准 1.75 自动往上偏移 0.5%（grid 幅度）→ SELL @ 1.7538
- **stack_top**：设置为基准价 1.75（不是成交价）
- **BUY 梯队**：立即在 1.726, 1.703, 1.681... 挂单
- **SELL 挂单**：在 1.7538 等待成交
- **优点**：基于基准价自动偏移，更灵活
- **缺点**：需要手动指定基准价

**模式优先级**（同时指定时）：
```
--adopt > --limit > 默认市价
```

### 风控参数（重要）

| 参数 | 默认值 | 说明 | 调整建议 |
|------|--------|------|---------|
| `--max-notional-usdt` | 10000 | 单笔持仓名义价值上限（USDT） | 根据资金量调整 |
| `--min-margin-ratio` | 5.0 | 最小保证金率（5.0=500%） | 保守可调至 8.0 |

**示例**：
```bash
# 保守配置：单笔不超过 5000 USDT，保证金率不低于 800%
python -m strategy \
  --symbol BTCUSDT \
  --sz 0.1 \
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

### 核心原则：风控只拦"加风险"的方向（SELL）

> SELL = 加空 = 放大风险 → **每次新增 SELL（起仓 + 加仓）都过同一道风控闸门**。
> BUY = 平空 = 降低风险 → **平仓单一律放行**，不做账户风控。

### 统一风控函数 `_can_open_sell`

起仓和网格加 SELL **共用同一函数、同一套规则，无开关无分支**。拉一次账户快照（REST），复核两条：

```python
def _can_open_sell(self, px) -> bool:
    equity   = 账户权益(accountEquity)
    mmr      = 空头持仓维持保证金(mmr)
    cur_size = 空头持仓张数(total)

    # ① 保证金率 ≥ 500%
    if mmr > 0 and equity / mmr < 5.0:
        notify("⚠️ 新增 SELL 被风控拒: 保证金率 < 500%")
        return False

    # ② 加仓后总名义 ≤ 上限
    notional = (cur_size + POSITION_SZ) * px
    if notional > max_notional_usdt:   # 默认 10000 USDT
        notify("⚠️ 新增 SELL 被风控拒: 总名义超限")
        return False

    return True   # 无持仓 / 查询异常 → 放行
```

### 两个调用路径（区别只在被拒动作）

| 路径 | 函数 | 被拒动作 |
|------|------|---------|
| **起仓** | `_open`（市价 / 限价 / 基准三模式） | ⛔ 停策略（`sys.exit`） |
| **加 SELL** | `_place_sell`（成交/对账后移动 SELL） | ⚠️ 跳过不挂，**不动现有挂单**，下次成交/对账重试 |

- 保证金率 < 500% **或** 总名义超限 → 拒
- `mmr=0`（无持仓）/ 查询异常 → 放行（不误杀，靠 60s 对账和 monitor 兜底）
- 平仓 BUY（`reduce_only=True`）→ 跳过风控，直接挂

> 与独立的 [monitor.py](monitor.py) 配合：monitor 负责事后 Telegram 告警，本闸门负责**下单前拦截**，双重防护。

### BUY 重挂退避（防系统撤单死循环）

> 借鉴 OKX 设计：同一价位的 BUY 在交易所被系统反复撤单时，盲目重挂会刷爆下单频率（429 限流）。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `BUY_CANCEL_BACKOFF_N` | 3 | 窗口内被系统撤几次后触发退避 |
| `BUY_CANCEL_WINDOW_SEC` | 120 | 退避计数窗口（秒）|
| `BUY_CANCEL_BACKOFF_SEC` | 180 | 退避时长（秒），期间该价位不再挂 |

**逻辑**：
- 本策略主动撤的 BUY 在 `_refresh_orders` 里已先移出 `pending_buys`，所以走到 `_handle_buy_cancel` 的都是**系统/交易所侧撤单**，计入退避。
- 同价位 120s 内被撤 ≥3 次 → 该价位退避 180s（`_refresh_orders` 跳过），并发 Telegram 告警。
- 退避到期后由 60s 定时对账自然补挂。
- 退避状态是**内存态**（不持久化），重启即清零。

### SELL 限价带处理（不降级）

> 借鉴 OKX 设计：市价急涨穿过网格时，新 SELL 目标价可能落在交易所限价带之外被拒。

**行为**：SELL 被交易所拒单 → **不降级转市价、不退出策略**，仅记录 + Telegram 告警，等下次成交/对账重试。

- ✅ 避免追涨乱加空（市价回落到带内自动恢复挂 SELL）
- ✅ 持仓的 BUY 平仓网格不受影响，继续保护
- ⚠️ 已知局限：单边大涨时 `stack_top` 跟不上，策略在涨势中"安全暂停"加仓

### 其他安全机制

| 机制 | 说明 |
|------|------|
| **启动清理挂单** | 每次启动自动撤销交易所遗留订单，防止重复挂单 |
| **持仓接管** | state 文件丢失但交易所有持仓时，自动接管而非重复开仓 |
| **文件锁保护** | 多进程同时运行同一币种时，PKL 文件加独占锁 |
| **reduceOnly 强制** | 所有 BUY 平仓单强制带 `reduceOnly=yes`，防止反手开多 |
| **精度校验** | 下单前按合约 `pricePrecision` 取整价格，避免被拒单 |

---

## 📡 账户监控 (monitor.py)

独立的后台监控进程，与策略**并发运行、互不依赖**，专注账户风险监测和"策略存活检测"。

```bash
# 与策略分开跑（建议长驻）
nohup python3 monitor.py > monitor.log 2>&1 &
tail -f monitor.log
```

### 监控维度

REST 每 60s 拉一次账户/持仓快照，跑全量检查：

| 维度 | 分级/条件 | 动作 |
|------|----------|------|
| **保证金率** | > 1000% | 仅日志 |
| | < 700%（警告）| ⚡ TG 告警（节流 10 分钟）|
| | < 500%（严重）| ⚠️ TG 告警（策略已停止加仓）|
| | < 300%（紧急）| 🚨 TG 告警（建议立即手动减仓）|
| **资金费率** | 空头被吃 ≥ 0.1%/8h（警告）/ ≥ 0.2%/8h（严重）| TG 告警（节流 1 小时）|
| **挂单形态** | 持仓的 SELL≠1 或 BUY<1，连续 3 次 tick | 🩺 告警"策略可能已停"（节流 30 分钟）|
| **心跳** | 每 1 小时 | 💓 发送运行摘要 |
| **权益曲线** | 每 tick | 追加到 `equity_log.csv` |

### 🩺 挂单形态探针（核心）

> 移植自 OKX monitor 设计——**检测策略进程是否在服务器上悄悄挂掉**。

- **原理**：网格策略对每个空头持仓维持「恰好 1 个 SELL 加空单 + 至少 1 个 BUY 平空单」。挂单是 GTC，策略进程死后挂单仍留在交易所，但不再被移动/补挂，迟早漂移成畸形（如 SELL 成交后无人补 → 0 个 SELL）。
- **用挂单形态当探针**：连续 3 次 tick（约 3 分钟）都畸形才告警，过滤掉 refresh 撤挂空窗、限价带安全暂停等瞬时态。
- **价值**：你用 `nohup` 在服务器跑策略，进程崩溃时这是**唯一能及时发现的途径**。

### 权益曲线 CSV

`equity_log.csv` 字段：`ts, datetime, equity, mgn_ratio, mmr, n_positions`，可用于事后分析权益增长 / 风险率趋势。

### 与策略的关系

- ✅ 完全独立，只读账户数据，不下单不改仓
- ✅ 不启动也不影响策略（可选）
- ⚠️ 与策略都可能发 TG，但角度不同（monitor=账户风险/存活，strategy=交易事件）

> 资金费率来源：`/api/v3/market/tickers` 的 `fundingRate` 字段（与行情同端点）。做空在**负费率**时支付资金费。

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
python -m strategy --symbol WLDUSDT --sz 100 --grid 0.02
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
python -m strategy --symbol WLDUSDT --sz 100 --grid 0.015
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

### Q7: 如何安全测试？

项目**仅支持实盘**（已移除模拟盘）。建议：
1. 用小额资金起步（如 `--sz 1`），观察日志和 Telegram 通知是否正常
2. 确认风控、起仓、加减仓全流程无误
3. 逐步加大仓位

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
- 建议先小额实盘测试（如 `--sz 1`），确认参数合适后再加大仓位

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
1. 实盘从小额开始（如 `--sz 1`）
2. 设置合理的 `--max-notional`（建议 ≤ 账户资金的 10%）
3. 定期检查 Telegram 通知和日志
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
- 示例：`--sz 10 --grid 0.02 --max-notional-usdt 100`

**稳健型**（有经验）：
- 每单价值：100 ~ 500 USDT
- 最大持仓：≤ 10 单
- 总资金占用：≤ 3000 USDT
- 示例：`--sz 50 --grid 0.015 --max-notional-usdt 500`

**激进型**（专业玩家）：
- 每单价值：500 ~ 2000 USDT
- 最大持仓：≤ 20 单
- 总资金占用：≤ 20000 USDT
- 示例：`--sz 200 --grid 0.01 --max-notional-usdt 2000`

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
| Order-Channel | 订单状态更新（本策略使用） | [官方文档](https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Order-Channel) |
| Fill-Channel | 成交明细 | [官方文档](https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Fill-Channel) |
| Positions-Channel | 持仓更新 | [官方文档](https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Positions-Channel) |

**连接信息**（已验证，记录于此避免重复查询）：

```
私有频道 URL: wss://ws.bitget.com/v2/ws/private
心跳: 每 30s 发送字符串 "ping"，服务端回 "pong"（2 分钟无 ping 断连）
限速: 每秒最多 10 条消息
```

**① 登录认证**（`op=login`，`args` 为**数组**）：
```json
{
  "op": "login",
  "args": [{
    "apiKey": "<api_key>",
    "passphrase": "<passphrase>",
    "timestamp": "<unix_秒>",
    "sign": "Base64(HMAC_SHA256(secretKey, timestamp + 'GET' + '/user/verify'))"
  }]
}
```
> 注意：签名用 **GET + /user/verify**，timestamp 为**秒**（30s 过期）。

**② 订阅订单频道**（`op=subscribe`，`args` 为**数组**）：
```json
{
  "op": "subscribe",
  "args": [{"instType": "USDT-FUTURES", "channel": "orders", "instId": "BGBUSDT"}]
}
```
> `instId` 可为具体交易对或 `"default"`（全部）。订阅成功响应为 `{"event":"subscribe","arg":{...}}`。

**③ 推送数据结构**（`action`=snapshot/update）：
```json
{
  "action": "snapshot",
  "arg": {"instType": "USDT-FUTURES", "channel": "orders", "instId": "default"},
  "data": [{
    "orderId": "133...",
    "clientOid": "...",
    "status": "filled",
    "side": "buy",
    "price": "3000",
    "size": "0.4",
    "priceAvg": "3000",
    "accBaseVolume": "0.4",
    "fillPrice": "",
    "uTime": "1760461517274"
  }],
  "ts": 1760461517285
}
```

**关键字段映射**（Bitget → 本策略 on_fill）：

| Bitget 字段 | 本策略字段 | 说明 |
|------|------|------|
| `orderId` | `ordId` | 订单 ID |
| `status` | `status` | `filled`/`canceled`（注意单 l）/`partially_filled`/`live` |
| `priceAvg` | `avgPx` | 成交均价 |
| `accBaseVolume` | `accFillSz` | 累计成交数量 |

**降级**：WebSocket 连接失败时自动降级到每 60s 定时对账。

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
**版本**：v2.9.0（monitor 升级：保证金率 4 级 + 资金费率 + 挂单形态探针 + 权益曲线 CSV）
