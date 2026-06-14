# PROMPT — 栈式金字塔做空策略 · Maker 版 (Short Stack Pyramid, Bitget UTA)

> 把这份文档完整复制粘贴给任何 AI 或工程师，对方应能从零实现出一个生产可用的自动化交易机器人。
> 适用平台：**Bitget 统一账户 (UTA) USDT 本位永续合约**，仅使用 `/api/v3` 接口（REST 轮询，无 WS）。
> 本文档与 `bitget_short_pyramid/` 目录下的实际实现一一对应（2026-06-11 实盘验证通过）。

---

## 0. 角色与目标

你是一名资深量化交易工程师，请根据下面**完整的策略规格**，用 Python 实现一个自动化做空机器人。最终交付：

- 在 Bitget UTA 实盘可完整跑通：起仓 → 挂单 → 成交检测 → 重挂 → 平仓
- `dry-run` / `live` 一键切换（同一份代码，dry-run 不发真实请求）
- 多币种通过开多进程实现（每进程一个合约，state / 日志独立）
- 状态持久化 (PKL)、重启接管持仓、异常处理齐全

## 1. 策略思想（一句话）

> **震荡市做空机器（Maker 优先版）：常驻 1 个 postOnly SELL 在栈顶上方 0.5%，常驻 N 个 reduceOnly postOnly BUY 梯队在栈顶下方；SELL 成交即加空一档（栈顶上移），BUY 成交即平掉一档（LIFO 等价）；价格穿过挂单未成交时 watchdog 自动降级市价单。**

与 taker 版（OKX 原型，见 `../okxAPI/PROMPT_short_pyramid.md`）的区别：

| | Taker 版 (OKX 原型) | **Maker 版 (本实现)** |
|---|---|---|
| 触发方式 | tick 价格穿越网格 → 市价单 | 预挂 postOnly 限价单等待成交 |
| 手续费 | taker (0.06%) | **maker (0.02%)**，降级时才付 taker |
| 行情来源 | WebSocket 实时推送 | REST 轮询（默认 5s） |
| 持仓记录 | `stack: List[float]` 完整栈 | `stack_top + opens/closes` 计数器 |
| 兜底机制 | 冷却 30s | watchdog 穿价降级市价 |

## 2. 适用范围与前置条件

| 项 | 必须 |
|---|---|
| 账户类型 | **Bitget 统一账户 (UTA)**，经典账户的 `/api/v2/mix` 接口不适用 |
| 标的合约 | USDT 本位永续，如 `BGBUSDT` / `WLDUSDT`（category=`USDT-FUTURES`） |
| 持仓模式 | **单向持仓 (one_way_mode)**，启动时自动设置 |
| 保证金模式 | **全仓 (cross)**，作为每笔订单的 `tdMode` 参数传递（UTA 无独立设置接口） |
| 杠杆 | 默认 **3x**，启动时自动设置 |
| API 权限 | UTA 交易（读写），**不开提现** |

## 3. 参数表

| 参数 | 默认值 | 说明 | CLI |
|---|---|---|---|
| `GRID_PCT` | 0.02（建议 0.005） | 网格幅度，SELL/BUY 挂单距栈顶的比例 | `--grid` |
| `POSITION_SZ` | **必填** | 每单张数（每次加/平的数量） | `--size`（必须显式传入） |
| `MAX_BUYS` | 60 | 预挂 BUY 梯队数量上限 | 代码常量 |
| `WATCHDOG_BUFFER_PCT` | 0.001 | 价格穿过挂单超过 0.1% 才触发降级 | 代码常量 |
| `interval` | 5 | 主循环轮询间隔（秒） | `--interval` |
| `leverage` | 3 | 杠杆 | `--leverage` |

> ⚠️ **强制要求**：`--size` 必须每次启动显式传入，不允许有"安全"默认值（防止"我以为还是上次那个数"惯性下单）。
>
> ⚠️ **遗留参数**：`--multiplier` / `--layers` / `--tp-pct` / `--min-liq-dist` / `--max-pos-usdt` / `--max-orders` 是旧版（层数组版）的参数，**当前 Maker 版策略未使用**，传了也不生效。

## 4. 核心数据结构（State, PKL 持久化）

```python
state = {
    "symbol": str,
    # —— 栈核心（不存完整栈，只存栈顶 + 计数）——
    "stack_top": float,           # 栈顶入场价（最近一次加仓的成交价）
    "opens": int,                 # 累计加仓次数
    "closes": int,                # 累计平仓次数
    "total_realized_pnl": float,  # 累计已实现盈亏
    # —— Maker 挂单追踪 ——
    "pending_sell_ord_id": str | None,   # 当前常驻 SELL 单的 orderId
    "pending_sell_px": float | None,     # 其挂单价
    "pending_buys": dict,                # {orderId: {"entry_px": float, "target_px": float}}
    # —— 时间戳 ——
    "last_action_time": float,
    "last_refresh_ts": float,
    "last_reconcile_ts": float,
}
```

