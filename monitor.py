"""账户监控器 - 保证金率分级告警 + 挂单形态探针 + 权益曲线 CSV

工作模式：REST 每 60s 拉一次账户/持仓快照，跑全量检查（兜底，无 WS）。
检查维度：
  ① 保证金率分级（信息/警告/严重/紧急）
  ② 资金费率（空头持仓在负费率时被吃利息）
  ③ 挂单形态探针（策略存活检测）：每个空头持仓应有「恰好 1 个 SELL + 至少 1 个 BUY」，
     连续 N 次 tick 不满足 → 策略可能已停，告警
  ④ 权益曲线写入 equity_log.csv
  ⑤ 每小时心跳

用法: python monitor.py   （Ctrl+C 退出，建议 nohup/systemd 长驻）
"""
import time
import os
import csv
import logging
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

from config import parse_monitor_args
from client import BitgetClient


def _setup_logger(name: str):
    """创建日志记录器"""
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join("logs", f"{name}_{timestamp}.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


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


class AccountMonitor:
    # 保证金率分级（倍数，5.0 = 500%）
    MGN_INFO = 10.0        # 1000% 以下仅日志
    MGN_WARN = 7.0         # 700% 警告
    MGN_CRITICAL = 5.0     # 500% 严重（= 策略停止加仓阈值）
    MGN_EMERGENCY = 3.0    # 300% 紧急（建议手动减仓）

    # 资金费率（8h 周期）：空头持仓在负费率时被吃利息
    FUNDING_WARN = 0.001       # 0.1% / 8h ≈ 年化 110%
    FUNDING_CRITICAL = 0.002   # 0.2% / 8h ≈ 年化 219%

    INTERVAL_SEC = 60
    HEARTBEAT_SEC = 3600              # 每小时一条心跳
    ORDER_SHAPE_CONSECUTIVE = 3       # 挂单形态连续异常 N 次才告警
    THROTTLE_MGN = 600                # 保证金率告警节流 10 分钟
    THROTTLE_SHAPE = 1800             # 挂单形态告警节流 30 分钟
    THROTTLE_FUNDING = 3600           # 资金费率告警节流 1 小时
    EQUITY_CSV = "equity_log.csv"

    def __init__(self, cfg):
        self.cfg = cfg
        self.log = _setup_logger("monitor")
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log)
        self._last_alerts = {}
        self._start_ts = time.time()
        self._last_heartbeat = 0.0
        self._tick_count = 0
        self._order_bad_streak = {}   # symbol -> 连续异常次数

    # ------------------------------------------------------------------
    def run(self):
        """主循环"""
        self.log.info("监控启动")
        self._send_msg("🟢 监控启动")
        self._ensure_csv_header()
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:
                    self.log.error(f"tick 失败: {e}", exc_info=True)
                time.sleep(self.INTERVAL_SEC)
        except KeyboardInterrupt:
            self.log.info("监控已停止")

    def tick(self):
        """单次扫描"""
        self._tick_count += 1

        acct = self.client.get_account()
        if acct.get("code") != "00000":
            self.log.warning(f"查询账户失败: {acct.get('msg', 'unknown')}")
            return
        equity = float(acct.get("data", [{}])[0].get("accountEquity") or 0)

        pos_resp = self.client.get_position("")
        if pos_resp.get("code") != "00000":
            self.log.warning(f"查询持仓失败: {pos_resp.get('msg', 'unknown')}")
            return
        positions = [p for p in pos_resp.get("data", []) if float(p.get("total") or 0) != 0]

        # 保证金率 = 权益 / 维持保证金总和
        total_mmr = sum(float(p.get("mmr") or 0) for p in positions)
        mgn_ratio = equity / total_mmr if total_mmr > 0 else float('inf')

        self._log_equity(equity, mgn_ratio, total_mmr, len(positions))
        self._check_margin(equity, mgn_ratio)
        self._check_funding(positions)
        self._check_order_shape(positions)
        self._maybe_heartbeat(equity, mgn_ratio, len(positions))

    # ------------------------------------------------------------------
    # ① 保证金率分级
    # ------------------------------------------------------------------
    def _check_margin(self, equity: float, mgn_ratio: float):
        if mgn_ratio == float('inf') or mgn_ratio <= 0:
            return  # 无持仓

        pct = mgn_ratio * 100
        if mgn_ratio < self.MGN_EMERGENCY:
            self._alert("mgn_emergency",
                        f"🚨 保证金率紧急: {pct:.0f}% < {self.MGN_EMERGENCY*100:.0f}%\n"
                        f"强烈建议立即手动减仓！权益: {equity:.2f} USDT",
                        self.THROTTLE_MGN)
        elif mgn_ratio < self.MGN_CRITICAL:
            self._alert("mgn_critical",
                        f"⚠️ 保证金率严重: {pct:.0f}% < {self.MGN_CRITICAL*100:.0f}%\n"
                        f"策略已停止加仓，仅允许平仓 | 权益: {equity:.2f}",
                        self.THROTTLE_MGN)
        elif mgn_ratio < self.MGN_WARN:
            self._alert("mgn_warn",
                        f"⚡ 保证金率警告: {pct:.0f}% < {self.MGN_WARN*100:.0f}%\n"
                        f"考虑减仓或追加保证金 | 权益: {equity:.2f}",
                        self.THROTTLE_MGN)
        elif mgn_ratio < self.MGN_INFO:
            self.log.info(f"保证金率 {pct:.0f}% (info)")

    # ------------------------------------------------------------------
    # ② 资金费率（空头持仓在负费率时被吃利息）
    # ------------------------------------------------------------------
    def _check_funding(self, positions: list):
        for p in positions:
            sym = p.get("symbol")
            if not sym:
                continue
            fr = self.client.get_funding_rate(sym)
            # 做空：负费率时空头支付（被吃）；做多：正费率时被吃
            is_short = p.get("holdSide") == "short"
            paying = (is_short and fr < 0) or (not is_short and fr > 0)
            if not paying:
                continue

            abs_fr = abs(fr)
            if abs_fr >= self.FUNDING_CRITICAL:
                tag = "🔴"
            elif abs_fr >= self.FUNDING_WARN:
                tag = "⚠️"
            else:
                continue

            annualized = abs_fr * 3 * 365 * 100
            self._alert(f"funding_{sym}",
                        f"{tag} 资金费率高: {sym}\n"
                        f"当前 {fr*100:+.4f}% / 8h（年化约 {annualized:.0f}%）\n"
                        f"空头正在支付资金费，考虑减仓或对冲",
                        self.THROTTLE_FUNDING)

    # ------------------------------------------------------------------
    # ③ 挂单形态探针（策略存活检测）
    # ------------------------------------------------------------------
    def _check_order_shape(self, positions: list):
        """每个空头持仓应有「恰好 1 个 SELL + 至少 1 个 BUY」。

        挂单是 GTC，策略进程死了挂单仍留在交易所但不再被移动/补挂，迟早漂移成
        畸形（SELL 成交后无人补 → 0 SELL 等）。用挂单形态当"策略是否还在维护网格"
        的探针。连续 N 次 tick 都异常才告警，过滤 refresh 撤挂空窗等瞬时态。
        """
        if not positions:
            self._order_bad_streak.clear()
            return

        held = {p.get("symbol"): p for p in positions if p.get("symbol")}

        for sym, pos in held.items():
            orders = self.client.get_open_orders(sym)
            sell = sum(1 for o in orders if o.get("side") == "sell")
            buy = sum(1 for o in orders if o.get("side") == "buy")

            if sell == 1 and buy >= 1:        # 形态正常
                self._order_bad_streak.pop(sym, None)
                continue

            streak = self._order_bad_streak.get(sym, 0) + 1
            self._order_bad_streak[sym] = streak
            if streak < self.ORDER_SHAPE_CONSECUTIVE:
                self.log.warning(f"[挂单形态] {sym} 异常 SELL={sell} BUY={buy} "
                                 f"(连续 {streak}/{self.ORDER_SHAPE_CONSECUTIVE}，暂不告警)")
                continue

            mins = streak * self.INTERVAL_SEC // 60
            self._alert(f"shape_{sym}",
                        f"🩺 挂单形态异常: {sym}\n"
                        f"持仓 {pos.get('total')} 张，但挂单 SELL={sell}(应=1) / BUY={buy}(应≥1)\n"
                        f"已连续 {streak} 次异常 (~{mins} 分钟)\n"
                        f"策略可能已停止，请检查服务器进程",
                        self.THROTTLE_SHAPE)

        # 清理已平仓合约的 streak
        for sym in [s for s in self._order_bad_streak if s not in held]:
            self._order_bad_streak.pop(sym, None)

    # ------------------------------------------------------------------
    # ④ 心跳
    # ------------------------------------------------------------------
    def _maybe_heartbeat(self, equity: float, mgn_ratio: float, n_pos: int):
        now = time.time()
        if now - self._last_heartbeat < self.HEARTBEAT_SEC:
            return
        self._last_heartbeat = now
        uptime = (now - self._start_ts) / 3600
        ratio_str = "—" if mgn_ratio == float('inf') else f"{mgn_ratio*100:.0f}%"
        self._send_msg(f"💓 {uptime:.1f}h | tick #{self._tick_count} | "
                       f"权益: {equity:.2f} | 保证金率: {ratio_str} | 持仓: {n_pos}")

    # ------------------------------------------------------------------
    # ⑤ 权益曲线 CSV
    # ------------------------------------------------------------------
    def _ensure_csv_header(self):
        if os.path.exists(self.EQUITY_CSV) and os.path.getsize(self.EQUITY_CSV) > 0:
            return
        with open(self.EQUITY_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["ts", "datetime", "equity", "mgn_ratio", "mmr", "n_positions"])

    def _log_equity(self, equity: float, mgn_ratio: float, mmr: float, n_pos: int):
        ratio = "" if mgn_ratio == float('inf') else f"{mgn_ratio:.4f}"
        with open(self.EQUITY_CSV, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                f"{time.time():.3f}",
                datetime.now().isoformat(timespec="seconds"),
                f"{equity:.4f}", ratio, f"{mmr:.4f}", n_pos,
            ])

    # ------------------------------------------------------------------
    def _alert(self, key: str, msg: str, throttle_sec: int):
        """告警（带节流）"""
        now = time.time()
        if now - self._last_alerts.get(key, 0) < throttle_sec:
            return
        self._last_alerts[key] = now
        self.log.warning(f"[ALERT] {msg}")
        self._send_msg(msg)

    def _send_msg(self, msg: str):
        """发送 Telegram"""
        if self.cfg.tg_bot_token:
            _send_telegram(f"账户监控\n{msg}", self.cfg.tg_bot_token, self.cfg.tg_chat_id)


def main():
    cfg = parse_monitor_args()
    AccountMonitor(cfg).run()


if __name__ == "__main__":
    main()
