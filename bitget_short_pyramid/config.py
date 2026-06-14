"""配置与 CLI 参数解析"""
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
    min_margin_ratio: float = 5.0
    max_notional_usdt: float = 10000
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""


def parse_args() -> Config:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Bitget 金字塔做空网格机器人")
    parser.add_argument("mode", choices=["dry-run", "live"], help="运行模式")
    parser.add_argument("--symbol", required=True, help="交易对")
    parser.add_argument("--size", type=float, required=True, help="每单大小")
    parser.add_argument("--grid", type=float, default=0.02, help="网格幅度 (0.02=2%%)")
    parser.add_argument("--leverage", type=int, default=3, help="杠杆倍数")
    parser.add_argument("--interval", type=int, default=5, help="轮询间隔(秒)")
    parser.add_argument("--min-margin-ratio", type=float, default=5.0, help="保证金率阈值 (5.0=500%%)")
    parser.add_argument("--max-notional-usdt", type=float, default=10000, help="单笔持仓限额 (USDT)")

    args = parser.parse_args()

    return Config(
        mode=args.mode,
        symbol=args.symbol.upper(),
        size=args.size,
        grid_pct=args.grid,
        leverage=args.leverage,
        interval=args.interval,
        min_margin_ratio=args.min_margin_ratio,
        max_notional_usdt=args.max_notional_usdt,
        tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_chat_id=os.getenv("TG_CHAT_ID", ""),
        api_key=os.getenv("BITGET_API_KEY", ""),
        secret_key=os.getenv("BITGET_SECRET_KEY", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    )