不变式：

- **当前持仓档数 `n_positions = opens - closes`**
- **`n_positions × POSITION_SZ` == 交易所净空持仓张数**
- 任何 state 字段变动后**立刻** `save_state()`（pickle 到 `state_short_pyramid_<SYMBOL>.pkl`）

## 5. 网格几何

设栈顶价为 `T`，网格幅度 `g`：

```
SELL (加空, 非 reduceOnly):  T × (1 + g)            —— 永远只有 1 个
BUY_i (平空, reduceOnly):    T × (1 - g)^i,  i = 1..min(n_positions, MAX_BUYS)
```

- 所有挂单价必须按合约 `pricePrecision` **向下取整**（否则被拒单）
- SELL 成交 → `stack_top` 上移到成交价 → 整个梯队基于新栈顶重算重挂
- BUY 成交 → `closes += 1` → 梯队深度减一（`stack_top` 不变）

## 6. 启动流程（严格按顺序）

```
[1] 加载状态
    读 state_short_pyramid_<SYMBOL>.pkl，没有 → 空状态

[2] 获取合约信息  GET /api/v3/market/instruments?category=USDT-FUTURES&symbol=...
    把 v3 字段映射为内部名: minOrderQty→minTradeNum, quantityMultiplier→sizeMultiplier,
    minOrderAmount→minTradeUSDT, pricePrecision→pricePlace, quantityPrecision→volumePlace
    status != "online" → 拒绝启动

[3] 设持仓模式  POST /api/v3/account/set-hold-mode  {holdMode: "one_way_mode"}

[4] 设杠杆      POST /api/v3/account/set-leverage   {symbol, category, leverage}

[5] size 验证   用标记价 + 合约信息校验 minTradeNum / sizeMultiplier / minTradeUSDT

[6] 起仓判定 (stack_top <= 0 时):
    [6a] 先查交易所持仓 —— 若已有空仓 → **接管**:
         stack_top = openPriceAvg, opens = int(total / POSITION_SZ), closes = 0
         （这一步防止重启后重复开仓，是踩过的真实大坑）
    [6b] 没有持仓 → 市价空 POSITION_SZ 张起仓:
         成交价 = 标记价（备选: 查持仓均价）; 都拿不到 → 报错退出，绝不瞎猜价格

[7] refresh_orders(reason="startup")
    按 §7 的 diff 算法挂出 1 SELL + N BUY（重启且挂单未变时应当 cancel=0 place=0）
```

## 7. refresh_orders — 挂单 diff 算法（策略心脏之一）

目标态：1 个 SELL + `min(n_positions, MAX_BUYS)` 个 BUY。**只撤/挂有变化的单，绝不全撤全挂**：

```python
def refresh_orders(reason):
    desired_sell_px = round_px(stack_top * (1 + GRID_PCT))
    # SELL: 无单 / 价格不符 → 撤旧挂新; 否则不动
    if not pending_sell_ord_id or abs(pending_sell_px - desired_sell_px) > 1e-9:
        cancel(pending_sell_ord_id); place_post_only_sell(desired_sell_px)

    # BUY 梯队: 容差匹配 (tol = stack_top × GRID_PCT × 0.4)
    desired = [round_px(stack_top * (1-GRID_PCT)**i) for i in 1..depth]
    to_cancel = [挂着但不在 desired 容差内的单]
    to_place  = [desired 中没有对应挂单的价位]
    撤 to_cancel; 挂 to_place
```

- postOnly 下单：`POST /api/v3/trade/place-order` + `timeInForce: "post_only"`
- SELL postOnly 被拒（如价格已穿）→ **降级市价加仓**
- 下单成功必须记录返回的 `data.orderId`（注意不是 `ordId`）

## 8. 主循环 on_tick（轮询版，间隔 `interval` 秒）

```python
def _tick():
    mark_price = get_price(symbol)        # GET market/tickers, 取 markPrice
    if mark_price <= 0: return            # 行情失败跳过本轮

    with lock:
        _check_fills()                    # ① 成交检测（先于 watchdog！）
        _maker_watchdog(mark_price)       # ② 穿价降级
```

### 8.1 _check_fills — 成交检测（WS on_fill 回调的轮询等价物）

```
查一次挂单列表 GET /api/v3/trade/unfilled-orders → open_ids 集合
对每个本地追踪的 orderId:
    仍在 open_ids       → 还挂着, 跳过（正常情况每 tick 只多这 1 次 API 调用）
    消失了              → GET /api/v3/trade/order-info 查终态:
        SELL filled     → stack_top = avgPrice, opens += 1, refresh_orders("fill_sell")
        BUY  filled     → closes += 1, 从 pending_buys 移除, refresh_orders("fill_buy")
        cancelled       → 清除本地记录, refresh_orders 重挂 (外部撤单恢复)
        partially_filled → 视为仍挂着, 不动
```

