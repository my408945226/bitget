"""账户监控器 - 保证金率告警"""
import time
import os
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
    ALERTS = [
        (3.0, "🚨 保证金率紧急"),
        (5.0, "⚠️ 保证金率严重"),
        (7.0, "⚡ 保证金率警告"),
    ]

    def __init__(self, cfg):
        self.cfg = cfg
        self.log = _setup_logger("monitor")
        self.client = BitgetClient(cfg.api_key, cfg.secret_key, cfg.passphrase, self.log)
        self._last_alerts = {}
        self._start_ts = time.time()
        self._last_heartbeat = 0.0

    def run(self):
        """主循环"""
        try:
            self.log.info("监控启动")
            self._send_msg("监控启动")

            count = 0
            while True:
                try:
                    count += 1
                    self.log.info(f"[{count}] 扫描中...")
                    self.tick()
                except Exception as e:
                    self.log.error(f"tick 失败: {e}", exc_info=True)
                time.sleep(60)
        except KeyboardInterrupt:
            self.log.info("监控已停止")

    def tick(self):
        """单次扫描"""
        try:
            acct = self.client.get_account()
            if acct.get("code") != "00000":
                self.log.warning(f"查询账户失败: {acct.get('msg', 'unknown')}")
                return

            equity = float(acct.get("data", [{}])[0].get("accountEquity", "0"))

            pos_resp = self.client.get_position("")
            if pos_resp.get("code") != "00000":
                self.log.warning(f"查询持仓失败: {pos_resp.get('msg', 'unknown')}")
                return

            positions = pos_resp.get("data", [])
            if positions:
                mmr = float(positions[0].get("mmr", 0))
                mgn_ratio = equity / mmr if mmr > 0 else float('inf')
                self.log.debug(f"保证金率: {mgn_ratio*100:.1f}% | 权益: {equity:.2f}")
                self._check_alerts(equity, mgn_ratio)
                self._maybe_heartbeat(equity, mgn_ratio)
            else:
                self.log.debug(f"无持仓 | 权益: {equity:.2f}")
        except Exception as e:
            self.log.error(f"tick 异常: {e}", exc_info=True)

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
            _send_telegram(f"账户监控\n{msg}", self.cfg.tg_bot_token, self.cfg.tg_chat_id)


def main():
    cfg = parse_monitor_args()
    AccountMonitor(cfg).run()


if __name__ == "__main__":
    main()
