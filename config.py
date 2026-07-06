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
    adopt_auto: bool = False      # --adopt 裸写：自动取账户最后成交价（无则市价）作基准
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
    parser.add_argument(
        "--adopt", nargs="?", const="AUTO", default=None, dest="adopt_raw",
        help="接管基准价：'--adopt <px>' 用指定价；裸写 '--adopt' 自动取该 symbol "
             "账户最后一笔成交价（查不到用当前市价）；不写则不启用",
    )

    args = parser.parse_args()

    # --adopt 三态解析：None(不写) / 'AUTO'(裸写) / float(带值)
    adopt_auto = False
    adopt_sell_px = 0.0
    if args.adopt_raw is not None:
        if str(args.adopt_raw).upper() == "AUTO":
            adopt_auto = True
        else:
            adopt_sell_px = float(args.adopt_raw)

    return Config(
        symbol=args.symbol.upper(),
        size=args.size,
        grid_pct=args.grid,
        leverage=args.leverage,
        interval=args.interval,
        max_notional_usdt=args.max_notional,
        initial_sell_px=args.initial_sell_px,
        adopt_sell_px=adopt_sell_px,
        adopt_auto=adopt_auto,
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