> **没有这一步，postOnly 真实成交后状态永远不更新，watchdog 会重复加仓 —— 这是必须实现的，不是可选项。**

### 8.2 _maker_watchdog — 穿价降级

```
价格 >= pending_sell_px × (1 + 0.001)  → 挂单没接住, 市价加仓 (market_add)
价格 <= 最高 BUY target × (1 - 0.001)  → 市价平仓该档 (market_close_specific, reduceOnly)
```

市价降级成功后同样更新 `opens/closes` 并 `refresh_orders`。

## 9. Bitget UTA v3 API 速查（全部实测验证，错一个字段就是一连串报错）

| 操作 | 接口 | 关键参数 |
|---|---|---|
| 下单 | `POST /api/v3/trade/place-order` | `symbol, category, tdMode:"cross", side, orderType, qty, price, clientOid`；postOnly 加 `timeInForce:"post_only"`；平仓加 `reduceOnly:"yes"` |
| 撤单 | `POST /api/v3/trade/cancel-order` | `symbol, category, orderId` |
| 查挂单 | `GET /api/v3/trade/unfilled-orders` | `category, symbol` → `data.list[]` |
| 查订单 | `GET /api/v3/trade/order-info` | `category, symbol, orderId` → `orderStatus: new/partially_filled/filled/cancelled`, `avgPrice` |
| 行情 | `GET /api/v3/market/tickers` | `category, symbol` → `markPrice` / `lastPrice` |
| 合约信息 | `GET /api/v3/market/instruments` | `category, symbol` |
| 持仓 | `GET /api/v3/position/current-position` | `category, symbol` → `total, openPriceAvg, holdSide` |
| 杠杆 | `POST /api/v3/account/set-leverage` | `symbol, category, leverage` |
| 持仓模式 | `POST /api/v3/account/set-hold-mode` | `holdMode: "one_way_mode"` |

**踩坑清单（违反任意一条 = 报错）：**

1. **所有 GET 接口必须带 `category=USDT-FUTURES`**，缺了报 `400172 Parameter verification failed`（公开行情接口也一样）
2. 合约标识字段是 `symbol`，不是 v2/OKX 的 `instId`
3. 数量/价格字段是 `qty` / `price`，不是 `sz` / `px`
4. **one_way_mode 下单不要传 `posSide`**，传了报 `25236 开仓类型不正确`
5. 下单响应是 `data.orderId`，不是 `ordId`
6. ticker 字段是 `markPrice` / `lastPrice`，不是 `markPx` / `lastPx`
7. 签名：`timestamp + METHOD + path + "?" + sortedQuery + body`，HMAC-SHA256 后 base64，header 为 `ACCESS-KEY / ACCESS-SIGN / ACCESS-TIMESTAMP / ACCESS-PASSPHRASE`

## 10. 风控与硬规则

| 规则 | 实现 |
|---|---|
| size 合法性（minTradeNum / 倍数 / 最小名义） | 启动时校验，不过则拒绝启动 |
| 挂单价精度 | 按 `pricePlace` 向下取整后再下单 |
| 平仓必须 `reduceOnly` | BUY 梯队与市价平仓全部带 `reduceOnly:"yes"` |
| 重启防重复开仓 | 起仓前先查持仓，已有空仓则接管（§6 [6a]） |
| 行情失败 | 跳过本轮 tick，watchdog 与成交检测都不跑 |
| 起仓拿不到成交价 | 报错退出，**禁止用任意常数兜底**（曾因兜底 1.0 产生过 0.995 的垃圾挂单） |
| 并发保护 | 所有状态变更在 `threading.RLock` 内 |
| 退出 | SIGINT → 保存 state 后退出（**不撤单**，重启后由 diff 算法接管） |

**关键约束**（与 OKX 版一致）：

- 代码永远只动指定 `symbol` 的仓位，绝不批量平仓 / 绝不动其他币种
- 永远不在有持仓时改杠杆、不切换持仓/保证金模式（启动时的设置是幂等的）
- 状态不一致时倾向于"接管/重挂"而非静默修改持仓

## 11. 文件结构

