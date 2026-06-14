"""账户监控器 - 权益曲线 + 风控告警

功能：
  - 定时快照：每 N 秒拉取账户信息，记录到 CSV
  - 权益曲线：Excel 可视化用
  - 风控告警：保证金率、单笔持仓、浮亏等
  - Telegram 通知：关键告警推送
  - 心跳信号：确保监控在线

使用：
  python -m bitget_short_pyramid.monitor --symbol BGBUSDT --interval 60
  或
  python monitor.py
"""
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import parse_args, Config
from .client import BitgetClient
from .logger import setup_logger
from .utils import send_telegram


@dataclass
class MonitorConfig:
    """监控配置"""
    symbol: str = "BGBUSDT"
    interval_sec: int = 60  # 拉快照间隔

    # 保证金率告警（倍数）
    mgn_warn: float = 7.0   # 700%
    mgn_critical: float = 5.0  # 500%
    mgn_emergency: float = 3.0  # 300%

    # 持仓告警
    max_single_position_usdt: float = 10000  # 单笔上限

    # 权益曲线记录
    csv_path: str = ""

    # 心跳（秒）
    heartbeat_sec: int = 3600  # 1小时

    # 告警节流
    throttle_sec: int = 600  # 10分钟内同一告警最多1次


class AccountMonitor:
    def __init__(self, cfg: Config, monitor_cfg: Optional[MonitorConfig] = None):
        self.cfg = cfg
        self.monitor_cfg = monitor_cfg or MonitorConfig(symbol=cfg.symbol)
        self.log = setup_logger(f"monitor_{cfg.symbol}")
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log)

        # CSV 路径
        if not self.monitor_cfg.csv_path:
            self.monitor_cfg.csv_path = f"equity_monitor_{cfg.symbol}.csv"

        # 告警节流（避免重复推送）
        self._last_alerts = {}  # {key: timestamp}

        # 统计
        self._running = True
        self._start_ts = time.time()
        self._tick_count = 0
        self._last_heartbeat = 0.0

        # 初始化 CSV
        self._init_csv()

    def _init_csv(self):
        """初始化 CSV 文件"""
        path = Path(self.monitor_cfg.csv_path)
        if path.exists() and path.stat().st_size > 0:
            return

        with path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "timestamp", "datetime", "account_equity", "available_balance",
                "margin_ratio", "positions_count", "alert"
            ])
        self.log.info(f"CSV 初始化: {self.monitor_cfg.csv_path}")

    def run(self):
        """主循环"""
        mode = "DRY" if self.cfg.mode == "dry-run" else "LIVE"
        self.log.info(f"监控启动 [{mode}] symbol={self.cfg.symbol} interval={self.monitor_cfg.interval_sec}s")

        self._notify("监控启动", f"模式={mode} | 保证金率告警阈值: {self.monitor_cfg.mgn_warn*100:.0f}% / {self.monitor_cfg.mgn_critical*100:.0f}%")

        while self._running:
            try:
                self.tick()
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.log.error(f"监控异常: {e}", exc_info=True)
                self._notify_alert("监控异常", str(e))

            time.sleep(self.monitor_cfg.interval_sec)

        self.log.info("监控已停止")
        self._notify("监控停止", f"运行时长: {(time.time()-self._start_ts)/3600:.1f}h")

    def tick(self):
        """单次扫描"""
        if self.cfg.mode == "dry-run":
            return

        self._tick_count += 1

        try:
            account = self.client.get_account()
            equity = float(account["data"][0].get("accountEquity", "0"))
            available = float(account["data"][0].get("available", "0"))

            # 查询持仓
            pos_resp = self.client.get_position(self.cfg.symbol)
            positions = pos_resp.get("data", [])
            n_pos = sum(1 for p in positions if float(p.get("total", 0)) > 0)

            # 保证金率
            if len(positions) > 0 and positions[0].get("mmr"):
                mmr = float(positions[0].get("mmr", 0))
                equity_safe = equity if equity > 0 else 1
                mgn_ratio = equity_safe / mmr if mmr > 0 else float('inf')
            else:
                mgn_ratio = float('inf')

            # 记录到 CSV
            self._log_equity(equity, available, mgn_ratio, n_pos)

            # 检查告警
            self._check_alerts(equity, available, mgn_ratio, n_pos, positions)

            # 心跳
            self._maybe_heartbeat(equity, mgn_ratio, n_pos)

        except Exception as e:
            self.log.warning(f"查询账户失败: {e}")

    def _check_alerts(self, equity: float, available: float, mgn_ratio: float, n_pos: int, positions: list):
        """检查风控告警"""
        # 保证金率告警
        if mgn_ratio != float('inf'):
            if mgn_ratio < self.monitor_cfg.mgn_emergency:
                self._alert("mgn_emergency",
                    f"🚨 保证金率紧急: {mgn_ratio*100:.1f}% < {self.monitor_cfg.mgn_emergency*100:.0f}%\n"
                    f"权益: {equity:.2f} USDT | 强烈建议立即减仓")
            elif mgn_ratio < self.monitor_cfg.mgn_critical:
                self._alert("mgn_critical",
                    f"⚠️ 保证金率严重: {mgn_ratio*100:.1f}% < {self.monitor_cfg.mgn_critical*100:.0f}%\n"
                    f"权益: {equity:.2f} USDT | 仅允许平仓")
            elif mgn_ratio < self.monitor_cfg.mgn_warn:
                self._alert("mgn_warn",
                    f"⚡ 保证金率警告: {mgn_ratio*100:.1f}% < {self.monitor_cfg.mgn_warn*100:.0f}%\n"
                    f"权益: {equity:.2f} USDT | 建议减仓")

        # 单笔持仓超限
        current_price = self.client.get_price(self.cfg.symbol)
        for p in positions:
            total = float(p.get("total", 0))
            if total > 0:
                notional = total * current_price
                if notional > self.monitor_cfg.max_single_position_usdt:
                    self._alert(f"position_limit_{self.cfg.symbol}",
                        f"单笔超限: {notional:.2f} USDT > {self.monitor_cfg.max_single_position_usdt}\n"
                        f"持仓: {total}张 @ {current_price:.6f}")

    def _maybe_heartbeat(self, equity: float, mgn_ratio: float, n_pos: int):
        """定时心跳"""
        if self.monitor_cfg.heartbeat_sec <= 0:
            return

        now = time.time()
        if now - self._last_heartbeat < self.monitor_cfg.heartbeat_sec:
            return

        self._last_heartbeat = now
        uptime_hr = (now - self._start_ts) / 3600

        msg = (
            f"💓 心跳\n"
            f"运行: {uptime_hr:.1f}h | tick #{self._tick_count}\n"
            f"权益: {equity:.2f} USDT\n"
            f"保证金率: {mgn_ratio*100:.0f}%\n"
            f"持仓: {n_pos}个"
        )
        self._notify("心跳", msg)

    def _log_equity(self, equity: float, available: float, mgn_ratio: float, n_pos: int):
        """记录权益曲线"""
        ts = time.time()
        dt = datetime.fromtimestamp(ts).isoformat(timespec="seconds")

        with open(self.monitor_cfg.csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                f"{ts:.3f}", dt, f"{equity:.4f}", f"{available:.4f}",
                f"{mgn_ratio:.6f}", n_pos, ""
            ])

    def _alert(self, key: str, msg: str):
        """告警（带节流）"""
        now = time.time()
        last = self._last_alerts.get(key, 0)

        if now - last < self.monitor_cfg.throttle_sec:
            return  # 节流

        self._last_alerts[key] = now
        self._notify_alert(key, msg)

    def _notify(self, event: str, details: str):
        """普通通知"""
        msg = f"<b>{self.cfg.symbol}</b>\n{event}\n{details}"
        self.log.info(f"{event} | {details}")

        if self.cfg.tg_bot_token:
            send_telegram(msg, self.cfg.tg_bot_token, self.cfg.tg_chat_id)

    def _notify_alert(self, alert_type: str, details: str):
        """告警通知"""
        msg = f"<b>{self.cfg.symbol}</b>\n{details}"
        self.log.warning(f"[ALERT] {alert_type} | {details}")

        if self.cfg.tg_bot_token:
            send_telegram(msg, self.cfg.tg_bot_token, self.cfg.tg_chat_id)


def main():
    """CLI 入口"""
    cfg = parse_args()

    monitor_cfg = MonitorConfig(
        symbol=cfg.symbol,
        interval_sec=cfg.interval,
        csv_path=f"equity_monitor_{cfg.symbol}.csv",
        heartbeat_sec=3600,
    )

    monitor = AccountMonitor(cfg, monitor_cfg)
    try:
        monitor.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
