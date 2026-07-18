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
- `notifier.py`：`TelegramNotifier` — TG 推送（节流去重 + 连续失败本地自救告警 + 直连绕代理）

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
| **下单响应 orderId** | ⚠️ reduceOnly（BUY 平仓）单 place-order 成功（code 00000）却常返回 `data.orderId=**None**`，只给 `clientOid` → 必须用 clientOid 反查 order-info 回填真 orderId（`client._resolve_ord_id_by_client_oid`），否则整个下游追踪全断 |

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

- 快照走 **REST**（`get_account` + `get_position`），不读 WS 缓存；`get_account(use_cache=True)` 带 3s TTL 缓存（保证金率秒级不剧变，减轻多币共用账户对 `/account/assets` 的限频压力；只缓存成功结果，失败/对账/监控仍实时）
- 查询异常 → **放行**（fail-open，避免临时网络抖动卡住网格）
- 起仓被拒 → `sys.exit(1)` 停策略；加 SELL 被拒 → WARN+TG 跳过不挂，下次成交/对账重试

## 成交驱动（WS 实时 + REST 对账双保险）

- **WebSocket 订单频道**（`ws_connect`）后台线程实时推 `orders` → `_on_ws_message` 归一化 → `on_fill`
  - **带自动重连 + 僵尸检测**（`client.py`）：按官方规则「收不到 pong 即重连」，每 20s 发 `ping`，任何消息/pong 刷新存活时间戳，超 50s 无消息 → 判半开/僵尸连接主动关闭重连；关库自带协议 ping（`ping_interval=None`）避免与应用层心跳互斥；重连退避 1→30s
  - **重连后回调 `on_reconnect`** → 置 `last_reconcile_ts=0` 强制立即对账，补断连空窗期漏掉的成交
  - `should_stop` 优雅退出重连循环；库未装/彻底失败才降级为纯对账兜底
- **60s 定时对账 `_reconcile`**（`RECONCILE_SEC`）：①补 WS 漏推（`_check_missed_fills` 用 REST 查 filled 补发虚拟 on_fill）②查交易所实际持仓与本地 opens-closes 对比，差 ≥1 张自动修复（交易所为准）③零头（`okx_sz % POSITION_SZ`）→ WARN 要求人工平仓
  - **单一共享快照**：`_do_reconcile` 顶部一次性相邻取 `open_orders`+`positions`，`open_ids` 传给 `_check_missed_fills`、`exch_sz` 从同一持仓快照算，避免「订单列表 vs 持仓」两次异步 REST 时间差致同一笔被双算（FUTUUSDT 2026-06-24，见坑表/memory `bitget-reconcile-double-count`）
  - **diff 修复摘 oid**：diff<0 时 `closes+=n` 前把不在 `open_ids` 的 BUY 从 `pending_buys` 摘掉，迟到的 WS 推送被 `on_fill` 幂等丢弃。原则：计数只走 missed_fill→on_fill 一条 oid 路径，diff 仅兜底真正外部漂移
  - **diff 修复平仓必须下移 stack_top**：diff 路径拿不到成交价，用当前市价重锚（仅在市价 < stack_top 时下移，不上抬）。否则 oid 丢失时全部平仓走 diff、stack_top 锚死高位、SELL 不跟行情=网格冻结亏钱（REDUSDT 2026-07-05）。OKX 对应：`_handle_missed_fill` 的 `stack_top=fill_px`
  - 防 race 两层：①**guard 在锁内判定**（`_reconcile` 把 5s 内有成交`last_fill_ts`/刷新`last_refresh_ts`则跳过的判断放进 `with RLock`，避免锁外通过 guard→等锁→拿锁时成交已发生仍跑对账的 TOCTOU）②对账主体 `_do_reconcile` **全程持 `RLock`**，与 `on_fill` 串行，防 closes/opens 读改写竞态多算
  - **时间戳分离**：`last_fill_ts`（成交，防 race 用）vs `last_reconcile_ts`（主循环 60s 调度用），不可复用——复用会让成交把对账调度顶掉、race guard 也失效
