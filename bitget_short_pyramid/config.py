"""配置与 CLI 参数解析"""
import argparse
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    symbol: str
    product_type: str
    margin_coin: str
    margin_mode: str
    size: float
    grid_pct: float
    multiplier: float
    layers: int
    tp_pct: float
    leverage: int
    interval: int
    min_liq_dist: float
    max_pos_usdt: float
    max_orders: int
    mode: str
    # 风控参数（对应 OKX 双层风控）
    min_margin_ratio: float = 5.0  # 保证金率阈值 (500%)
    max_notional_usdt: float = 10000  # 单笔持仓名义价值上限 (USDT)
    # Telegram 通知
    tg_bot_token: str = ""
    tg_chat_id: str = ""

    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""


def parse_args() -> Config:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Bitget USDT本位合约金字塔做空网格机器人"
    )
    parser.add_argument("mode", choices=["dry-run", "live"], help="运行模式")
    parser.add_argument("--symbol", required=True, help="交易对")
    parser.add_argument("--product-type", default="USDT-FUTURES")
    parser.add_argument("--margin-coin", default="USDT")
    parser.add_argument("--margin-mode", default="crossed", choices=["crossed", "isolated"])
    parser.add_argument("--size", type=float, required=True, help="首层开仓数量")
    parser.add_argument("--grid", type=float, default=0.02, help="网格间距 (0.02=2%%)")
    parser.add_argument("--multiplier", type=float, default=1.2, help="每层递增倍数")
    parser.add_argument("--layers", type=int, default=8, help="最大层数")
    parser.add_argument("--tp-pct", type=float, default=0.01, help="止盈比例 (0.01=1%%)")
    parser.add_argument("--leverage", type=int, default=3, help="杠杆")
    parser.add_argument("--interval", type=int, default=5, help="轮询间隔(秒)")
    parser.add_argument("--min-liq-dist", type=float, default=0.15, help="最小强平距离")
    parser.add_argument("--max-pos-usdt", type=float, default=0, help="最大持仓价值(USDT)")
    parser.add_argument("--max-orders", type=int, default=100, help="每日最大下单次数")
    # 风控参数
    parser.add_argument("--min-margin-ratio", type=float, default=5.0,
                        help="最小保证金率 (5.0=500%%)")
    parser.add_argument("--max-notional-usdt", type=float, default=10000,
                        help="单笔持仓名义价值上限 (USDT)")

    args = parser.parse_args()

    return Config(
        mode=args.mode,
        symbol=args.symbol.upper(),
        product_type=args.product_type,
        margin_coin=args.margin_coin,
        margin_mode=args.margin_mode,
        size=args.size,
        grid_pct=args.grid,
        multiplier=args.multiplier,
        layers=args.layers,
        tp_pct=args.tp_pct,
        leverage=args.leverage,
        interval=args.interval,
        min_liq_dist=args.min_liq_dist,
        max_pos_usdt=args.max_pos_usdt,
        max_orders=args.max_orders,
        min_margin_ratio=args.min_margin_ratio,
        max_notional_usdt=args.max_notional_usdt,
        tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_chat_id=os.getenv("TG_CHAT_ID", ""),
        api_key=os.getenv("BITGET_API_KEY", ""),
        secret_key=os.getenv("BITGET_SECRET_KEY", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    )
