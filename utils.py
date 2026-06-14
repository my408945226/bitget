"""工具函数"""
import logging
from typing import Optional

try:
    import requests
except ImportError:
    requests = None


def send_telegram(msg: str, bot_token: str, chat_id: str, logger: Optional[logging.Logger] = None):
    """发送 Telegram 消息"""
    if not bot_token or not chat_id or not requests:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        if logger:
            logger.warning(f"TG异常: {e}")
