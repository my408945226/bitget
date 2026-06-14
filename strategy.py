"""Bitget 金字塔做空策略"""
import sys
import time
import signal
import threading
import pickle
import logging
import os
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

from config import parse_args, Config
from client import BitgetClient, _gen_cl_ord_id


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


# ============ 通知模块 ============
def _send_telegram(msg: str, bot_token: str, chat_id: str):
    """发送 Telegram 消息"""
    if not bot_token or not chat_id or not requests:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = _setup_logger(cfg.symbol)
        self.dry_run = cfg.mode == "dry-run"
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log, dry_run=self.dry_run)
        self.state: State = _load_state(cfg.symbol)
        self.GRID_PCT = cfg.grid_pct
        self.POSITION_SZ = cfg.size
        self.MAX_BUYS = 60
        self.RECONCILE_SEC = 30
        self.contract_info: dict = {}
        self.last_reconcile_ts = 0.0
        self.last_refresh_ts = 0.0
        self._lock = threading.RLock()
        self._running = True
        self._error_state = {}
        signal.signal(signal.SIGINT, self._handle_exit)

    def _handle_exit(self, sig, frame):
        """优雅退出（SIGINT 处理）"""
        self.log.info("收到中断信号，正在优雅退出...")
        self._running = False
        self._notify("⏹️ 策略已手动停止")

    def _notify(self, msg: str):
        """发送通知"""
        self.log.info(msg)
        if not self.dry_run and self.cfg.tg_bot_token:
            _send_telegram(f"<b>{self.cfg.symbol}</b>\n{msg}", self.cfg.tg_bot_token, self.cfg.tg_chat_id)

    def _save(self):
        """保存状态"""
        _save_state(self.state)

    def _safe_cancel(self, ord_id: str):
        """撤单（失败则记录，由调用者决定是否快速失败）"""
        try:
            resp = self.client.cancel(self.cfg.symbol, ord_id)
            if resp and resp.get("code") != "00000":
                self.log.warning(f"撤单失败 {ord_id}: {resp.get('msg')}")
                return False
            return True
        except Exception as e:
            self.log.warning(f"撤单异常 {ord_id}: {e}")
            return False

    def _check_account_config(self) -> bool:
        """账户配置检查（快速失败原则）"""
        try:
            # ① 检查合约有效性
            if not self.contract_info or self.contract_info.get("symbolStatus") != "normal":
                self.log.error(f"合约无效: {self.contract_info.get('symbolStatus')}")
                return False

            # ② 账户层级检查（必须为 3 = 跨币种保证金）
            acc = self.client.get_account()
            acct_lv = acc.get("data", [{}])[0].get("accountLevel", "")
            if acct_lv != "3":
                self.log.error(f"账户层级必须为 3，当前: {acct_lv}")
                return False

            # ③ 杠杆一致性检查（有持仓时必须 = 3x）
            positions = self.client.get_position(self.cfg.symbol).get("data", [])
            for p in positions:
                if float(p.get("pos", 0)) != 0:
                    lever = float(p.get("lever", 0))
                    if abs(lever - 3) > 0.1:
                        self.log.error(f"杠杆不一致: {lever}，需要 3x")
                        return False

            # ④ 无持仓时设置杠杆 3x
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
        mode = "DRY" if self.dry_run else "LIVE"
        self.log.info(f"=== 初始化 {self.cfg.symbol} [{mode}] ===")

        self.contract_info = self.client.get_contracts(self.cfg.symbol) or {}

        if not self.dry_run:
            if not self._check_account_config():
                sys.exit(1)
            self._cancel_stale_pending_on_startup()

        # 接管或起仓
        if not self._adopt_position():
            self._open()

        # 挂网格
        self._refresh_orders()

        n = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))
        self._notify(f"✅ {self.cfg.symbol} | pos={n}")
        self._save()

    def _adopt_position(self) -> bool:
        """接管持仓"""
        if self.dry_run:
            return False
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
        """起仓"""
        if self.cfg.adopt_sell_px > 0:
            base_px = self.cfg.adopt_sell_px
            sell_px = self._round_px(base_px * (1 + self.GRID_PCT))
            self.state["stack_top"] = base_px
        elif self.cfg.initial_sell_px > 0:
            sell_px = self.cfg.initial_sell_px
            self.state["stack_top"] = sell_px
        else:
            resp = self.client.place_order(self.cfg.symbol, "sell", self.POSITION_SZ, 0.0, order_type="market")
            if resp.get("code") != "00000":
                self.log.error(f"起仓失败: {resp.get('msg')}")
                sys.exit(1)

            px = 1.76 if self.dry_run else self.client.get_price(self.cfg.symbol)
            if px <= 0:
                self.log.error("无法获取成交价")
                sys.exit(1)

            self.state["stack_top"] = px
            self.state["opens"] = 1
            self._save()
            return

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
        """主循环（WS 事件驱动，定时对账为备选）"""
        try:
            self.init()
        except SystemExit:
            raise
        except Exception as e:
            self.log.error(f"启动失败: {e}")
            sys.exit(1)

        self.log.info("策略运行中（WS 驱动）...")
        while self._running:
            try:
                if not self.dry_run:
                    # 定时对账（防 WS 漏推送）
                    now = time.time()
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

    def _get_okx_size(self) -> float:
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

    def _reconcile(self):
        """定时对账（每 30s）"""
        # 防 race：最近 5s 内有成交或刷新，跳过
        now = time.time()
        time_since_refresh = now - self.last_refresh_ts
        time_since_fill = now - self.last_reconcile_ts

        if time_since_refresh < 5 or time_since_fill < 5:
            return

        try:
            # ① 查询 OKX 实际持仓
            okx_sz = self._get_okx_size()

            # ② 计算本地预期持仓
            opens = self.state.get("opens", 0)
            closes = self.state.get("closes", 0)
            local_sz = (opens - closes) * self.POSITION_SZ

            # ③ 检测零头（部分成交但未完全对齐）
            frac = okx_sz % self.POSITION_SZ
            if frac > 1e-9:
                self.log.error(f"检测到零头: {frac:.4f}，需人工处理")
                self._notify(f"⚠️ 零头告警: {frac:.4f}，请在 OKX 网页手动平仓")
                return

            # ④ 对比（容忍 < POSITION_SZ 的差异）
            diff = okx_sz - local_sz

            if abs(diff) < self.POSITION_SZ - 1e-9:
                # 一致，补挂缺失的单
                self._ensure_orders_complete()
                return

            # ⑤ 差异 >= 1 张，自动修复（OKX 为准）
            if diff < 0:
                # OKX < 本地：部分被平 → closes++
                n_closed = round(-diff / self.POSITION_SZ)
                self.state["closes"] += n_closed
                self.log.warning(f"对账修复(平): closes+{n_closed}")
                self._notify(f"对账修复: 平仓 {n_closed} 张")

            elif diff > 0:
                # OKX > 本地：外部加仓 → opens++
                n_added = round(diff / self.POSITION_SZ)
                self.state["opens"] += n_added
                self.log.warning(f"对账修复(加): opens+{n_added}")
                self._notify(f"对账修复: 加仓 {n_added} 张")

            self._save()
            self._refresh_orders()

            # ⑥ 检查漏推送
            self._check_missed_fills()

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

        # 检查 SELL
        sell_id = self.state.get("pending_sell_ord_id")
        if not sell_id or (sell_id not in open_ids):
            sell_px = self._round_px(stack_top * (1 + self.GRID_PCT))
            self._place_sell(sell_px)

        # 检查 BUY
        if n_pos > 0:
            self._refresh_orders()

    def _check_missed_fills(self):
        """检查 WS 漏推送（补救已成交但漏推的订单）"""
        try:
            open_ids = {o.get("orderId") for o in self.client.get_open_orders(self.cfg.symbol)}

            # 检查 SELL
            sell_id = self.state.get("pending_sell_ord_id")
            if sell_id and sell_id not in open_ids:
                info = self.client.get_order_info(self.cfg.symbol, sell_id)
                if info.get("orderStatus") == "filled":
                    # 补救：补推一个虚拟的 on_fill 事件
                    fake_order = {
                        "ordId": sell_id,
                        "status": "filled",
                        "avgPx": info.get("avgPrice", self.state.get("pending_sell_px")),
                    }
                    self.on_fill(fake_order)

            # 检查 BUY
            for oid in list(self.state.get("pending_buys", {}).keys()):
                if oid not in open_ids:
                    info = self.client.get_order_info(self.cfg.symbol, oid)
                    if info.get("orderStatus") == "filled":
                        # 补救：补推一个虚拟的 on_fill 事件
                        fake_order = {
                            "ordId": oid,
                            "status": "filled",
                            "avgPx": info.get("avgPrice"),
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
            self._notify(f"⚠️ SELL 零头: {frac}，请手动平仓")
            return

        px = float(order.get("avgPx") or 0) or self.state.get("pending_sell_px", 0)
        self.state["stack_top"] = px
        self.state["opens"] += 1
        self.state["pending_sell_ord_id"] = None
        self.state["pending_sell_px"] = None
        self.last_reconcile_ts = time.time()

        self._notify(f"SELL @{px:.6f} opens={self.state['opens']}")
        self._save()
        self._refresh_orders()

    def _handle_sell_cancel(self, order: dict):
        """SELL 被撤 → 重挂同价位"""
        acc_fill = float(order.get("accFillSz") or 0)
        if acc_fill > 0:
            self.log.error(f"SELL 部分成交但被撤: {acc_fill}，零头需人工处理")
            self._notify(f"⚠️ SELL 零头: {acc_fill}，请手动平仓")
            return

        self.state["pending_sell_ord_id"] = None
        self._save()
        self._refresh_orders()

    def _handle_buy_fill(self, order: dict):
        """BUY 成交 → 平仓"""
        ord_id = order.get("ordId")
        px = float(order.get("avgPx") or 0)
        entry_px = self.state.get("stack_top", 0)
        pnl = (entry_px - px) * self.POSITION_SZ

        self.state["closes"] += 1
        self.state["pending_buys"].pop(ord_id, None)
        self.last_reconcile_ts = time.time()

        self._notify(f"BUY @{px:.6f} PnL={pnl:+.2f}")
        self._save()

        # 检查一轮是否完成
        opens = self.state.get("opens", 0)
        closes = self.state.get("closes", 0)
        if opens > 0 and opens == closes:
            self._cycle_complete()
        else:
            self._refresh_orders()

    def _handle_buy_cancel(self, order: dict):
        """BUY 被撤 → 删除，后续对账补挂"""
        ord_id = order.get("ordId")
        acc_fill = float(order.get("accFillSz") or 0)

        if acc_fill > 0:
            self.log.error(f"BUY 部分成交但被撤: {acc_fill}，零头需人工处理")
            self._notify(f"⚠️ BUY 零头: {acc_fill}，请手动平仓")
            self.state["pending_buys"].pop(ord_id, None)
            self._save()
            return

        self.state["pending_buys"].pop(ord_id, None)
        self._save()

    def _cycle_complete(self):
        """一轮交易完成"""
        self._notify("一轮做空完成")
        self.state["stack_top"] = 0
        self.state["opens"] = 0
        self.state["closes"] = 0
        self.state["pending_sell_ord_id"] = None
        self.state["pending_sell_px"] = None
        self.state["pending_buys"] = {}
        self._save()
        sys.exit(0)

    def _refresh_orders(self):
        """刷新网格（SELL 唯一，BUY 数量 = min(持仓, MAX_BUYS)）"""
        with self._lock:
            self.last_refresh_ts = time.time()

            stack_top = self.state.get("stack_top", 0)
            if stack_top <= 0:
                return

            n_pos = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))

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

            # ④ 挂缺少的单（补几何梯队）
            current_pxs = [float(v.get("target_px", 0)) for v in pending.values()]
            for tpx, epx in desired:
                if not any(abs(tpx - c) < tol for c in current_pxs):
                    self._place_buy(epx, tpx)

            self._save()

    def _place_sell(self, px: float):
        """挂SELL（原则：SELL 唯一，永远只有 1 个）"""
        # 强制检查：不能同时有 2 个 SELL
        old_sell_id = self.state.get("pending_sell_ord_id")
        if old_sell_id:
            self.log.error(f"BUG: 检测到多个 SELL，old_id={old_sell_id}，立即退出")
            sys.exit(1)

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
            elif resp.get('code') == "50016":
                self._market_add()
            else:
                # 清挂单失败 → 快速失败
                self.log.error(f"SELL 挂单失败: {resp.get('msg')}")
                sys.exit(1)
        except Exception as e:
            self.log.error(f"SELL 挂单异常: {e}")
            sys.exit(1)

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

    def _market_add(self):
        """市价加仓"""
        px = self.client.get_price(self.cfg.symbol) or self.state.get("stack_top", 0)
        if px <= 0:
            return

        try:
            resp = self.client.place_order(
                self.cfg.symbol, "sell", self.POSITION_SZ, 0.0,
                order_type="market"
            )
            if resp.get("code") == "00000":
                self.state["stack_top"] = px
                self.state["opens"] += 1
                self._save()
                self._refresh_orders()
        except:
            pass

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