```
bitget_short_pyramid/
├── strategy.py    # 策略主体: init / run / _tick / _check_fills / _maker_watchdog
│                  #          / _refresh_orders / _adopt_existing_position / 市价降级
├── client.py      # UTA v3 客户端: 签名 + §9 全部接口, dry_run 模式返回假单
├── state.py       # State(dict) + load/save/delete (PKL)
├── config.py      # CLI 参数 + .env (BITGET_API_KEY/SECRET_KEY/PASSPHRASE)
├── precision.py   # Decimal 精度: quantize_price / quantize_size / validate_order_size
├── logger.py      # 每币种独立日志 logs/<SYMBOL>_<ts>.log
└── utils.py
state_short_pyramid_<SYMBOL>.pkl   # 状态文件（每币种隔离）
```

## 12. 启动命令

```bash
# dry-run（不发真实请求，模拟价格波动）
python -m bitget_short_pyramid.strategy dry-run --symbol BGBUSDT --size 4 --grid 0.005

# live（启动时交互式输入 yes 确认）
python -m bitget_short_pyramid.strategy live --symbol BGBUSDT --size 4 --grid 0.005 --interval 5

# 多币种 = 多进程，各开一个窗口
python -m bitget_short_pyramid.strategy live --symbol WLDUSDT --size 100 --grid 0.01
```

`.env`：

```
BITGET_API_KEY=xxx
BITGET_SECRET_KEY=xxx
BITGET_PASSPHRASE=xxx
```

## 13. 已知限制与注意事项

1. **轮询延迟**：成交检测与 watchdog 最坏滞后一个 `interval`（默认 5s）。行情剧烈时穿价降级的实际成交价会比挂单价差；要更快只能上 WS 私有频道（未实现）。
2. **单边大涨没有硬止损**：SELL 成交→栈顶上移→继续挂更高的 SELL，持续累积空仓。这是震荡市策略，趋势市会持续浮亏。
3. **`stack_top` 计数器模型的代价**：不保存每档真实入场价（接管持仓时用均价当栈顶），PnL 统计是近似值；好处是重启接管/对账简单。
4. **市价降级加仓后 `stack_top` 用当前标记价估算**，与真实成交价可能有滑点级偏差。
5. **无 Telegram 通知**：当前只有文件日志（OKX 版的 TG 通知矩阵未移植）。无人值守跑实盘前建议补上。
6. **保证金率风控未实现**：OKX 版的 `mgnRatio < 500% 拒绝加仓` 规则未移植，UTA 下需改用 `GET /api/v3/account/assets` 的账户风险率字段。
7. **MAX_BUYS=60 的挂单数量**：持仓档数大时启动会一次性挂几十个单，注意交易所单合约挂单数上限（BGBUSDT 为 400）。
8. **dry-run 撮合是假的**：dry-run 只验证流程/状态机/精度，不能评估收益。

## 14. 验收清单（实盘前必须全过）

- [ ] dry-run 全流程跑通：起仓 → 挂 SELL+BUY → tick 循环无异常
- [ ] live 小额起仓成功，SELL/BUY postOnly 挂单价格精度正确（4 位小数等）
- [ ] Ctrl+C 中止 → 重启 → state 从 PKL 恢复，refresh 显示 `cancel=0 place=0`（不重复撤挂）
- [ ] 删除 state 文件 + 交易所有持仓 → 重启 → 日志出现"接管已有持仓"，**没有**新开仓
- [ ] 手动在 App 撤掉一个 BUY 挂单 → 下一个 tick 内检测到 cancelled 并自动重挂
- [ ] SELL 挂单真实成交 → stack_top 上移 + 梯队整体重挂（看日志 `[fill] SELL 成交`）
- [ ] BUY 挂单真实成交 → closes+1 + 该档消失（看日志 `[fill] BUY 成交`）
- [ ] 行情接口故意断开（断网 30s）→ tick 跳过不崩溃，恢复后继续
- [ ] 两个币种同时跑 1 小时，state / 日志 / 挂单互不污染

## 15. 反模式（绝对不要做）

- ❌ 用 ticker 价或任意常数当成交价"兜底"（产生过 0.995 垃圾单的事故根源）
- ❌ postOnly 成交靠"价格穿过挂单价"推断（必须查 order-info 终态，否则与 watchdog 重复触发）
- ❌ refresh 时全撤全挂（必须 diff + 容差匹配，否则刷 API 限频且丢队列优先级）
- ❌ 每 tick 对每个挂单逐一查 order-info（先查一次 unfilled-orders 列表，只对消失的单查详情）
- ❌ 平仓单不带 `reduceOnly`（单向模式下会反手开多）
- ❌ one_way_mode 下单传 `posSide`（直接报 25236）
- ❌ 把 `--size` 写死默认值（必须 CLI 强制传入）
- ❌ 多进程共享同一个 state 文件（必须按 symbol 区分）
- ❌ 重启时不查交易所持仓直接市价起仓（重复开仓事故根源）

---

**完。**

实现完成后，请按 §14 的清单逐项验证再上实盘。任何一条验证失败 → 修代码 → 重新跑全清单。
