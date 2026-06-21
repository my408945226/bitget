"""配置"""
import argparse
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    symbol: str
    size: float
    grid_pct: float
    leverage: int = 3
    interval: int = 5
    max_notional_usdt: float = 10000
    initial_sell_px: float = 0.0  # 限价起仓价格
    adopt_sell_px: float = 0.0    # 基准价（自动往上偏移）
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""


def parse_args() -> Config:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Bitget 金字塔做空（实盘）")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--sz", type=float, required=True, dest="size", help="每单张数")
    parser.add_argument("--grid", type=float, default=0.02)
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--max-notional", type=float, default=10000)
    parser.add_argument("--limit", type=float, default=0, dest="initial_sell_px", help="限价起仓价格")
    parser.add_argument("--adopt", type=float, default=0, dest="adopt_sell_px", help="基准价（自动往上偏移）")

    args = parser.parse_args()
    return Config(
        symbol=args.symbol.upper(),
        size=args.size,
        grid_pct=args.grid,
        leverage=args.leverage,
        interval=args.interval,
        max_notional_usdt=args.max_notional,
        initial_sell_px=args.initial_sell_px,
        adopt_sell_px=args.adopt_sell_px,
        tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_chat_id=os.getenv("TG_CHAT_ID", ""),
        api_key=os.getenv("BITGET_API_KEY", ""),
        secret_key=os.getenv("BITGET_SECRET_KEY", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    )


def parse_monitor_args() -> Config:
    """监控专用参数解析"""
    load_dotenv()
    parser = argparse.ArgumentParser(description="Bitget 账户监控")
    args = parser.parse_args()

    return Config(
        symbol="",
        size=1.0,
        grid_pct=0.0,
        interval=60,  # 固定 60 秒
        tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_chat_id=os.getenv("TG_CHAT_ID", ""),
        api_key=os.getenv("BITGET_API_KEY", ""),
        secret_key=os.getenv("BITGET_SECRET_KEY", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    )