- **`on_fill` / `_do_reconcile` 共用 `RLock` 串行化**，防 WS + 对账并发重复挂单/重复计数
- **`_cycle_complete` 收尾 = 撤单 + 扫尾平残留 + 退出（对齐 OKX）**：一轮结束（opens==closes）即撤所有挂单，再用 `_sweep_residual_position` 以 reduceOnly 市价把交易所仍残留的裸头（竞态多算 closes 残留的 ~1 单 / partial 漏账等）**平掉**后 exit(0)。**不再**「实盘≠0 就重建网格续跑」并反复弹「平仓不彻底：实盘仍有 ~1 单」告警（ACEUSDT 2026-07-19）——真正的收尾语义是 OKX 那样清掉残留收工。灰尘（对齐后 < 最小下单额）才平不掉，由 `_sweep_residual_position` 告警留人工
- **退出前扫尾 `_sweep_residual_position`**：正常退出分支撤光挂单后，再查一次真实持仓，有裸头（partial fill 漏账 / 撤单空窗刚成交）用 **reduceOnly 市价单**平掉（只减不增，已平不反手）；**灰尘**（数量取整为 0 / 名义 < `minTradeUSDT`）平不掉必被拒 → 告警留人工，不硬下；失败也告警不吞异常

## 网格运行逻辑（`_refresh_orders`）

- **SELL 唯一**：永远只 1 个 @ `stack_top × (1+grid)`，过 `_can_open_sell` 风控；
  挂单成功记 `pending_sell_ord_id`/`pending_sell_px`；交易所拒单（限价带等）**不降级不退出**，仅告警等回落
- **BUY 梯队**：depth = `min(持仓张数, MAX_BUYS=60)`，几何价位 `stack_top × (1-grid)^i`，
  `reduceOnly` 固定配对单（`entry_px` 记录后永不改动）
- **SELL 成交** → `stack_top=成交价`, opens++, refresh
- **BUY 成交** → `stack_top=成交价`（下移，使加仓 SELL 重新锚定 `成交价×(1+grid)` 跟随行情下移，与 OKX `_handle_close_filled` 一致；漏此步会让 SELL 冻结在最高价、网格脱离行情而亏钱），closes++，PnL 用该 BUY 挂单时记录的 `entry_px`（非 stack_top）；一轮完成（opens==closes>0）则 `_cycle_complete` 撤所有单 + `sys.exit(0)`
- **BUY 被系统撤** → 退避计数：同价位 120s 窗口被撤 ≥3 次 → 退避 180s 不挂（`_note_buy_cancel`/`_in_backoff`），避免无脑重挂打限频
- **`_place_sell` 强制单例**：检测到已有 pending SELL → **先撤旧再挂新**（对齐 OKX `_refresh_sell_only_locked`，撤旧→清引用→挂新）。**不再 `sys.exit(1)` 自杀**——旧设计任何补挂/竞态重入都会误报「多个 SELL」杀进程留裸仓（VELODROMEUSDT 2026-07-04）

## 起仓三选一（优先级 adopt > initial > 默认市价）

| CLI 参数 | 动作 |
|---|---|
| （默认无参数） | **post-only 盘口追价** SELL 1 单（`_open_maker_chase`，挂卖一 `ask1Price` 做 maker，未成交撤单按最新盘口追价直到成交），stack_top=成交均价，opens=1。移植 aster；省吃单点差/拿返佣。Bitget post_only 会 taker 时下单直接被拒(code≠00000)→追价（非币安 EXPIRED 语义） |
| `--limit <px>` | 限价 SELL 1 单，stack_top=px，opens=0 等成交（旧名 `--initial-sell-px`） |
| `--adopt <px>` | 基准价：stack_top=px，挂 SELL @ px×(1+grid)，opens=0（旧名 `--adopt-sell-px`） |
| `--adopt`（裸写） | 自动基准价：取该 symbol 账户最后一笔成交价（`get_last_fill_price`，无则当前市价兜底）写回 `adopt_sell_px`，复用上面基准价逻辑。argparse `nargs='?' const='AUTO'`，`Config.adopt_auto` 标志，`init` 里 `_resolve_auto_adopt_px` 解析 |

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

## 通知格式（`notifier.py`）

