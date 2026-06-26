"""Bitget 金字塔做空策略"""
import sys
import math
import time
import signal
import threading
import asyncio
import pickle
import logging
import os
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime

from config import parse_args, Config
from client import BitgetClient, _gen_cl_ord_id
from notifier import TelegramNotifier


# ============ 日志模块 ============
def _setup_logger(symbol: str, log_dir: str = "logs") -> logging.Logger:
    """创建日志记录器"""
    os.makedirs(log_dir, exist_ok=True)
    logger_name = f"sp_{symbol}"
    logger = logging.getLogger(logger_name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{symbol}_{timestamp}.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# ============ 精度处理模块 ============
def _to_decimal(value) -> Decimal:
    """安全转换为 Decimal"""
    if value is None or value == "" or value == "0":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _quantize_price(price: Decimal, price_place: int) -> Decimal:
    """向下处理价格小数位"""
    if price_place < 0:
        price_place = 0
    fmt = Decimal(10) ** -price_place
    return price.quantize(fmt, rounding=ROUND_DOWN)


def _validate_order_size(size: Decimal, mark_price: Decimal, contract_info: dict) -> tuple:
    """验证下单数量是否满足合约要求"""
    min_trade_num = _to_decimal(contract_info.get("minTradeNum", 0))
    if size < min_trade_num:
        return False, f"size {size} < minTradeNum {min_trade_num}"
    return True, ""


# ============ 状态管理模块 ============
class State(dict):
    """状态字典"""
    def __init__(self, symbol: str):
        super().__init__()
        self["symbol"] = symbol
        self["stack_top"] = 0.0
        self["opens"] = 0
        self["closes"] = 0
        self["pending_sell_ord_id"] = None
        self["pending_sell_px"] = None
        self["pending_buys"] = {}
        self["last_action_time"] = 0.0


def _state_path(symbol: str) -> Path:
    """返回状态文件路径"""
    return Path(f"state_short_pyramid_{symbol}.pkl")


def _state_backup_path(symbol: str) -> Path:
    """返回状态备份路径"""
    return Path(f"state_short_pyramid_{symbol}.pkl.bak")


def _load_state(symbol: str) -> State:
    """创建新状态（每次启动都删除旧 pkl，保留备份）"""
    path = _state_path(symbol)
    backup_path = _state_backup_path(symbol)

    # 保存旧状态作为备份
    if path.exists():
        try:
            path.rename(backup_path)
        except Exception:
            path.unlink()

    return State(symbol)


def _save_state(state: State) -> None:
    """保存状态文件（带备份）"""
    path = _state_path(state["symbol"])
    backup_path = _state_backup_path(state["symbol"])

    try:
        # 如果新文件已存在，先备份
        if path.exists():
            try:
                path.rename(backup_path)
            except Exception:
                pass

        # 写入新状态
        with open(path, "wb") as f:
            pickle.dump(dict(state), f)
            f.flush()
            os.fsync(f.fileno())

    except Exception as e:
        # 写入失败时尝试恢复备份
        if backup_path.exists():
            try:
                backup_path.rename(path)
            except Exception:
                pass
        raise Exception(f"状态保存失败: {e}")


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = _setup_logger(cfg.symbol)
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log)
        self.notifier = TelegramNotifier(cfg.tg_bot_token, cfg.tg_chat_id, self.log)
        self.state: State = _load_state(cfg.symbol)
        self.GRID_PCT = cfg.grid_pct
        self.POSITION_SZ = cfg.size
        self.MAX_BUYS = 60
        self.RECONCILE_SEC = 60
        # BUY 重挂退避：同价位窗口内被系统撤 ≥N 次 → 退避一段时间不挂
        self.BUY_CANCEL_BACKOFF_N = 3
        self.BUY_CANCEL_WINDOW_SEC = 120
        self.BUY_CANCEL_BACKOFF_SEC = 180
        self._buy_cancel_hits = {}   # px_key -> [被撤时间戳]
        self._buy_backoff = {}       # px_key -> 退避截止时间
        self.contract_info: dict = {}
        self.last_reconcile_ts = 0.0   # 主循环对账调度时间戳（每 60s）
        self.last_fill_ts = 0.0        # 最近一次成交时间戳（对账防 race 专用，与调度分开）
        self.last_refresh_ts = 0.0
        self._lock = threading.RLock()
        self._running = True
        self._error_state = {}
        signal.signal(signal.SIGINT, self._handle_exit)

    def _handle_exit(self, sig, frame):
        """优雅退出（SIGINT 处理）"""
        self.log.info("收到中断信号，正在优雅退出...")
        self._running = False
        self._notify("策略已手动停止")

    def _notify(self, msg: str, level: str = "INFO",
                dedup_key: str = None, throttle_sec: int = None):
        """发送通知（自动加 symbol 前缀，便于多策略共用一个 TG 时区分 token；
        等级前缀/失败检测/紧急自救由 notifier 负责）。
        dedup_key+throttle_sec：同 key 在冷却期内只发一次，期间压制次数累计到下条，
        用于会重复触发的告警（如每 60s 对账的零头告警）防刷屏。"""
        self.notifier.send(f"{self.cfg.symbol} | {msg}", level=level,
                           dedup_key=dedup_key, throttle_sec=throttle_sec)

    def _n_and_remainder(self, exch_size: float):
        """返回 (整数张数 N, 零头 remainder)。对齐 OKX 版 `_n_and_remainder`。
        关键：浮点除法 0.09/0.01=8.999... 被 floor 成 8 是个长期 bug。
        若 raw 非常接近整数（|raw-round(raw)|<1e-6）视为整除，否则 floor，多出部分为零头。"""
        if self.POSITION_SZ <= 0:
            return (0, exch_size)
        raw = exch_size / self.POSITION_SZ
        rounded = round(raw)
        if abs(raw - rounded) < 1e-6 and rounded >= 0:
            n = int(rounded)
        else:
            n = max(0, math.floor(raw))
        remainder = exch_size - n * self.POSITION_SZ
        if abs(remainder) < 1e-9:
            remainder = 0.0
        return (n, remainder)

    def _save(self):
        """保存状态"""
        _save_state(self.state)

    def _safe_cancel(self, ord_id: str) -> bool:
        """撤单（订单不存在视为成功，其他失败才返回 False）"""
        try:
            resp = self.client.cancel(self.cfg.symbol, ord_id)
            if resp and resp.get("code") != "00000":
                # 订单不存在（25204）视为成功（已被成交或撤销）
                if resp.get("code") == "25204":
                    return True
                self.log.warning(f"撤单失败 {ord_id}: {resp.get('msg')}")
                return False
            return True
        except Exception as e:
            self.log.warning(f"撤单异常 {ord_id}: {e}")
            return False

    def _check_account_config(self) -> bool:
        """账户配置检查（Bitget UTA）"""
        try:
            # ① 检查合约有效性
            if not self.contract_info or self.contract_info.get("symbolStatus") != "normal":
                self.log.error(f"合约无效: {self.contract_info.get('symbolStatus')}")
                return False

            # ② 持仓模式必须为 one_way_mode（单向持仓）
            resp = self.client.set_hold_mode("one_way_mode")
            if resp.get("code") != "00000":
                self.log.error(f"设置单向持仓失败: {resp.get('msg')}")
                return False

            # ③ 设置杠杆 3x（无论是否有持仓都设置）
            resp = self.client.set_leverage(self.cfg.symbol, 3)
            if resp.get("code") != "00000":
                self.log.error(f"设杠杆失败: {resp.get('msg')}")
                return False

            return True

        except Exception as e:
            self.log.error(f"账户检查异常: {e}")
            return False

    def _cancel_stale_pending_on_startup(self):
        """阶段 3: 清理启动前的挂单"""
        try:
            orders = self.client.get_open_orders(self.cfg.symbol)
            for o in orders:
                self._safe_cancel(o.get("orderId"))
            if orders:
                self.log.info(f"清理 {len(orders)} 个旧挂单")
        except Exception as e:
            self.log.warning(f"清理挂单异常: {e}")

    def init(self):
        """初始化"""
        self.log.info(f"=== 初始化 {self.cfg.symbol} [LIVE] ===")

        self.contract_info = self.client.get_contracts(self.cfg.symbol) or {}

        if not self._check_account_config():
            sys.exit(1)
        self._cancel_stale_pending_on_startup()

        # 接管或起仓
        if not self._adopt_position():
            self._open()
            if self.cfg.adopt_sell_px > 0:
                open_mode = "基准"
            elif self.cfg.initial_sell_px > 0:
                open_mode = "限价"
            else:
                open_mode = "市价"
        else:
            open_mode = "接管"

        # 挂网格
        self._refresh_orders()

        self._notify(
            f"策略启动 | 数量 {self.POSITION_SZ} | "
            f"网格 {self.GRID_PCT*100:.1f}% | 开仓 {open_mode}"
        )
        self._save()

    def _adopt_position(self) -> bool:
        """接管持仓"""
        try:
            for p in self.client.get_position(self.cfg.symbol).get("data", []):
                if p.get("holdSide") == "short":
                    total = float(p.get("total") or 0)
                    avg = float(p.get("openPriceAvg") or 0)
                    if total > 0 and avg > 0:
                        self.state["stack_top"] = avg
                        self.state["opens"] = max(1, int(total / self.POSITION_SZ))
                        self.state["closes"] = 0
                        self.log.info(f"接管持仓 {total}张")
                        return True
        except:
            pass
        return False

    def _open(self):
        """起仓（三种模式，与加仓共用 _can_open_sell；起仓被拒 → 停策略）"""
        if self.cfg.adopt_sell_px > 0:
            base_px = self.cfg.adopt_sell_px
            sell_px = self._round_px(base_px * (1 + self.GRID_PCT))
            self.state["stack_top"] = base_px
        elif self.cfg.initial_sell_px > 0:
            sell_px = self.cfg.initial_sell_px
            self.state["stack_top"] = sell_px
        else:
            # 市价起仓：先取价过风控，再下单
            px = self.client.get_price(self.cfg.symbol)
            if px <= 0:
                self.log.error("无法获取市价")
                sys.exit(1)
            if not self._can_open_sell(px):
                self.log.error("起仓被风控拒，策略停止")
                sys.exit(1)

            resp = self.client.place_order(self.cfg.symbol, "sell", self.POSITION_SZ, 0.0, order_type="market")
            if resp.get("code") != "00000":
                self.log.error(f"起仓失败: {resp.get('msg')}")
                sys.exit(1)

            self.state["stack_top"] = px
            self.state["opens"] = 1
            self._save()
            return

        # 限价/基准模式：挂初始 SELL 前过风控
        if not self._can_open_sell(sell_px):
            self.log.error("起仓被风控拒，策略停止")
            sys.exit(1)

        resp = self.client.place_order(self.cfg.symbol, "sell", self.POSITION_SZ, sell_px,
                                       order_type="limit", cl_ord_id=_gen_cl_ord_id("INIT"))
        if resp.get("code") != "00000":
            self.log.error(f"起仓失败: {resp.get('msg')}")
            sys.exit(1)

        self.state["opens"] = 0
        self.state["closes"] = 0
        self.state["pending_sell_ord_id"] = resp.get("data", {}).get("orderId")
        self.state["pending_sell_px"] = sell_px
        self._save()

    def run(self):
        """主循环"""
        try:
            self.init()
        except SystemExit:
            raise
        except Exception as e:
            self.log.error(f"启动失败: {e}")
            sys.exit(1)

        # 启动 WebSocket（实时推送）
        self._start_ws_thread()

        self.log.info("策略运行中...")
        while self._running:
            try:
                now = time.time()
                # 定时对账（防漏推送，每 60s 一次）
                if now - self.last_reconcile_ts >= self.RECONCILE_SEC:
                    self._reconcile()
                    self.last_reconcile_ts = now

            except SystemExit:
                raise
            except Exception as e:
                self.log.error(f"异常: {e}")

            time.sleep(self.cfg.interval)

        self._save()
        self.log.info("策略已停止")

    def _get_exchange_size(self) -> float:
        """获取交易所实际持仓"""
        try:
            for p in self.client.get_position(self.cfg.symbol).get("data", []):
                if p.get("holdSide") == "short":
                    return float(p.get("total") or 0)
        except Exception as e:
            self.log.debug(f"获取持仓异常: {e}")
        return 0.0

    def _get_position_detail(self) -> dict:
        """获取持仓信息"""
        try:
            for p in self.client.get_position(self.cfg.symbol).get("data", []):
                if p.get("holdSide") == "short":
                    return {
                        "size": float(p.get("total") or 0),
                        "avg_price": float(p.get("openPriceAvg") or 0),
                        "margin_ratio": float(p.get("marginRatio") or 0),
                    }
        except:
            pass
        return {}

    def _on_ws_reconnect(self):
        """WS (重)连成功回调：强制下次主循环立即对账，补上断连空窗期漏掉的成交"""
        self.last_reconcile_ts = 0.0

    def _start_ws_thread(self):
        """启动 WebSocket 线程（实时推送，带自动重连）"""
        def ws_loop():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.client.ws_connect(
                    self.cfg.symbol, self._on_ws_message,
                    on_reconnect=self._on_ws_reconnect,
                    should_stop=lambda: not self._running,
                ))
            except Exception as e:
                self.log.warning(f"WebSocket 连接失败: {e}，依赖定时对账防护")

        ws_thread = threading.Thread(target=ws_loop, daemon=True)
        ws_thread.start()
        self.log.info("WebSocket 线程已启动")

    async def _on_ws_message(self, push: dict):
        """WebSocket 订单推送回调（归一化 Bitget 字段 → on_fill 格式）"""
        status = push.get("status")  # live / partially_filled / filled / canceled
        if status not in ("filled", "canceled"):
            return
        order = {
            "ordId": push.get("orderId"),
            "status": "filled" if status == "filled" else "cancelled",
            "avgPx": push.get("priceAvg") or push.get("fillPrice") or 0,
            "accFillSz": push.get("accBaseVolume") or push.get("baseVolume") or 0,
        }
        self.on_fill(order)

    def _reconcile(self):
        """对账入口：防 race（5s 内有成交/刷新则跳过）后持 RLock 调用，
        与 on_fill 串行，防止 closes/opens 读改写竞态多算/漏算而留裸仓。
        last_fill_ts 专记成交，与主循环调度用的 last_reconcile_ts 分开。

        ★ race guard 必须在锁内判定：若放在锁外，on_fill 正在处理成交（持锁）时
        本函数可能先通过 guard（last_fill_ts 尚未更新）再阻塞等锁，等拿到锁时成交
        已发生却照样跑对账 → 与 WS 双算（FUTUUSDT 2026-06-24 事故）。对齐 OKX 版
        on_tick 的 `with lock: maybe_reconcile`。"""
        with self._lock:
            now = time.time()
            if now - self.last_refresh_ts < 5 or now - self.last_fill_ts < 5:
                return
            self._do_reconcile()

    def _do_reconcile(self):
        """对账主体（已在 RLock 内）- 先检查漏推送，再修复差异"""
        try:
            # ★ 单一共享快照：一次性、相邻地取「挂单 + 持仓」，供漏推检查与 diff 复用。
            # 旧实现里 _check_missed_fills 自取 get_open_orders、diff 又自取 get_position，
            # 两次不同步的 REST 读之间「订单列表 vs 持仓」存在时间差：持仓已减、挂单列表
            # 仍列着该 BUY → 漏推检查跳过、diff 却记 closes 且不摘 oid → 稍后 WS 推送再记
            # 一次 = 双算（FUTUUSDT 2026-06-24）。对齐 OKX 版共享快照结构。
            try:
                open_orders = self.client.get_open_orders(self.cfg.symbol)
                positions = self.client.get_position(self.cfg.symbol).get("data", [])
            except Exception as e:
                self.log.warning(f"对账查询失败，本轮跳过: {e}")
                return
            open_ids = {o.get("orderId") for o in open_orders}
            exch_sz = 0.0
            for p in positions:
                if p.get("holdSide") == "short":
                    exch_sz = float(p.get("total") or 0)
                    break

            # ① 优先检查 WS 漏推送（用共享挂单快照；已成交→补发 on_fill 并 pop oid）
            self._check_missed_fills(open_ids)

            # ③ 计算本地预期持仓
            opens = self.state.get("opens", 0)
            closes = self.state.get("closes", 0)
            local_sz = (opens - closes) * self.POSITION_SZ

            # ④ 检测零头（部分成交但未完全对齐）。对齐 OKX 版理念：
            #    零头不进 stack 但**不中断对账**——仅按整张部分继续修复 diff，
            #    零头留在交易所等人工平掉。告警带 dedup_key + 1h 节流防每 60s 刷屏
            #    （旧实现无 dedup、且 return 掉对账，导致零头存在期间「刷屏 + 网格停摆」）。
            n_exch, frac = self._n_and_remainder(exch_sz)
            if frac > 1e-9:
                self.log.error(f"检测到零头: {frac:.4f}（整张 {n_exch}），按整张部分继续对账")
                self._notify(f"零头告警: {frac:.4f}，请在网页手动平仓（不影响网格）",
                             level="WARN", dedup_key="remainder", throttle_sec=3600)

            # ⑤ 对比（只按整张部分；容忍 < POSITION_SZ 的差异）
            effective_exch = n_exch * self.POSITION_SZ
            diff = effective_exch - local_sz

            if abs(diff) < self.POSITION_SZ - 1e-9:
                # 一致，补挂缺失的单
                self._ensure_orders_complete()
                return

            # ⑥ 差异 >= 1 张，自动修复（交易所为准）
            if diff < 0:
                # 交易所 < 本地：部分被平 → closes++
                n_closed = round(-diff / self.POSITION_SZ)
                # ★ 摘掉已不在交易所挂单列表里的 BUY（= 已成交/已撤的 oid），防止稍后到达
                # 的 WS 推送对同一笔再记一次 closes。摘除后 on_fill 的「非追踪订单直接 return」
                # 幂等丢弃这些迟到推送（FUTUUSDT 2026-06-24 双算根因修复）。
                for oid in [o for o in list(self.state.get("pending_buys", {}).keys())
                            if o not in open_ids]:
                    self.state["pending_buys"].pop(oid, None)
                self.state["closes"] += n_closed
                self.log.warning(f"对账修复(平): closes+{n_closed}")
                self._notify(f"对账: 平仓 {n_closed} 张")

            elif diff > 0:
                # 交易所 > 本地：外部加仓 → opens++
                n_added = round(diff / self.POSITION_SZ)
                self.state["opens"] += n_added
                self.log.warning(f"对账修复(加): opens+{n_added}")
                self._notify(f"对账: 加仓 {n_added} 张")

            self._save()

            # 平到 opens==closes>0 → 一轮完成，与 WS 平仓路径(_handle_buy_fill)一致：
            # 撤所有挂单 + sys.exit(0)。漏了这步会导致仓位已平却空转、甚至凭空重挂 SELL。
            opens = self.state.get("opens", 0)
            closes = self.state.get("closes", 0)
            if opens > 0 and opens == closes:
                self.log.info("对账检测到一轮已平完，正常结束")
                self._cycle_complete()
                return

            self._refresh_orders()

        except Exception as e:
            self.log.warning(f"对账异常: {e}")

    def _ensure_orders_complete(self):
        """补挂缺失的单"""
        stack_top = self.state.get("stack_top", 0)
        if stack_top <= 0:
            return

        try:
            open_ids = {o.get("orderId") for o in self.client.get_open_orders(self.cfg.symbol)}
        except:
            return

        n_pos = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))

        # 无持仓时不补任何单（防止 0 持仓凭空挂 SELL 重新开空）
        if n_pos == 0:
            return

        # 检查 SELL
        sell_id = self.state.get("pending_sell_ord_id")
        if not sell_id or (sell_id not in open_ids):
            sell_px = self._round_px(stack_top * (1 + self.GRID_PCT))
            self._place_sell(sell_px)

        # 检查 BUY
        self._refresh_orders()

    def _check_missed_fills(self, open_ids=None):
        """检查 WS 漏推送（补救已成交但漏推的订单）

        :param open_ids: 可选的交易所挂单 id 集合（对账传入共享快照，避免重复 REST 读、
                         也避免与 diff 的持仓读不同步）。为 None 时自取（兼容旧调用）。
        """
        try:
            if open_ids is None:
                open_ids = {o.get("orderId") for o in self.client.get_open_orders(self.cfg.symbol)}

            # 检查 SELL（Bitget v3 字段为 status/priceAvg，兼容 orderStatus/avgPrice 命名）
            sell_id = self.state.get("pending_sell_ord_id")
            if sell_id and sell_id not in open_ids:
                info = self.client.get_order_info(self.cfg.symbol, sell_id)
                if (info.get("status") or info.get("orderStatus")) == "filled":
                    # 补救：补推一个虚拟的 on_fill 事件
                    fake_order = {
                        "ordId": sell_id,
                        "status": "filled",
                        "avgPx": (info.get("priceAvg") or info.get("avgPrice")
                                  or self.state.get("pending_sell_px")),
                    }
                    self.on_fill(fake_order)

            # 检查 BUY
            for oid in list(self.state.get("pending_buys", {}).keys()):
                if oid not in open_ids:
                    info = self.client.get_order_info(self.cfg.symbol, oid)
                    if (info.get("status") or info.get("orderStatus")) == "filled":
                        # 补救：补推一个虚拟的 on_fill 事件
                        fake_order = {
                            "ordId": oid,
                            "status": "filled",
                            "avgPx": info.get("priceAvg") or info.get("avgPrice"),
                        }
                        self.on_fill(fake_order)

        except Exception as e:
            self.log.debug(f"漏推送检测异常: {e}")

    def on_fill(self, order: dict):
        """WS 推送订单成交事件（串行化：RLock 防止并发重复挂单）"""
        with self._lock:
            ord_id = order.get("ordId")
            status = order.get("status")  # "filled" / "cancelled" / "rejected"

            # ① 验证：只处理本合约的 pending 订单
            sell_id = self.state.get("pending_sell_ord_id")
            pending_buys = self.state.get("pending_buys", {})

            if ord_id not in {sell_id} | set(pending_buys.keys()):
                return  # 忽略非追踪订单

            # ② 订单成交或被撤销
            if status == "filled":
                if ord_id == sell_id:
                    self._handle_sell_fill(order)
                elif ord_id in pending_buys:
                    self._handle_buy_fill(order)

            elif status in ("cancelled", "mmp_cancelled", "rejected"):
                if ord_id == sell_id:
                    self._handle_sell_cancel(order)
                elif ord_id in pending_buys:
                    self._handle_buy_cancel(order)

    def _handle_sell_fill(self, order: dict):
        """SELL 成交 → 加仓"""
        # 原则：零头不进 stack，需人工处理
        acc_fill = float(order.get("accFillSz") or 0)
        frac = acc_fill % self.POSITION_SZ
        if frac > 1e-9:
            self.log.error(f"SELL 零头告警: 成交 {acc_fill}，零头 {frac}")
            self._notify(f"SELL 零头: {frac}，请手动平仓", level="WARN")
            return

        px = float(order.get("avgPx") or 0) or self.state.get("pending_sell_px", 0)
        self.state["stack_top"] = px
        self.state["opens"] += 1
        self.state["pending_sell_ord_id"] = None
        self.state["pending_sell_px"] = None
        self.last_fill_ts = time.time()

        n = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))
        self._notify(f"加仓成交 | 成交价 {px:.6f} | 持仓 {n}单")
        self._save()
        self._refresh_orders()

    def _handle_sell_cancel(self, order: dict):
        """SELL 被撤 → 重挂同价位"""
        acc_fill = float(order.get("accFillSz") or 0)
        if acc_fill > 0:
            self.log.error(f"SELL 部分成交但被撤: {acc_fill}，零头需人工处理")
            self._notify(f"SELL 零头: {acc_fill}，请手动平仓", level="WARN")
            return

        self.state["pending_sell_ord_id"] = None
        self._save()
        self._refresh_orders()

    def _handle_buy_fill(self, order: dict):
        """BUY 成交 → 平仓（与 OKX 版 _handle_close_filled 一致）"""
        ord_id = order.get("ordId")
        px = float(order.get("avgPx") or 0)
        # 固定配对入场价：取挂单时记录的 entry_px（不可变），缺失才回退 stack_top。
        # 旧实现误用 stack_top 当入场价 → PnL 与盈亏统计失真。
        buy_info = self.state.get("pending_buys", {}).get(ord_id, {})
        entry_px = float(buy_info.get("entry_px") or 0) or self.state.get("stack_top", 0)
        pnl = (entry_px - px) * self.POSITION_SZ

        self.state["closes"] += 1
        self.state["pending_buys"].pop(ord_id, None)
        # ★ BUY 平仓成交 → stack_top 下移到成交价，使下一个加仓 SELL 重新锚定在
        # px×(1+grid) 跟随行情下移（与 OKX 一致）。旧实现漏了这步：stack_top 冻结在
        # 最高 SELL 价，加仓网格脱离行情、错失下行带的再做空 → 持续亏钱。
        if px > 0:
            self.state["stack_top"] = px
        self.last_fill_ts = time.time()

        self._notify(f"平仓成交 | 成交价 {px:.6f} | 盈亏 {pnl:+.2f}")
        self._save()

        # 检查一轮是否完成
        opens = self.state.get("opens", 0)
        closes = self.state.get("closes", 0)
        if opens > 0 and opens == closes:
            self._cycle_complete()
        else:
            self._refresh_orders()

    def _handle_buy_cancel(self, order: dict):
        """BUY 被撤 → 退避计数后重挂（能进到这里说明非本策略主动撤 = 系统撤）

        注：本策略主动撤的 BUY 在 _refresh_orders 里已先 pop 出 pending_buys，
        因此走到这里的撤单都是交易所/系统侧撤单，计入退避。
        """
        ord_id = order.get("ordId")
        acc_fill = float(order.get("accFillSz") or 0)
        info = self.state.get("pending_buys", {}).get(ord_id, {})
        re_px = float(info.get("target_px") or 0)

        self.state["pending_buys"].pop(ord_id, None)
        self._save()

        if acc_fill > 0:
            self.log.error(f"BUY 部分成交但被撤: {acc_fill}，零头需人工处理")
            self._notify(f"BUY 零头: {acc_fill}，请手动平仓", level="WARN")
            return

        # 系统撤单退避计数：频繁被撤同价位 → 退避，告警
        if re_px > 0 and self._note_buy_cancel(re_px):
            self.log.error(f"BUY @{re_px:.6f} 窗口内被撤 ≥{self.BUY_CANCEL_BACKOFF_N} 次，退避 {self.BUY_CANCEL_BACKOFF_SEC}s")
            self._notify(f"BUY @{re_px:.6f} 频繁被系统撤，退避 {self.BUY_CANCEL_BACKOFF_SEC}s", level="WARN")

        # 补挂缺口（_refresh_orders 内部会跳过退避中的价位）
        self._refresh_orders()

    def _cycle_complete(self):
        """一轮交易完成 - 清理所有挂单后退出。

        退出前**必须**核对交易所实际持仓：opens==closes 只是本地账目，若对账/WS
        竞态多算了 closes，会在实盘仍有持仓时误判完成。直接退出将留下无网格的裸空单
        （TNSRUSDT 2026-06-21 事故）。故实盘 ≠ 0 时不退出，按实盘修正并重挂网格自愈。
        """
        exch_sz = self._get_exchange_size()
        if exch_sz > 1e-9:
            n_pos = max(1, round(exch_sz / self.POSITION_SZ))
            frac = exch_sz % self.POSITION_SZ
            self.log.error(f"一轮完成前发现实盘仍有 {exch_sz} 张持仓，取消退出，按实盘修正重挂网格")
            if frac > 1e-9:
                self._notify(f"⚠️ 平仓不彻底且有零头 {frac:.4f}，请手动核查", level="CRITICAL")
            self._notify(f"⚠️ 平仓不彻底：实盘仍有 ~{n_pos} 单，已按实盘修正继续维护网格（防裸仓）", level="CRITICAL")
            # 以交易所为准重置账目并重建 stack_top（同接管逻辑），撤旧挂单后重挂
            self.state["opens"] = n_pos
            self.state["closes"] = 0
            self.state["pending_sell_ord_id"] = None
            self.state["pending_sell_px"] = None
            self.state["pending_buys"] = {}
            det = self._get_position_detail()
            if det.get("avg_price", 0) > 0:
                self.state["stack_top"] = det["avg_price"]
            self._save()
            self._cancel_stale_pending_on_startup()
            self._refresh_orders()
            return

        # 实盘确认为 0 → 正常清理退出
        # 撤销 SELL
        sell_id = self.state.get("pending_sell_ord_id")
        if sell_id:
            self._safe_cancel(sell_id)

        # 撤销所有 BUY
        for oid in list(self.state.get("pending_buys", {}).keys()):
            self._safe_cancel(oid)

        # 清空本地状态
        self.state["stack_top"] = 0
        self.state["opens"] = 0
        self.state["closes"] = 0
        self.state["pending_sell_ord_id"] = None
        self.state["pending_sell_px"] = None
        self.state["pending_buys"] = {}
        self._save()

        self._notify("一轮做空完成，所有挂单已清理", level="TRADE")
        sys.exit(0)

    def _refresh_orders(self):
        """刷新网格（SELL 唯一，BUY 数量 = min(持仓, MAX_BUYS)）"""
        with self._lock:
            self.last_refresh_ts = time.time()

            stack_top = self.state.get("stack_top", 0)
            if stack_top <= 0:
                return

            n_pos = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))

            # 限价/基准起仓等待态：opens==0 且初始 SELL 还挂着等成交 → 原样不动。
            # 初始 SELL 挂在 stack_top（--limit）或基准价×(1+grid)（--adopt）处，
            # 若此时按 stack_top×(1+grid) 重算会把它误撤、又因 n_pos==0 不补，导致空挂。
            # 等它成交后 _handle_sell_fill 置 opens=1 再走正常网格。
            if n_pos == 0 and self.state.get("pending_sell_ord_id"):
                return

            # ① SELL 唯一：永远只有 1 个 @ stack_top × (1+GRID)
            sell_px = self._round_px(stack_top * (1 + self.GRID_PCT))
            cur_sell_id = self.state.get("pending_sell_ord_id")
            cur_sell_px = self.state.get("pending_sell_px")

            if not cur_sell_id or cur_sell_px is None or abs(cur_sell_px - sell_px) > 1e-9:
                if cur_sell_id:
                    if not self._safe_cancel(cur_sell_id):
                        self.log.error(f"撤 SELL 失败: {cur_sell_id}，快速失败")
                        sys.exit(1)
                if n_pos > 0:
                    self._place_sell(sell_px)

            # ② BUY 梯队：depth = min(持仓, MAX_BUYS)
            if n_pos == 0:
                # 无持仓则撤所有 BUY
                for oid in list(self.state.get("pending_buys", {}).keys()):
                    if not self._safe_cancel(oid):
                        self.log.error(f"撤 BUY 失败: {oid}，快速失败")
                        sys.exit(1)
                self.state["pending_buys"] = {}
                self._save()
                return

            depth = min(n_pos, self.MAX_BUYS)
            desired = []
            for i in range(1, depth + 1):
                tpx = self._round_px(stack_top * ((1 - self.GRID_PCT) ** i))
                epx = stack_top * ((1 - self.GRID_PCT) ** (i - 1))
                desired.append((tpx, epx))

            pending = self.state.get("pending_buys", {})
            tol = max(1e-9, stack_top * self.GRID_PCT * 0.4)

            # ③ 撤销不需要的单（多了裁最深的 = 价格最低的 BUY）
            to_cancel = []
            for oid, info in pending.items():
                if not any(abs(float(info.get("target_px", 0)) - d[0]) < tol for d in desired):
                    to_cancel.append(oid)

            # 如果要撤的单数 > depth，只撤最深的（最低价）
            if len(to_cancel) > len(desired) - len(pending) + len(to_cancel):
                # 按价格排序，撤最低的
                to_cancel.sort(key=lambda oid: float(pending[oid].get("target_px", float('inf'))))
                to_cancel = to_cancel[:len(to_cancel) - (depth - len(pending) + len(to_cancel))]

            for oid in to_cancel:
                if not self._safe_cancel(oid):
                    self.log.error(f"撤 BUY 失败: {oid}，快速失败")
                    sys.exit(1)
                pending.pop(oid, None)

            # ④ 挂缺少的单（补几何梯队，退避中的价位跳过）
            current_pxs = [float(v.get("target_px", 0)) for v in pending.values()]
            for tpx, epx in desired:
                if any(abs(tpx - c) < tol for c in current_pxs):
                    continue
                if self._in_backoff(tpx):
                    continue  # 该价位退避中，等退避结束后由对账补挂
                self._place_buy(epx, tpx)

            self._save()

    def _can_open_sell(self, px: float) -> bool:
        """统一风控（起仓 + 加 SELL 共用，无分支）。两条规则：
        ① 保证金率 ≥ 500%（equity / mmr ≥ 5.0）
        ② 加仓后总名义 ≤ max_notional_usdt
        无持仓/查询异常 → 放行。
        """
        try:
            # 账户级权益与维持保证金（联合保证金账户级，比持仓级更可靠）
            a = self.client.get_account().get("data", [{}])[0]
            equity = float(a.get("accountEquity") or 0)
            mmr = float(a.get("mmr") or 0)

            cur_size = 0.0
            for p in self.client.get_position(self.cfg.symbol).get("data", []):
                if p.get("holdSide") == "short":
                    cur_size = float(p.get("total") or 0)
                    break

            # ① 保证金率 ≥ 500%
            if mmr > 0 and equity / mmr < 5.0:
                self._notify(f"新增 SELL 被风控拒: 保证金率 {equity/mmr*100:.0f}% < 500%", level="WARN")
                return False

            # ② 加仓后总名义 ≤ 上限
            notional = (cur_size + self.POSITION_SZ) * px
            if notional > self.cfg.max_notional_usdt:
                self._notify(f"新增 SELL 被风控拒: 总名义 {notional:.0f} > {self.cfg.max_notional_usdt}", level="WARN")
                return False

            return True
        except Exception as e:
            self.log.warning(f"风控查询异常，放行: {e}")
            return True

    def _place_sell(self, px: float):
        """挂SELL（原则：SELL 唯一，永远只有 1 个）"""
        # 强制检查：不能同时有 2 个 SELL
        old_sell_id = self.state.get("pending_sell_ord_id")
        if old_sell_id:
            self.log.error(f"BUG: 检测到多个 SELL，old_id={old_sell_id}，立即退出")
            sys.exit(1)

        # 风控闸门：加 SELL 被拒 → 跳过不挂（不动现有挂单，下次成交/对账重试）
        if not self._can_open_sell(px):
            return

        try:
            resp = self.client.place_order(
                self.cfg.symbol, "sell", self.POSITION_SZ, px,
                order_type="limit", cl_ord_id=_gen_cl_ord_id("S")
            )
            if resp.get("code") == "00000":
                oid = resp.get("data", {}).get("orderId")
                self.state["pending_sell_ord_id"] = oid
                self.state["pending_sell_px"] = px
                self._save()
            else:
                # 交易所拒单（如限价带：SELL 目标远低于市价）→ 不降级、不退出，
                # 仅记录 + 告警，等下次成交/对账重试。避免追涨乱加空。
                self.log.warning(f"SELL 挂单被交易所拒(不降级): code={resp.get('code')} msg={resp.get('msg')}")
                self._notify(f"SELL 被拒(限价带?): {resp.get('msg')}，暂停加仓等回落", level="WARN")
        except Exception as e:
            # 网络/异常同样不退出，等下次重试
            self.log.warning(f"SELL 挂单异常(不降级): {e}")

    def _place_buy(self, entry: float, px: float):
        """挂BUY（原则：固定配对，entry_px 永不改动）"""
        try:
            resp = self.client.place_order(
                self.cfg.symbol, "buy", self.POSITION_SZ, px,
                order_type="limit", reduce_only=True, cl_ord_id=_gen_cl_ord_id("B")
            )
            if resp.get("code") == "00000":
                oid = resp.get("data", {}).get("orderId")
                if oid:
                    if "pending_buys" not in self.state:
                        self.state["pending_buys"] = {}
                    # 固定配对：entry_px 记录后永不改动
                    self.state["pending_buys"][oid] = {
                        "entry_px": entry,   # 对应的 SELL 价格（不可变）
                        "target_px": px      # BUY 价格
                    }
            else:
                # BUY 挂单失败 → 记录但继续（可能是价格穿过等）
                self.log.warning(f"BUY 挂单失败: {resp.get('msg')}")

        except Exception as e:
            self.log.warning(f"BUY 挂单异常: {e}")

    # ====== BUY 重挂退避 ======

    def _note_buy_cancel(self, px: float) -> bool:
        """记录一次 BUY 系统撤单；窗口内累计 ≥N 次则进入退避，返回 True"""
        now = time.time()
        k = round(float(px), 8)
        hits = [t for t in self._buy_cancel_hits.get(k, []) if now - t < self.BUY_CANCEL_WINDOW_SEC]
        hits.append(now)
        self._buy_cancel_hits[k] = hits
        if len(hits) >= self.BUY_CANCEL_BACKOFF_N:
            self._buy_backoff[k] = now + self.BUY_CANCEL_BACKOFF_SEC
            self._buy_cancel_hits[k] = []
            return True
        return False

    def _in_backoff(self, px: float) -> bool:
        """该价位是否在退避窗口内"""
        now = time.time()
        k = round(float(px), 8)
        until = self._buy_backoff.get(k, 0)
        if until <= now:
            self._buy_backoff.pop(k, None)
            return False
        return True

    def _round_px(self, px: float) -> float:
        """取整价格"""
        try:
            place = int(self.contract_info.get("pricePlace", 4))
        except:
            place = 4
        return float(_quantize_price(_to_decimal(px), place))


if __name__ == "__main__":
    cfg = parse_args()
    Strategy(cfg).run()
