# Bitget Pyramid Trading — Claude Project Memory

> 本项目是 OKX 金字塔做空策略的 Bitget 移植版。架构相近但**交易所/接口/成交驱动方式不同**：
> Bitget 统一账户(UTA) `/api/v3` 接口、WebSocket 订单频道实时驱动 `on_fill`（OKX 版是纯 REST 轮询）。
> OKX 版细节见 `../okxAPI/CLAUDE.md`，但下方坑表/规则以本文件为准。

## 项目概览

Bitget UTA v3 永续合约金字塔做空工具集：
- `client.py`：`BitgetClient` — REST 签名/请求 + WebSocket 私有订单频道（`ws_connect`）
- `config.py`：CLI 参数 + `.env` 读取（`parse_args` 策略 / `parse_monitor_args` 监控）
- `strategy.py`：`Strategy` 主体（金字塔做空，WS 实时成交 + 60s REST 对账兜底）
- `monitor.py`：`AccountMonitor` 账户监控守护（保证金率/资金费率/挂单形态/权益 CSV）

运行（见 `bitgetReadMe.txt`）：
```
python3 -m strategy --symbol IDUSDT --sz 200 --grid 0.01
python3 monitor.py
```

## 安全约束（默认强制，除非用户授权例外）

1. **不主动改杠杆为非 3x** — `_check_account_config` 启动时无条件设 3x（`set_leverage`）
2. **持仓模式固定 one_way_mode（单向）** — `set_hold_mode("one_way_mode")`，UTA 单向不传 posSide
3. **保证金模式固定 cross（全仓）** — `place_order` 写死 `tdMode=cross`
4. **只操作本 `--symbol`** — 不动其他合约持仓
5. **不提交 `.env`** — 含 BITGET_API_KEY / SECRET / PASSPHRASE / TG token（`.gitignore` 已含）
6. **全部 GTC limit 单**（市价仅用于默认起仓路径）

## Bitget UTA v3 API 关键格式（踩过的坑，见 memory `bitget-uta-v3-api-format`）

| 项 | 要求 |
|----|------|
| `category` | **GET/POST 都必带** `"USDT-FUTURES"`（缺失即报错） |
| 下单字段 | `qty`/`price`/`clientOid`/`orderId`（**不是** OKX 的 sz/px/clOrdId/ordId） |
| 单向持仓 | `holdMode=one_way_mode`，下单**不传** posSide/tradeSide |
| 签名 | `Base64(HMAC_SHA256(secret, ts + METHOD + path + "?"+query + body))`，GET 的 query 须按 key 升序 |
| WS 登录 | `op=login`，sign = `Base64(HMAC_SHA256(secret, ts+"GET"+"/user/verify"))`，ts 为**秒** |
| 成功码 | `code == "00000"` |
| 账户接口 | UTA 禁用 v2 经典接口（`40085`），必须用 `/api/v3/account/assets` |
| reduceOnly | 字符串 `"yes"`（非布尔） |

`client.py` 把 v3 字段归一化为 OKX/v2 兼容名（`holdSide`/`openPriceAvg`/`unrealizedPL`/`total`/`minTradeNum` 等），
策略层沿用 OKX 命名。改 `client.py` 时注意保持这层映射。

## 账户权益读取（联合保证金，反复踩坑）

`get_account` 账户总权益**按数值择优**：`accountEquity → usdtEquity → effEquity`，
**不能用 `or` 回退**（"0" 是合法真值，会误判为缺失）。equity=0 时打 WARN 并 dump 所有字段。
保证金率 = `accountEquity / mmr`（账户级维持保证金；`mmr=0` 视为无持仓）。

## 风控规则（单一入口 `_can_open_sell`，strategy.py）

起仓 + 加 SELL 共用同一函数，无开关无分支。两条规则：

| # | 规则 | 条件 |
|---|------|------|
| 1 | 保证金率 ≥ 500% | `mmr>0 且 accountEquity/mmr < 5.0` → 拒 |
| 2 | 加仓后总名义 ≤ 上限 | `(现有持仓张数 + 本单) × px > max_notional_usdt`（默认 10000）→ 拒 |

- 快照走 **REST**（`get_account` + `get_position`），不读 WS 缓存
- 查询异常 → **放行**（fail-open，避免临时网络抖动卡住网格）
- 起仓被拒 → `sys.exit(1)` 停策略；加 SELL 被拒 → WARN+TG 跳过不挂，下次成交/对账重试

## 成交驱动（WS 实时 + REST 对账双保险）

- **WebSocket 订单频道**（`ws_connect`）后台线程实时推 `orders` → `_on_ws_message` 归一化 → `on_fill`
  - 30s 心跳 ping；连接失败自动降级，仅靠对账兜底
- **60s 定时对账 `_reconcile`**（`RECONCILE_SEC`）：①补 WS 漏推（`_check_missed_fills` 用 REST 查 filled 补发虚拟 on_fill）②查交易所实际持仓与本地 opens-closes 对比，差 ≥1 张自动修复（交易所为准）③零头（`okx_sz % POSITION_SZ`）→ WARN 要求人工平仓
  - 防 race：最近 5s 内有成交/刷新则跳过本次对账
- **`on_fill` 用 `RLock` 串行化**，防 WS + 对账并发重复挂单

## 网格运行逻辑（`_refresh_orders`）

- **SELL 唯一**：永远只 1 个 @ `stack_top × (1+grid)`，过 `_can_open_sell` 风控；
  挂单成功记 `pending_sell_ord_id`/`pending_sell_px`；交易所拒单（限价带等）**不降级不退出**，仅告警等回落
