"""栈式金字塔做空策略 (Short Pyramid)

策略逻辑：
  - 启动 → 市价空 1 张，stack_top = 入场价
  - 1 个 SELL (加空) @ stack_top × (1+grid)
  - N 个 BUY (平空) @ stack_top × (1-grid)^i
  - SELL 成交 → stack_top 更新 → 重挂网格
  - BUY 成交 → closes += 1 → 重挂网格
  - 定时对账：30s 检查一次本地 vs 交易所持仓
"""
import sys
import time
import signal
import logging
import threading
from typing import Dict, Any

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
        self.client = BitgetClient(
            cfg.api_key, cfg.secret_key, cfg.passphrase,
            self.log, dry_run=self.dry_run,
        )
        self.state: State = load_state(cfg.symbol)

        # 策略参数
        self.GRID_PCT = cfg.grid_pct
        self.POSITION_SZ = cfg.size
        self.MAX_BUYS = 60
        self.RECONCILE_INTERVAL = 30  # 定时对账间隔（秒）

        # 合约信息
        self.contract_info: dict = {}

        # 时间戳追踪
        self.last_reconcile_ts = 0.0

        # 线程锁
        self._lock = threading.RLock()
        self._running = True

        signal.signal(signal.SIGINT, self._handle_exit)

    def _handle_exit(self, sig, frame):
        self.log.info("收到退出信号，保存 state 后退出...")
        self._notify("🛑 策略停止", "收到退出信号，正常退出", "WARNING")
        self._running = False

    def _notify(self, event: str, details: str, level: str = "INFO"):
        """P2: 统一通知入口（控制台 + Telegram）"""
        icon = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "ERROR": "❌",
            "TRADE": "💹",
            "SUCCESS": "✅"
        }
        msg = f"<b>{icon.get(level, '')} {self.cfg.symbol}</b>\n{event}\n{details}"

        # 控制台/文件日志
        log_msg = msg.replace("<b>", "").replace("</b>", "")
        if level == "ERROR":
            self.log.error(log_msg)
        elif level == "WARNING":
            self.log.warning(log_msg)
        else:
            self.log.info(log_msg)

        # Telegram（仅实盘模式）
        if not self.dry_run and self.cfg.tg_bot_token:
            send_telegram(msg, self.cfg.tg_bot_token, self.cfg.tg_chat_id, self.log)

    # ====== 初始化 ======

    def init(self):
        """初始化策略 - 阶段化流程"""
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        self.log.info(f"=== 初始化 {self.cfg.symbol} [{mode}] ===")

        # 阶段 1: 获取合约信息
        self.contract_info = self.client.get_contracts(self.cfg.symbol) or {}
        if not self.contract_info:
            self.contract_info = {
                "symbolStatus": "normal",
                "sizeMultiplier": 1,
                "volumePlace": 0,
                "minTradeNum": 1,
                "minTradeUSDT": 10,
            }

        # 阶段 2: 账户配置
        if not self.dry_run:
            try:
                self.client.set_hold_mode("one_way_mode")
                self.client.set_leverage(self.cfg.symbol, self.cfg.leverage)
            except Exception as e:
                self.log.warning(f"账户配置异常: {e}")

        # 阶段 3: 验证合约参数
        self._validate_contract()

        # 阶段 4: 清理遗留挂单
        if not self.dry_run:
            self._cancel_all_exchange_orders()
            time.sleep(0.5)

        # 阶段 5: 持仓接管或起仓
        if self.state.get("stack_top", 0) <= 0:
            if not self._adopt_existing_position():
                self._open_initial()

        # 阶段 6: 挂初始网格
        if self.state.get("stack_top", 0) > 0:
            self._refresh_orders(reason="startup")

        self._notify(
            "🚀 策略启动",
            f"网格={self.GRID_PCT*100:.2f}% | 每单={self.POSITION_SZ}张 | 持仓={self._n_positions()}单",
            "SUCCESS"
        )
        self.log.info(f"启动完成: {mode} | stack_top={self.state.get('stack_top', 0):.6f}")

    def _cancel_all_exchange_orders(self):
        """启动时清理遗留挂单"""
        if self.dry_run:
            return
        try:
            open_orders = self.client.get_open_orders(self.cfg.symbol)
            for order in open_orders:
                try:
                    self.client.cancel(self.cfg.symbol, order.get("orderId"))
                except Exception:
                    pass
            if open_orders:
                self.log.info(f"撤销 {len(open_orders)} 笔遗留挂单")
        except Exception as e:
            self.log.warning(f"清理挂单异常: {e}")

    def _validate_contract(self):
        """验证合约参数"""
        if self.dry_run:
            return
        try:
            mark = self.client.get_price(self.cfg.symbol)
            if mark <= 0:
                return
            size = to_decimal(self.cfg.size)
            ok, msg = validate_order_size(size, to_decimal(mark), self.contract_info)
            if not ok:
                self.log.error(f"size 验证失败: {msg}")
                sys.exit(1)
        except Exception as e:
            self.log.warning(f"合约验证异常: {e}")

    def _n_positions(self) -> int:
        """当前持仓张数 = opens - closes"""
        return max(0, self.state.get("opens", 0) - self.state.get("closes", 0))

    def _check_margin_ratio(self) -> bool:
        """检查保证金率（对应 OKX MIN_MARGIN_RATIO）

        Bitget UTA v3 无直接 marginRatio 字段，用账户权益作为代理指标。
        账户权益 < 100 USDT 时禁止加仓，防止小资金过度杠杆。
        """
        if self.dry_run:
            return True
        try:
            account = self.client.get_account()
            equity_str = account["data"][0].get("accountEquity", "0")
            equity = float(equity_str)
            if equity < 100:
                self.log.error(f"[风控] 账户权益 {equity:.2f} < 100 USDT，禁止加仓")
                return False
            return True
        except Exception as e:
            self.log.warning(f"[风控] 查询账户失败: {e}，跳过本轮加仓")
            return False

    def _check_position_limit(self, current_price: float) -> bool:
        """检查单笔持仓名义价值（对应 OKX MAX_NOTIONAL_USDT）"""
        notional = self.POSITION_SZ * current_price
        if notional > self.cfg.max_notional_usdt:
            self.log.error(
                f"[风控] 单笔价值 {notional:.2f} > "
                f"{self.cfg.max_notional_usdt} USDT，禁止加仓"
            )
            return False
        return True

    def _round_px(self, px: float) -> float:
        """按合约 pricePlace 取整价格（避免精度超限被拒单）"""
        try:
            place = int(self.contract_info.get("pricePlace", 4))
        except (TypeError, ValueError):
            place = 4
        return float(quantize_price(to_decimal(px), place))

    # ====== 起仓 ======

    def _adopt_existing_position(self) -> bool:
        """接管已有持仓（若有）"""
        if self.dry_run:
            return False
        try:
            resp = self.client.get_position(self.cfg.symbol)
            for p in resp.get("data", []):
                if p.get("holdSide") != "short":
                    continue
                total = float(p.get("total") or 0)
                avg_px = float(p.get("openPriceAvg") or 0)
                if total <= 0 or avg_px <= 0:
                    continue
                opens = max(1, int(total / float(self.POSITION_SZ)))
                self.state["stack_top"] = avg_px
                self.state["opens"] = opens
                self.state["closes"] = 0
                self.state["last_action_time"] = time.time()
                save_state(self.state)
                self.log.info(f"接管持仓: {total}张 @ {avg_px:.6f}")
                return True
        except Exception as e:
            self.log.warning(f"接管异常: {e}")
        return False

    def _open_initial(self):
        """起仓：第一单市价空"""
        self.log.info(f"起仓市价空 {self.POSITION_SZ} 张")
        try:
            resp = self.client.place_order(
                self.cfg.symbol, "sell", self.POSITION_SZ, 0.0,
                order_type="market"
            )
            if resp.get("code") != "00000":
                self.log.error(f"起仓失败: {resp.get('msg')}")
                sys.exit(1)

            # 获取成交价
            if self.dry_run:
                fill_px = 1.76
            else:
                fill_px = self.client.get_price(self.cfg.symbol)
                if fill_px <= 0:
                    try:
                        pos = self.client.get_position(self.cfg.symbol)
                        for p in pos.get("data", []):
                            if p.get("holdSide") == "short":
                                fill_px = float(p.get("openPriceAvg") or 0)
                                break
                    except Exception:
                        pass

            if fill_px <= 0:
                self.log.error("无法获取成交价")
                sys.exit(1)

            self.state["stack_top"] = fill_px
            self.state["opens"] = 1
            self.state["last_action_time"] = time.time()
            save_state(self.state)
            self.log.info(f"起仓成功 @ {fill_px:.6f}")
        except Exception as e:
            self.log.error(f"起仓异常: {e}")
            sys.exit(1)

    # ====== 主循环 ======

    def run(self):
        """策略主循环 - 事件检查 + 定时对账"""
        self.init()
        self.log.info(f"策略主循环启动, 轮询间隔={self.cfg.interval}s")

        while self._running:
            try:
                self._tick()
            except Exception as e:
                self.log.error(f"策略异常: {e}", exc_info=True)
            time.sleep(self.cfg.interval)

        save_state(self.state)
        self.log.info("策略已安全退出")

    def _tick(self):
        """主循环：订单检查 + 定时对账"""
        with self._lock:
            # 检查订单填充（实时）
            if not self.dry_run:
                self._check_fills()

            # 定时对账（每 RECONCILE_INTERVAL 秒）
            now = time.time()
            if now - self.last_reconcile_ts >= self.RECONCILE_INTERVAL:
                self._reconcile()
                self.last_reconcile_ts = now

    def _reconcile(self):
        """定时对账：本地栈 vs 交易所实际持仓"""
        if self.dry_run:
            return

        try:
            resp = self.client.get_position(self.cfg.symbol)
            okx_size = 0.0
            for p in resp.get("data", []):
                if p.get("holdSide") == "short":
                    try:
                        okx_size = float(p.get("total") or 0)
                        break
                    except (TypeError, ValueError):
                        pass

            # 本地预期持仓
            local_size = (self.state.get("opens", 0) - self.state.get("closes", 0)) * self.POSITION_SZ

            # 容差：< 1 张认为一致
            diff = abs(okx_size - local_size)
            if diff < self.POSITION_SZ - 1e-9:
                return  # 一致，无需动作

            # 持仓不一致 → 自动修复（以交易所为准）
            if okx_size < local_size:
                # 本地多 → closes 增加
                n_closed = round((local_size - okx_size) / self.POSITION_SZ)
                self.state["closes"] += n_closed
                self.log.warning(f"[对账] 本地多 {n_closed} 张，closes += {n_closed}")
            else:
                # 本地少 → opens 增加
                n_added = round((okx_size - local_size) / self.POSITION_SZ)
                self.state["opens"] += n_added
                self.log.warning(f"[对账] 本地少 {n_added} 张，opens += {n_added}")

            save_state(self.state)
            # 重挂网格确保完整
            self._refresh_orders_locked(reason="reconcile")

        except Exception as e:
            self.log.warning(f"[对账] 异常: {e}")

    def _check_fills(self):
        """轮询订单成交状态"""
        sell_id = self.state.get("pending_sell_ord_id")
        pending_buys = self.state.get("pending_buys", {})
        if not sell_id and not pending_buys:
            return

        try:
            open_ids = {o.get("orderId") for o in self.client.get_open_orders(self.cfg.symbol)}
        except Exception:
            return

        # SELL 单成交检测
        if sell_id and sell_id not in open_ids:
            info = self.client.get_order_info(self.cfg.symbol, sell_id)
            status = info.get("orderStatus", "")
            if status == "filled":
                fill_px = float(info.get("avgPrice") or 0) or self.state.get("pending_sell_px") or 0.0
                self.state["pending_sell_ord_id"] = None
                self.state["pending_sell_px"] = None
                self.state["stack_top"] = fill_px
                self.state["opens"] += 1
                self.state["last_action_time"] = time.time()
                save_state(self.state)
                self.log.info(f"SELL成交 @ {fill_px:.6f}")
                self._notify("🔻 SELL成交", f"opens={self.state['opens']}", "TRADE")
                self._refresh_orders_locked(reason="fill_sell")
            elif status == "cancelled":
                self.state["pending_sell_ord_id"] = None
                self.state["pending_sell_px"] = None
                save_state(self.state)
                self._refresh_orders_locked(reason="sell_cancelled")

        # BUY 单成交检测
        for ord_id in list(pending_buys.keys()):
            if ord_id in open_ids:
                continue
            info = self.client.get_order_info(self.cfg.symbol, ord_id)
            status = info.get("orderStatus", "")
            if status == "filled":
                fill_px = float(info.get("avgPrice") or 0)
                self.state["closes"] += 1
                self.state["last_action_time"] = time.time()

                # 撤销所有旧 BUY 单
                for buy_id in list(pending_buys.keys()):
                    try:
                        self.client.cancel(self.cfg.symbol, buy_id)
                    except:
                        pass
                self.state["pending_buys"] = {}
                save_state(self.state)

                entry_px = float(info.get("avgPrice") or self.state.get("stack_top", 0))
                pnl = (entry_px - fill_px) * self.POSITION_SZ
                self.log.info(f"BUY成交 @ {fill_px:.6f} | 盈亏={pnl:+.2f}")
                self._notify("🔺 BUY成交", f"closes={self.state['closes']} | PnL={pnl:+.2f}", "TRADE")
                self._refresh_orders_locked(reason="fill_buy")
            elif status == "cancelled":
                pending_buys.pop(ord_id, None)
                save_state(self.state)
                self._refresh_orders_locked(reason="buy_cancelled")

    # ====== 核心: 重挂 SELL + 多个 BUY ======

    def _refresh_orders(self, reason: str = ""):
        """重挂目标: 1 个 SELL + min(持仓数, MAX_BUYS) 个 BUY"""
        with self._lock:
            self._refresh_orders_locked(reason)

    def _refresh_orders_locked(self, reason: str):
        """重挂网格：1 个 SELL + min(持仓数, MAX_BUYS) 个 BUY"""
        stack_top = self.state.get("stack_top", 0.0)
        if stack_top <= 0:
            self._cancel_all_pending()
            return

        n_positions = self._n_positions()

        # SELL 目标价
        desired_sell_px = self._round_px(stack_top * (1 + self.GRID_PCT))
        cur_sell_px = self.state.get("pending_sell_px")
        cur_sell_id = self.state.get("pending_sell_ord_id")

        if not cur_sell_id or cur_sell_px is None or abs(cur_sell_px - desired_sell_px) > 1e-9:
            if cur_sell_id:
                try:
                    self.client.cancel(self.cfg.symbol, cur_sell_id)
                except:
                    pass
                self.state["pending_sell_ord_id"] = None
                self.state["pending_sell_px"] = None
            self._place_limit_sell(desired_sell_px, reason)

        # BUY 梯队
        depth = min(n_positions, self.MAX_BUYS)
        desired_buys = []
        for i in range(1, depth + 1):
            target_px = self._round_px(stack_top * ((1 - self.GRID_PCT) ** i))
            entry_px = stack_top * ((1 - self.GRID_PCT) ** (i - 1))
            desired_buys.append((target_px, entry_px))

        pending_buys = self.state.get("pending_buys", {})
        current_buy_pxs = [float(v.get("target_px", 0)) for v in pending_buys.values()]
        match_tol = max(1e-9, stack_top * self.GRID_PCT * 0.4)

        # 撤销多余的 BUY
        to_cancel = [
            ord_id for ord_id, info in pending_buys.items()
            if not any(abs(float(info.get("target_px", 0)) - d[0]) < match_tol for d in desired_buys)
        ]
        for ord_id in to_cancel:
            try:
                self.client.cancel(self.cfg.symbol, ord_id)
            except:
                pass
            pending_buys.pop(ord_id, None)

        # 挂缺少的 BUY
        for target_px, entry_px in desired_buys:
            if not any(abs(target_px - current) < match_tol for current in current_buy_pxs):
                self._place_limit_buy(entry_px, target_px)

        self.state["last_refresh_ts"] = time.time()
        save_state(self.state)

    def _place_limit_sell(self, target_px: float, reason: str):
        """挂 SELL 限价单（加仓），带风控检查"""
        if not self._check_margin_ratio():
            self.log.warning("保证金不足，拒加仓")
            return
        if not self._check_position_limit(target_px):
            self.log.warning("单笔超限，拒加仓")
            return

        try:
            resp = self.client.place_order(
                self.cfg.symbol, "sell", self.POSITION_SZ, target_px,
                order_type="limit", reduce_only=False,
                cl_ord_id=_gen_cl_ord_id("spSELL")
            )
            if resp.get("code") == "00000":
                ord_id = resp.get("data", {}).get("orderId")
                self.state["pending_sell_ord_id"] = ord_id
                self.state["pending_sell_px"] = target_px
            else:
                self.log.warning(f"SELL被拒: {resp.get('msg')}")
                self._market_add()
        except Exception as e:
            self.log.error(f"SELL异常: {e}")

    def _place_limit_buy(self, entry_px: float, target_px: float):
        """挂 BUY 限价单（平仓），只减仓"""
        try:
            resp = self.client.place_order(
                self.cfg.symbol, "buy", self.POSITION_SZ, target_px,
                order_type="limit", reduce_only=True,
                cl_ord_id=_gen_cl_ord_id("spBUY")
            )
            if resp.get("code") == "00000":
                ord_id = resp.get("data", {}).get("orderId")
                if ord_id:
                    self.state["pending_buys"][ord_id] = {
                        "entry_px": entry_px,
                        "target_px": target_px,
                    }
        except Exception as e:
            self.log.warning(f"BUY异常: {e}")

    def _market_add(self):
        """市价加仓（限价被拒时的降级）"""
        cur_px = self.client.get_price(self.cfg.symbol)
        check_px = cur_px or self.state.get("stack_top", 0.0)
        if not self._check_position_limit(check_px):
            return

        try:
            resp = self.client.place_order(
                self.cfg.symbol, "sell", self.POSITION_SZ, 0.0,
                order_type="market"
            )
            if resp.get("code") == "00000":
                self.state["stack_top"] = check_px
                self.state["opens"] += 1
                self.state["last_action_time"] = time.time()
                save_state(self.state)
                self._refresh_orders(reason="market_add")
        except Exception as e:
            self.log.warning(f"市价加仓失败: {e}")

    def _cancel_all_pending(self):
        """撤销所有待成交挂单"""
        sell_id = self.state.get("pending_sell_ord_id")
        if sell_id:
            try:
                self.client.cancel(self.cfg.symbol, sell_id)
            except:
                pass
            self.state["pending_sell_ord_id"] = None
            self.state["pending_sell_px"] = None

        for ord_id in self.state.get("pending_buys", {}):
            try:
                self.client.cancel(self.cfg.symbol, ord_id)
            except:
                pass
        self.state["pending_buys"] = {}
        save_state(self.state)


def main():
    cfg = parse_args()

    if cfg.mode == "live":
        print("=" * 60)
        print("  *** 风险提示 ***")
        print("  合约做空可能爆仓，本程序不保证盈利。")
        print("  请确保已充分了解风险后再运行。")
        print("  API Key 请勿泄露，建议只开读取+交易权限，不开提现。")
        print("=" * 60)
        confirm = input("确认运行 live 模式？请输入 yes 继续: ")
        if confirm.strip().lower() != "yes":
            print("已取消。")
            sys.exit(0)

    strategy = Strategy(cfg)
    strategy.run()


if __name__ == "__main__":
    main()
