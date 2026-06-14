"""栈式金字塔做空策略 - 简化版

核心逻辑:
  - 市价起仓 → 挂 SELL@stack_top×(1+grid) + BUY梯队@stack_top×(1-grid)^i
  - SELL成交 → stack_top上移 → 重挂网格
  - BUY成交 → closes++ → 重挂网格
"""
import sys
import time
import signal
import threading

from .config import parse_args, Config
from .client import BitgetClient, _gen_cl_ord_id
from .logger import setup_logger
from .state import load_state, save_state, State
from .precision import to_decimal, quantize_price, validate_order_size
from .utils import send_telegram


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = setup_logger(cfg.symbol)
        self.dry_run = cfg.mode == "dry-run"
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log, dry_run=self.dry_run)
        self.state: State = load_state(cfg.symbol)
        self.GRID_PCT = cfg.grid_pct
        self.POSITION_SZ = cfg.size
        self.MAX_BUYS = 60
        self.RECONCILE_SEC = 30  # 对账间隔
        self.contract_info: dict = {}
        self.last_reconcile_ts = 0.0
        self._lock = threading.RLock()
        self._running = True
        signal.signal(signal.SIGINT, self._handle_exit)

    def _handle_exit(self, sig, frame):
        self.log.info("退出")
        self._running = False

    def _notify(self, msg: str):
        """通知"""
        self.log.info(msg)
        if not self.dry_run and self.cfg.tg_bot_token:
            send_telegram(f"<b>{self.cfg.symbol}</b>\n{msg}", self.cfg.tg_bot_token, self.cfg.tg_chat_id)

    def init(self):
        """初始化"""
        mode = "DRY" if self.dry_run else "LIVE"
        self.log.info(f"=== {self.cfg.symbol} [{mode}] ===")

        # 获取合约信息
        self.contract_info = self.client.get_contracts(self.cfg.symbol) or {}
        if not self.contract_info:
            self.contract_info = {"symbolStatus": "normal", "sizeMultiplier": 1, "volumePlace": 0, "minTradeNum": 1, "minTradeUSDT": 10, "pricePlace": 4}

        # 设置账户
        if not self.dry_run:
            try:
                self.client.set_hold_mode("one_way_mode")
                self.client.set_leverage(self.cfg.symbol, self.cfg.leverage)
            except: pass

        # 验证 size
        if not self.dry_run:
            mark = self.client.get_price(self.cfg.symbol)
            if mark > 0:
                ok, msg = validate_order_size(to_decimal(self.cfg.size), to_decimal(mark), self.contract_info)
                if not ok:
                    self.log.error(f"size无效: {msg}")
                    sys.exit(1)

        # 清理挂单
        if not self.dry_run:
            for o in self.client.get_open_orders(self.cfg.symbol):
                try: self.client.cancel(self.cfg.symbol, o.get("orderId"))
                except: pass

        # 接管或起仓
        if self.state.get("stack_top", 0) <= 0:
            # 检查是否已有待成交的起仓单
            if self.state.get("pending_sell_ord_id"):
                self.log.info(f"恢复未成交的起仓单 {self.state['pending_sell_ord_id']}")
            elif not self._adopt_position():
                self._open_initial()

        # 初始化 pending_buys（防止后续访问时出现 KeyError）
        if "pending_buys" not in self.state:
            self.state["pending_buys"] = {}
            save_state(self.state)

        # 挂网格
        if self.state.get("stack_top", 0) > 0:
            self._refresh_orders()

        n = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))
        self._notify(f"启动 | grid={self.GRID_PCT*100:.1f}% sz={self.POSITION_SZ} pos={n}")

    def _adopt_position(self) -> bool:
        """接管持仓"""
        if self.dry_run: return False
        try:
            for p in self.client.get_position(self.cfg.symbol).get("data", []):
                if p.get("holdSide") == "short":
                    total = float(p.get("total") or 0)
                    avg = float(p.get("openPriceAvg") or 0)
                    if total > 0 and avg > 0:
                        self.state["stack_top"] = avg
                        self.state["opens"] = max(1, int(total / self.POSITION_SZ))
                        self.state["closes"] = 0
                        save_state(self.state)
                        self.log.info(f"接管 {total}张 @{avg:.6f}")
                        return True
        except: pass
        return False

    def _open_initial(self):
        """起仓：三种模式支持"""
        # 模式优先级：adopt_sell_px > initial_sell_px > 默认市价
        if self.cfg.adopt_sell_px > 0:
            self._open_adopt()
        elif self.cfg.initial_sell_px > 0:
            self._open_limit()
        else:
            self._open_market()

    def _open_market(self):
        """模式 1：市价起仓"""
        self.log.info(f"起仓(市价) {self.POSITION_SZ}张")
        resp = self.client.place_order(self.cfg.symbol, "sell", self.POSITION_SZ, 0.0, order_type="market")
        if resp.get("code") != "00000":
            self.log.error(f"起仓失败: {resp.get('msg')}")
            sys.exit(1)

        px = 1.76 if self.dry_run else self.client.get_price(self.cfg.symbol)
        if px <= 0:
            try:
                for p in self.client.get_position(self.cfg.symbol).get("data", []):
                    if p.get("holdSide") == "short":
                        px = float(p.get("openPriceAvg") or 0)
            except: pass
        
        if px <= 0:
            self.log.error("无法获取成交价")
            sys.exit(1)

        self.state["stack_top"] = px
        self.state["opens"] = 1
        self.state["closes"] = 0
        save_state(self.state)
        self.log.info(f"起仓成功 @{px:.6f}")

    def _open_limit(self):
        """模式 2：限价起仓（指定价格，立即启动网格）"""
        px = self.cfg.initial_sell_px
        self.log.info(f"起仓(限价) @ {px:.6f}")

        resp = self.client.place_order(
            self.cfg.symbol, "sell", self.POSITION_SZ, px,
            order_type="limit", cl_ord_id=_gen_cl_ord_id("initSELL")
        )
        if resp.get("code") != "00000":
            self.log.error(f"起仓失败: {resp.get('msg')}")
            sys.exit(1)

        ord_id = resp.get("data", {}).get("orderId")
        self.state["stack_top"] = px
        self.state["opens"] = 0
        self.state["closes"] = 0
        self.state["pending_sell_ord_id"] = ord_id
        self.state["pending_sell_px"] = px
        self.state["pending_buys"] = {}  # 初始化
        self.state["last_action_time"] = time.time()
        save_state(self.state)
        self.log.info(f"限价挂单 {ord_id} @ {px:.6f}，网格已启动")

    def _open_adopt(self):
        """模式 3：基准价起仓（自动偏移，立即启动网格）"""
        base_px = self.cfg.adopt_sell_px
        sell_px = self._round_px(base_px * (1 + self.GRID_PCT))
        self.log.info(f"起仓(基准) base={base_px:.6f} → SELL@{sell_px:.6f}")

        resp = self.client.place_order(
            self.cfg.symbol, "sell", self.POSITION_SZ, sell_px,
            order_type="limit", cl_ord_id=_gen_cl_ord_id("adoptSELL")
        )
        if resp.get("code") != "00000":
            self.log.error(f"起仓失败: {resp.get('msg')}")
            sys.exit(1)

        ord_id = resp.get("data", {}).get("orderId")
        self.state["stack_top"] = base_px
        self.state["opens"] = 0
        self.state["closes"] = 0
        self.state["pending_sell_ord_id"] = ord_id
        self.state["pending_sell_px"] = sell_px
        self.state["pending_buys"] = {}  # 初始化
        self.state["last_action_time"] = time.time()
        save_state(self.state)
        self.log.info(f"基准挂单 {ord_id} @ {sell_px:.6f}，网格已启动")

    def run(self):
        """主循环"""
        self.init()
        self.log.info("运行中...")
        while self._running:
            try:
                if not self.dry_run:
                    self._check_fills()
                    # 定时对账
                    now = time.time()
                    if now - self.last_reconcile_ts >= self.RECONCILE_SEC:
                        self._reconcile()
                        self.last_reconcile_ts = now
            except Exception as e:
                self.log.error(f"异常: {e}")
            time.sleep(self.cfg.interval)
        save_state(self.state)

    def _reconcile(self):
        """对账：本地 vs 交易所持仓 + 漏推送检测"""
        try:
            # 1. 查询交易所实际持仓
            pos_data = self.client.get_position(self.cfg.symbol).get("data", [])
            okx_sz = 0
            for p in pos_data:
                if p.get("holdSide") == "short":
                    okx_sz = float(p.get("total") or 0)
                    break

            local_sz = (self.state.get("opens", 0) - self.state.get("closes", 0)) * self.POSITION_SZ
            diff = abs(okx_sz - local_sz)

            # 2. 检测是否一致
            if diff >= self.POSITION_SZ - 1e-9:
                # 自动修复
                if okx_sz < local_sz:
                    n = round((local_sz - okx_sz) / self.POSITION_SZ)
                    self.state["closes"] += n
                    self.log.warning(f"对账: 本地多{n}张 → closes+{n}")
                else:
                    n = round((okx_sz - local_sz) / self.POSITION_SZ)
                    self.state["opens"] += n
                    self.log.warning(f"对账: 本地少{n}张 → opens+{n}")
                save_state(self.state)
                self._refresh_orders()

            # 3. 检测 WS 漏推送（订单消失但未标记成交）
            try:
                open_ids = {o.get("orderId") for o in self.client.get_open_orders(self.cfg.symbol)}

                # SELL 单是否丢失
                sell_id = self.state.get("pending_sell_ord_id")
                if sell_id and sell_id not in open_ids:
                    info = self.client.get_order_info(self.cfg.symbol, sell_id)
                    status = info.get("orderStatus", "unknown")
                    if status not in ["filled", "cancelled"]:
                        self.log.warning(f"检测到 SELL 单 {sell_id} 可能未推送成交，状态: {status}")

                # BUY 单是否丢失
                for oid in self.state.get("pending_buys", {}).keys():
                    if oid not in open_ids:
                        info = self.client.get_order_info(self.cfg.symbol, oid)
                        status = info.get("orderStatus", "unknown")
                        if status not in ["filled", "cancelled"]:
                            self.log.warning(f"检测到 BUY 单 {oid} 可能未推送成交，状态: {status}")
            except Exception as e:
                self.log.debug(f"漏推送检测失败: {e}")

        except Exception as e:
            self.log.warning(f"对账失败: {e}")

    def _check_fills(self):
        """检查成交"""
        sell_id = self.state.get("pending_sell_ord_id")
        buys = self.state.get("pending_buys", {})
        if not sell_id and not buys:
            return

        try:
            open_ids = {o.get("orderId") for o in self.client.get_open_orders(self.cfg.symbol)}
        except:
            return

        # SELL 成交或被撤销
        if sell_id and sell_id not in open_ids:
            info = self.client.get_order_info(self.cfg.symbol, sell_id)
            status = info.get("orderStatus", "")

            if status == "filled":
                px = float(info.get("avgPrice") or 0) or self.state.get("pending_sell_px") or 0
                self.state["stack_top"] = px
                self.state["opens"] += 1
                self.state["pending_sell_ord_id"] = None
                self.state["pending_sell_px"] = None
                save_state(self.state)
                self._notify(f"SELL @{px:.6f} opens={self.state['opens']}")
                self._refresh_orders()
            elif status == "cancelled":
                self.state["pending_sell_ord_id"] = None
                self.state["pending_sell_px"] = None
                save_state(self.state)
                self.log.warning("SELL 单被撤销，将重挂")
                self._refresh_orders()

        # BUY 成交或被撤销
        for oid in list(buys.keys()):
            if oid in open_ids:
                continue

            info = self.client.get_order_info(self.cfg.symbol, oid)
            status = info.get("orderStatus", "")

            if status == "filled":
                px = float(info.get("avgPrice") or 0)
                self.state["closes"] += 1

                # 撤销其他 BUY 单
                for bid in list(buys.keys()):
                    if bid != oid:
                        try: self.client.cancel(self.cfg.symbol, bid)
                        except: pass

                self.state["pending_buys"] = {}
                save_state(self.state)

                entry = self.state.get("stack_top", 0)
                pnl = (entry - px) * self.POSITION_SZ
                self._notify(f"BUY @{px:.6f} PnL={pnl:+.2f} closes={self.state['closes']}")
                self._refresh_orders()

            elif status == "cancelled":
                # BUY 单被撤销，只删除这个订单，稍后重挂
                buys.pop(oid, None)
                save_state(self.state)
                self.log.info(f"BUY 单 {oid} 被撤销")
                self._refresh_orders()

    def _refresh_orders(self):
        """重挂网格"""
        with self._lock:
            stack_top = self.state.get("stack_top", 0)
            if stack_top <= 0:
                return

            n_pos = max(0, self.state.get("opens", 0) - self.state.get("closes", 0))
            
            # SELL
            sell_px = self._round_px(stack_top * (1 + self.GRID_PCT))
            cur_sell_id = self.state.get("pending_sell_ord_id")
            cur_sell_px = self.state.get("pending_sell_px")
            
            if not cur_sell_id or cur_sell_px is None or abs(cur_sell_px - sell_px) > 1e-9:
                if cur_sell_id:
                    try: self.client.cancel(self.cfg.symbol, cur_sell_id)
                    except: pass
                self._place_sell(sell_px)

            # BUY 梯队
            depth = min(n_pos, self.MAX_BUYS)
            desired = []
            for i in range(1, depth + 1):
                tpx = self._round_px(stack_top * ((1 - self.GRID_PCT) ** i))
                epx = stack_top * ((1 - self.GRID_PCT) ** (i - 1))
                desired.append((tpx, epx))

            pending = self.state.get("pending_buys", {})
            cur_pxs = [float(v.get("target_px", 0)) for v in pending.values()]
            tol = max(1e-9, stack_top * self.GRID_PCT * 0.4)

            # 收集要撤销的订单
            to_cancel = []
            for oid, info in pending.items():
                if not any(abs(float(info.get("target_px", 0)) - d[0]) < tol for d in desired):
                    to_cancel.append(oid)

            # 撤销
            for oid in to_cancel:
                try: self.client.cancel(self.cfg.symbol, oid)
                except: pass
                pending.pop(oid, None)

            # 挂缺少的（排除已撤销的）
            current_pxs = [float(v.get("target_px", 0)) for v in pending.values()]
            for tpx, epx in desired:
                if not any(abs(tpx - c) < tol for c in current_pxs):
                    self._place_buy(epx, tpx)

            save_state(self.state)

    def _place_sell(self, px: float):
        """挂SELL"""
        # 风控检查
        if not self.dry_run:
            try:
                acc = self.client.get_account()
                eq = float(acc["data"][0].get("accountEquity", "0"))
                if eq < 100:
                    self.log.warning("权益不足，跳过挂单")
                    return
                notional = self.POSITION_SZ * px
                if notional > self.cfg.max_notional_usdt:
                    self.log.warning(f"单笔超限 {notional:.2f} > {self.cfg.max_notional_usdt}")
                    return
            except Exception as e:
                self.log.debug(f"风控检查异常: {e}")

        try:
            resp = self.client.place_order(
                self.cfg.symbol, "sell", self.POSITION_SZ, px,
                order_type="limit", cl_ord_id=_gen_cl_ord_id("S")
            )
            if resp.get("code") == "00000":
                oid = resp.get("data", {}).get("orderId")
                self.state["pending_sell_ord_id"] = oid
                self.state["pending_sell_px"] = px
                save_state(self.state)
            else:
                msg = resp.get('msg', '未知错误')
                self.log.warning(f"SELL 被拒 ({resp.get('code')}): {msg}")
                # 如果是价格穿过的错误（如 50016），降级市价
                if "50016" in str(resp.get('code')):
                    self.log.info("检测到价格穿过，降级市价加仓")
                    self._market_add()
        except Exception as e:
            self.log.error(f"SELL 异常: {e}")

    def _place_buy(self, entry: float, px: float):
        """挂BUY"""
        try:
            resp = self.client.place_order(self.cfg.symbol, "buy", self.POSITION_SZ, px, order_type="limit", reduce_only=True, cl_ord_id=_gen_cl_ord_id("B"))
            if resp.get("code") == "00000":
                oid = resp.get("data", {}).get("orderId")
                if oid:
                    # 确保 pending_buys 存在
                    if "pending_buys" not in self.state:
                        self.state["pending_buys"] = {}
                    self.state["pending_buys"][oid] = {"entry_px": entry, "target_px": px}
        except Exception as e:
            self.log.debug(f"BUY 下单失败: {e}")

    def _market_add(self):
        """市价加仓"""
        px = self.client.get_price(self.cfg.symbol) or self.state.get("stack_top", 0)
        try:
            resp = self.client.place_order(self.cfg.symbol, "sell", self.POSITION_SZ, 0.0, order_type="market")
            if resp.get("code") == "00000":
                self.state["stack_top"] = px
                self.state["opens"] += 1
                save_state(self.state)
                self._refresh_orders()
        except: pass

    def _round_px(self, px: float) -> float:
        """取整价格"""
        try:
            place = int(self.contract_info.get("pricePlace", 4))
        except:
            place = 4
        return float(quantize_price(to_decimal(px), place))


def main():
    cfg = parse_args()
    Strategy(cfg).run()


if __name__ == "__main__":
    main()