- **BUY 梯队**：depth = `min(持仓张数, MAX_BUYS=60)`，几何价位 `stack_top × (1-grid)^i`，
  `reduceOnly` 固定配对单（`entry_px` 记录后永不改动）
- **SELL 成交** → `stack_top=成交价`, opens++, refresh
- **BUY 成交** → closes++，一轮完成（opens==closes>0）则 `_cycle_complete` 撤所有单 + `sys.exit(0)`
- **BUY 被系统撤** → 退避计数：同价位 120s 窗口被撤 ≥3 次 → 退避 180s 不挂（`_note_buy_cancel`/`_in_backoff`），避免无脑重挂打限频
- **`_place_sell` 强制单例**：检测到已有 SELL → `sys.exit(1)`（防双 SELL bug）

## 起仓三选一（优先级 adopt > initial > 默认市价）

| CLI 参数 | 动作 |
|---|---|
| （默认无参数） | 市价 SELL 1 单，stack_top=市价，opens=1 |
| `--initial-sell-px <px>` | 限价 SELL 1 单，stack_top=px，opens=0 等成交 |
| `--adopt-sell-px <px>` | 基准价：stack_top=px，挂 SELL @ px×(1+grid)，opens=0 |

启动时优先 `_adopt_position`：若交易所已有 short 持仓则接管（opens=`total/POSITION_SZ`），否则才起仓。

## 启动流程（`init`）

```
① get_contracts（合约元信息）→ ② _check_account_config（合约有效 + 单向 + 3x 杠杆，失败 exit 1）
→ ③ _cancel_stale_pending_on_startup（清旧挂单）→ ④ _adopt_position 接管 或 _open 起仓
→ ⑤ _refresh_orders 挂网格 → ⑥ 启动 WS 线程 + 进入 60s 对账主循环
```

每次启动 `_load_state` 都**删旧 pkl 并备份为 `.bak`**（不复用旧状态，以交易所实际持仓为准）。

## 状态文件

`state_short_pyramid_<symbol>.pkl`（+ `.pkl.bak` 备份）：`stack_top`/`opens`/`closes`/`pending_sell_ord_id`/`pending_sell_px`/`pending_buys{oid:{entry_px,target_px}}`。
保存用 fsync + rename 备份保护。

## 监控器（monitor.py，独立进程）

REST 每 60s 全量扫描（无 WS）：
- **保证金率分级**：info 1000% / warn 700% / critical 500%(=策略停加仓阈值) / emergency 300%
- **资金费率**：空头在负费率被吃利息，≥0.1%/8h 警告、≥0.2% 严重（节流 1h）
- **挂单形态探针**：每个空头持仓应「恰好 1 SELL + ≥1 BUY」，连续 3 次 tick 异常才告警（策略存活检测，过滤 refresh 撤挂空窗）。只检查有 `state_short_pyramid_<sym>.pkl` 的合约（`_managed_instruments`），跳过手动/其它策略持仓避免误报；扫描失败回退全量
- **权益曲线** 写 `equity_log.csv`；每小时心跳
- 告警节流：保证金率 10min / 形态 30min / 资金费率 1h

## 通知格式

Telegram `_notify(msg, level)` 带等级前缀：`ℹ️ [INFO]` / `⚠️ [WARN]` / `🚨 [CRITICAL]`。
TG 消息用 HTML parse_mode — 文本里用 `≤`/`&lt;` 勿用裸 `<`（会被当标签，400 丢失）。

## 常见坑

| 现象 | 根因 | 修复 |
|------|------|------|
| `[40085]` 账户接口报错 | UTA 禁用 v2 经典接口 | 用 `/api/v3/account/assets`（已） |
| 账户权益读成 0 / 风控误判 | 联合保证金某字段为 0，`or` 回退误判 | `accountEquity→usdtEquity→effEquity` 按数值择优，非 `or`（已） |
| GET 报参数错 | 漏带 `category` | GET/POST 都必带 `USDT-FUTURES`（见 memory） |
| 下单字段不识别 | 误用 OKX 命名 sz/px/clOrdId | 用 qty/price/clientOid/orderId（已） |
| 单向持仓下单被拒 | 传了 posSide/tradeSide | one_way_mode 不传方向字段（已） |
| 撤单报 `25204` | 订单已成交/已撤 | `_safe_cancel` 视为成功（已） |
| BUY 被系统反复撤→限频 | 同价位无脑重挂 | 120s 内撤 ≥3 次→退避 180s（已） |
| WS 漏推成交不动 | 推送丢失 | 60s 对账 `_check_missed_fills` REST 补发虚拟 on_fill（已） |
| 部分成交零头进 stack | accFillSz 非整张 | 检测 `% POSITION_SZ` → WARN 要求人工平仓，不进 stack（已） |
| WS+对账并发重复挂单 | 两路同时改状态 | `on_fill`/`_refresh_orders` 用 `RLock` 串行（已） |
| TG 报警 400 丢失 | 裸 `<` 被当 HTML 标签 | 用 `≤`/`&lt;`（已） |
| monitor 对手动/其它策略持仓误报形态异常 | 探针假设每个持仓都是金字塔网格 | 只检查有 `state_short_pyramid_<sym>.pkl` 的合约（`_managed_instruments`，已） |

## 退出码

| Code | 含义 |
|------|------|
| 0 | 一轮做空完成（opens==closes）→ 撤所有挂单正常退出 |
| 1 | 启动/运行失败：账户配置不过、无法取市价、起仓被风控拒、撤单失败、检测到双 SELL |
