"""工具函数"""
import time
import random
import string
import logging
from typing import Optional

try:
    import requests
except ImportError:
    requests = None  # 可选依赖


def generate_client_oid(symbol: str) -> str:
    """生成唯一 clientOid: sp_<symbol>_<timestamp_ms>_<random>"""
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"sp_{symbol}_{ts}_{rand}"


def safe_float(value, default=0.0) -> float:
    """安全转换为 float"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def ts_now_ms() -> str:
    """当前毫秒时间戳字符串"""
    return str(int(time.time() * 1000))


def send_telegram(msg: str, bot_token: str, chat_id: str, logger: Optional[logging.Logger] = None):
    """P2: 发送 Telegram 消息（静默失败，不影响策略运行）"""
    if not bot_token or not chat_id:
        return
    if requests is None:
        if logger:
            logger.warning("requests 未安装，Telegram 通知不可用")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=5)
        if resp.status_code != 200 and logger:
            logger.warning(f"TG 发送失败: {resp.text}")
    except Exception as e:
        if logger:
            logger.warning(f"TG 异常: {e}")
