"""Telegram 通知器（参考 okxAPI/notifier.py 移植）

设计：
  - 单一职责：把一条消息发出去
  - 节流去重：同一 dedup_key 在冷却期内只发一次（避免刷屏）
  - 失败优雅：TG API 挂了不抛异常，降级到 log
  - 紧急自救：连续 N 次发送失败 → 蜂鸣(Windows) + 写 EMERGENCY_ALERT.txt 标志文件，
    解决"代理/VPN 挂了 TG 也没提醒"的盲区
  - 直连不走代理：服务器/新 VPN 环境下强制 proxies={} 屏蔽系统/env 代理

TG 消息用 HTML parse_mode — 文本里用 ≤/&lt; 勿用裸 <（会被当标签，400 丢失）。
"""
import os
import time
import logging

try:
    import requests
except ImportError:
    requests = None

LEVEL_EMOJI = {"INFO": "ℹ️", "WARN": "⚠️", "CRITICAL": "🚨", "TRADE": "💱"}


class TelegramNotifier:
    API_BASE = "https://api.telegram.org"
    EMERGENCY_FAIL_THRESHOLD = 3        # 连续 N 次失败触发本地告警
    EMERGENCY_COOLDOWN_SEC = 60         # 本地告警冷却，避免一直蜂鸣
    EMERGENCY_FLAG_FILE = "EMERGENCY_ALERT.txt"

    def __init__(self, bot_token: str = "", chat_id: str = "",
                 logger: logging.Logger = None,
                 default_throttle_sec: int = 0,
                 timeout: tuple = (5, 12)):
        self.bot_token = bot_token or os.getenv("TG_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TG_CHAT_ID", "")
        self.log = logger or logging.getLogger("notify")
        self.default_throttle_sec = default_throttle_sec
        self.timeout = timeout
        self.enabled = bool(self.bot_token and self.chat_id and requests)
        self._dedup = {}              # dedup_key -> last_sent_ts
        self._suppressed = {}         # dedup_key -> 压制计数
        self._consec_fail = 0
        self._last_emergency_ts = 0.0
        if not self.enabled:
            self.log.warning("TelegramNotifier 已禁用（缺凭据/requests），消息只写日志")

    def send(self, msg: str, level: str = "INFO", prefix: bool = True,
             dedup_key: str = None, throttle_sec: int = None) -> bool:
        """发送一条消息。
        :param prefix: True 自动加 `{emoji} <b>[LEVEL]</b>` 前缀；False 原样发（调用方已自带格式）
        :param dedup_key: 同 key 在 throttle_sec 内只发一次，期间压制次数累计到下条
        :return: True=已发送，False=被节流/禁用/失败
        """
        # 节流
        if dedup_key:
            cooldown = throttle_sec if throttle_sec is not None else self.default_throttle_sec
            now = time.time()
            if now - self._dedup.get(dedup_key, 0) < cooldown:
                self._suppressed[dedup_key] = self._suppressed.get(dedup_key, 0) + 1
                return False
            n_sup = self._suppressed.pop(dedup_key, 0)
            if n_sup > 0:
                msg += f"\n<i>(同期压制 {n_sup} 次)</i>"
            self._dedup[dedup_key] = now

        body = f"{LEVEL_EMOJI.get(level, 'ℹ️')} <b>[{level}]</b>\n{msg}" if prefix else msg

        # log 兜底（无论 TG 是否可用都落日志）
        self.log.info("[TG] %s", msg.replace("\n", " | "))

        if not self.enabled:
            return False

        ok = self._do_send(body)
        if ok:
            self._consec_fail = 0
        else:
            self._consec_fail += 1
            if self._consec_fail >= self.EMERGENCY_FAIL_THRESHOLD:
                self._trigger_emergency(body[:200])
        return ok

    def _do_send(self, body: str) -> bool:
        """单次 HTTP 发送（直连，强制不走代理）"""
        try:
            resp = requests.post(
                f"{self.API_BASE}/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": body,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
                proxies={"http": None, "https": None},   # ★ 屏蔽系统/env 代理
            )
            if resp.status_code != 200:
                self.log.error("TG 发送失败 status=%s body=%s",
                               resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:
            self.log.error("TG 发送异常: %s", e)
            return False

    def _trigger_emergency(self, last_msg: str):
        """连续 TG 失败 → 本地自救告警（不依赖网络）"""
        now = time.time()
        if now - self._last_emergency_ts < self.EMERGENCY_COOLDOWN_SEC:
            return
        self._last_emergency_ts = now

        msg = (f"TG 已连续失败 {self._consec_fail} 次，推送通道挂了！\n"
               f"可能原因: VPN/代理断开、网络中断、TG 被墙\n"
               f"最近一条: {last_msg!r}\n"
               f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.log.error("=" * 60)
        self.log.error("🚨 紧急: TG 通道已挂 🚨")
        self.log.error(msg)
        self.log.error("=" * 60)

        # 蜂鸣（Windows winsound，其他系统用 BEL）
        try:
            import platform
            if platform.system() == "Windows":
                import winsound
                for _ in range(3):
                    winsound.Beep(1000, 300)
                    time.sleep(0.1)
            else:
                print("\a\a\a", flush=True)
        except Exception:
            pass

        # 写标志文件（供外部脚本/监控轮询）
        try:
            with open(self.EMERGENCY_FLAG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    n = TelegramNotifier()
    print("发送结果:", n.send("✅ Bitget 通知通路测试", level="INFO"))
    for i in range(3):
        n.send(f"节流测试第 {i+1} 次", dedup_key="test", throttle_sec=300)
    print("节流测试完成（应只发 1 条）")
