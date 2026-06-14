"""账户监控器 - 保证金率告警"""
import time

from config import parse_monitor_args
from client import BitgetClient
from logger import setup_logger
from utils import send_telegram


class AccountMonitor:
    ALERTS = [
        (3.0, "🚨 保证金率紧急"),
        (5.0, "⚠️ 保证金率严重"),
        (7.0, "⚡ 保证金率警告"),
    ]

    def __init__(self, cfg):
        self.cfg = cfg
        self.log = setup_logger("monitor")
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log)
        self._last_alerts = {}
        self._start_ts = time.time()
        self._last_heartbeat = 0.0

    def run(self):
        """主循环"""
        self.log.info("监控启动")
        self._send_msg("监控启动")

        try:
            while True:
                self.tick()
                time.sleep(60)
        except KeyboardInterrupt:
            self.log.info("监控已停止")

    def tick(self):
        """单次扫描"""
        try:
            equity = float(self.client.get_account()["data"][0].get("accountEquity", "0"))
            positions = self.client.get_position("").get("data", [])

            if positions:
                mmr = float(positions[0].get("mmr", 0))
                mgn_ratio = equity / mmr if mmr > 0 else float('inf')
                self._check_alerts(equity, mgn_ratio)
                self._maybe_heartbeat(equity, mgn_ratio)
        except Exception as e:
            self.log.debug(f"查询异常: {e}")

    def _check_alerts(self, equity: float, mgn_ratio: float):
        """检查保证金率告警"""
        for threshold, emoji in self.ALERTS:
            if mgn_ratio < threshold:
                msg = f"{emoji}: {mgn_ratio*100:.1f}% | 权益: {equity:.2f} USDT"
                self._alert(f"mgn_{threshold}", msg)
                return

    def _maybe_heartbeat(self, equity: float, mgn_ratio: float):
        """定时心跳"""
        now = time.time()
        if now - self._last_heartbeat < 3600:
            return

        self._last_heartbeat = now
        uptime = (now - self._start_ts) / 3600
        self._send_msg(f"💓 {uptime:.1f}h | 权益: {equity:.2f} | 保证金率: {mgn_ratio*100:.0f}%")

    def _alert(self, key: str, msg: str):
        """告警（带节流）"""
        now = time.time()
        if now - self._last_alerts.get(key, 0) < 600:
            return

        self._last_alerts[key] = now
        self.log.warning(f"[ALERT] {msg}")
        self._send_msg(msg)

    def _send_msg(self, msg: str):
        """发送 Telegram"""
        if self.cfg.tg_bot_token:
            send_telegram(f"账户监控\n{msg}", self.cfg.tg_bot_token, self.cfg.tg_chat_id)


def main():
    cfg = parse_monitor_args()
    AccountMonitor(cfg).run()


if __name__ == "__main__":
    main()