统一走 `TelegramNotifier`（strategy/monitor 共用，参考 okx 版移植）：
- `send(msg, level, prefix=True, dedup_key, throttle_sec)`：`prefix=True` 自动加 `ℹ️ <b>[INFO]</b>` 等等级前缀；monitor 消息自带格式故 `prefix=False`
- **连续失败自救**：`send` 连续失败 ≥3 次 → 蜂鸣(Windows) + 写 `EMERGENCY_ALERT.txt`，解决"代理/VPN 挂了 TG 也没提醒"盲区
- **直连绕代理**：`proxies={http:None, https:None}` 屏蔽系统/env 代理；timeout=(5,12)
- strategy 的 `_notify` / monitor 的 `_send_msg` 都已收口到此模块
- TG 用 HTML parse_mode — 文本里用 `≤`/`&lt;` 勿用裸 `<`（会被当标签，400 丢失）

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
| **频繁"对账漂移"、成交全靠对账补**（TNSRUSDT 2026-06-21） | 旧 `ws_connect` 只发 ping 不检测 pong，TCP 半开/被 NAT 静默掐断时 `async for` 永久阻塞、不报错不重连 → 僵尸连接静默丢消息（官方文档：收不到 pong 即应重连） | `ws_connect` 加应用层 ping + pong/消息超时判僵尸 → 主动重连（退避）+ 重连后强制对账补空窗（已） |
| 部分成交零头进 stack | accFillSz 非整张 | 检测 `% POSITION_SZ` → WARN 要求人工平仓，不进 stack（已） |
| WS+对账并发重复挂单 | 两路同时改状态 | `on_fill`/`_refresh_orders` 用 `RLock` 串行（已） |
| TG 报警 400 丢失 | 裸 `<` 被当 HTML 标签 | 用 `≤`/`&lt;`（已） |
| monitor 对手动/其它策略持仓误报形态异常 | 探针假设每个持仓都是金字塔网格 | 只检查有 `state_short_pyramid_<sym>.pkl` 的合约（`_managed_instruments`，已） |
| WS 漏推兜底从不生效 | `_check_missed_fills` 用错字段 `orderStatus`/`avgPrice`（Bitget v3 是 `status`/`priceAvg`） | 兼容两种命名 `info.get("status") or info.get("orderStatus")`（已） |
| 仓位已平却空转、甚至凭空重挂 SELL | `_reconcile` 修复 closes 后漏查 `opens==closes → _cycle_complete()` | 对账平到 opens==closes>0 即撤单 exit(0)，与 WS 平仓路径一致（已） |
| 0 持仓凭空挂 SELL 重新开空 | `_ensure_orders_complete` 无视 n_pos 补 SELL | n_pos==0 直接 return 不补单（已） |
| **`--limit` 起仓后初始 SELL 被秒撤、策略空挂不触发**（TNSRUSDT 2026-06-21） | `_open` 限价模式把 SELL 挂在 `stack_top` 本身、`opens=0`，随后 `init` 调 `_refresh_orders` 按 `stack_top×(1+grid)` 重算，价格不符→撤掉初始 SELL，又因 `n_pos==0` 不补 | `_refresh_orders` 加等待态守卫：`n_pos==0 且有 pending_sell` 时原样不动，等成交后再走网格（已）。注：`--adopt` 因 SELL 挂在 `基准×(1+grid)` 与 refresh 期望一致，本无此问题 |
| **一轮"完成"后留下无网格裸空单**（TNSRUSDT 2026-06-21） | ①对账与 WS `on_fill` 并发改 closes（`_reconcile` 没持锁）多算 1 ②`last_reconcile_ts` 被成交与调度复用，race guard 失效 ③`_cycle_complete` 只看 `opens==closes` 不核实盘→提前退出留裸仓 | ①`_do_reconcile` 全程持 `RLock` 与 on_fill 串行 ②拆出 `last_fill_ts` 专记成交 ③`_cycle_complete` 退出前 sweep 实盘残留（reduceOnly 市价平掉 ~1 单等裸头）再退出，对齐 OKX（均已；2026-07-19 由「重建网格续跑」改为「扫尾平掉后收工」，见下条 ACEUSDT）|
| **【第一性根因】reduceOnly BUY 平仓全走对账、pending_buys 记不上、疯狂重挂、stack_top 冻结**（BGBUSDT 2026-07-10 诊断确认） | Bitget place-order 对 reduceOnly BUY 单下单成功（code 00000）却返回 `data.orderId=None`（只给 clientOid），`_place_buy` 提取不到 orderId → pending_buys 记不上 → BUY 成交 WS 推送被 on_fill 当「非追踪订单」丢弃 → 平仓只能靠对账 diff（历史上还不下移 stack_top）→ 账目滞后 + 网格冻结 + 超量重挂。**之前多轮修复（stack_top 下移/对账四防线/多SELL不自杀）都是在治这个根因的下游症状** | client.place_order 下单成功但 orderId 空时，用 clientOid 反查 order-info 回填真 orderId（`_resolve_ord_id_by_client_oid`，带 4 次重试）→ pending_buys 正常记录、WS 平仓推送能匹配，下游全部自愈（已） |
| **WS 长断→本地账目滞后→按虚高 n_pos 疯狂重挂 reduceOnly 单打限频（1 小时暴走）**（MIRAUSDT 2026-07-05） | WS 僵尸断连 2h（实盘已平近 0，本地仍记 6~7 单）+ REST 兜底失效（get_open_orders 40008 时间戳过期、order-info 429），对账缺 OKX 四道防线：①`_check_missed_fills` 只认 filled、canceled 不清引用→pending_buys 堆僵尸 ②无「持仓≈0 收尾」闸门→继续按本地补挂 ③reduceOnly 超量拒单只 warning 不弹栈 ④退避只挂 WS 回调、僵尸期收不到 canceled→退避永不触发 | 照 OKX 补齐：①canceled→清本地引用（429/查不到返回空 dict 时不动防误删）②`_do_reconcile` 加 `exch_sz<eps 且 local>0 → _cycle_complete()`（对齐 `_detect_and_handle_manual_close`，防暴走核心）③`_place_buy` 命中 `_is_position_insufficient` → 弹栈 closes+1（对齐 `_pop_stack_entry`）④对账 canceled 路径也走 `_note_buy_cancel` 退避（均已，见 memory `bitget-ws-longbreak-runaway`） |
| **加仓/补挂后误报「多个 SELL」立即 exit(1)、留裸仓**（VELODROMEUSDT 2026-07-04） | `_place_sell` 的「检测到已有 SELL → `sys.exit(1)`」是**过度防御设计**：它假设进来时 `pending_sell_ord_id` 必为 None，但 `_ensure_orders_complete`(对账补挂)等路径不清 id 就调它，竞态重入即误判双 SELL 杀进程 → 无网格裸空单 | 去掉 `sys.exit(1)` 自杀，改为**先撤旧 SELL→清引用→挂新**（对齐 OKX `_refresh_sell_only_locked`，OKX 从不因多 SELL 退出）（已） |
| **平仓成交后误报「多个 SELL」立即 exit(1)**（BGBUSDT 2026-07-02） | BUY 平仓令 stack_top 下移→SELL 目标价变→`_refresh_orders` 撤旧 SELL 后**没清 `pending_sell_ord_id`** 就调 `_place_sell`，后者读到旧 id 判双 SELL 而退出（stack_top 下移修复后才暴露此潜伏 bug） | 撤 SELL 成功后立即 `pending_sell_ord_id/px=None` 再挂新单，对齐 OKX `_refresh_sell_only_locked`（已） |
| **反复弹「平仓不彻底：实盘仍有 ~1 单，已按实盘修正继续维护网格」告警**（ACEUSDT 2026-07-19） | `_cycle_complete` 旧设计发现实盘 ≠ 0（竞态多算 closes 残留 ~1 单）时不退出，改为「重建网格续跑」并反复告警——但收尾语义应是把残留清掉收工，而非无限续跑 | 对齐 OKX：一轮结束一律 `_sweep_residual_position`（reduceOnly 市价平掉残留裸头）→ 清状态 → exit(0)，去掉「重建网格」分支；灰尘才留人工（已） |
| **同一笔平仓被对账 diff 与 WS 各记一次 closes，下轮反手 opens+1 圆账**（FUTUUSDT 2026-06-24） | ①`_check_missed_fills` 自取 `get_open_orders`、diff 又自取 `get_position`，两次异步 REST 间「持仓已减、挂单列表仍列着该 BUY」有时间差→漏推检查跳过、diff 记 closes 且不摘 oid ②diff 修复不从 `pending_buys` 摘已成交 oid→迟到 WS 推送再记一次 ③race guard 在锁外判 `last_fill_ts`，TOCTOU 失效 | 照 OKX 版 `_do_reconcile` 结构：①顶部一次性相邻取 open_orders+positions **共享快照**，`open_ids` 传给 `_check_missed_fills` ②diff<0 时 `closes+=n` 前把不在 `open_ids` 的 BUY 从 `pending_buys` 摘掉→`on_fill` 幂等丢弃迟到推送 ③guard 移进 `with self._lock:` 内（均已）。原则：计数只走 missed_fill→on_fill 一条 oid 路径，diff 仅兜底真正外部漂移 |

## 退出码

| Code | 含义 |
|------|------|
| 0 | 一轮做空完成（opens==closes）→ 撤所有挂单正常退出 |
| 1 | 启动/运行失败：账户配置不过、无法取市价、起仓被风控拒、撤单失败、检测到双 SELL |
