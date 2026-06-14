"""配置"""
import argparse
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    mode: str
    symbol: str
    size: float
    grid_pct: float
    leverage: int = 3
    interval: int = 5
    max_notional_usdt: float = 10000
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""


def parse_args() -> Config:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Bitget 金字塔做空")
    parser.add_argument("mode", choices=["dry-run", "live"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--size", type=float, required=True)
    parser.add_argument("--grid", type=float, default=0.02)
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--max-notional", type=float, default=10000)

    args = parser.parse_args()
    return Config(
        mode=args.mode,
        symbol=args.symbol.upper(),
        size=args.size,
        grid_pct=args.grid,
        leverage=args.leverage,
        interval=args.interval,
        max_notional_usdt=args.max_notional,
        tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_chat_id=os.getenv("TG_CHAT_ID", ""),
        api_key=os.getenv("BITGET_API_KEY", ""),
        secret_key=os.getenv("BITGET_SECRET_KEY", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    )
