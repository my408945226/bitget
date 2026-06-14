"""精度处理模块 - 使用 Decimal 进行精确计算"""
from decimal import Decimal, ROUND_DOWN, InvalidOperation


def to_decimal(value) -> Decimal:
    """安全转换为 Decimal"""
    if value is None or value == "" or value == "0":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def quantize_size(size: Decimal, size_multiplier: Decimal, volume_place: int) -> Decimal:
    """向下取整到 sizeMultiplier 的倍数，并限制小数位"""
    if size_multiplier <= 0:
        size_multiplier = Decimal("1")
    # 向下取整到 multiplier 的倍数
    steps = (size / size_multiplier).to_integral_value(rounding=ROUND_DOWN)
    result = steps * size_multiplier
    # 限制小数位
    if volume_place >= 0:
        fmt = Decimal(10) ** -volume_place
        result = result.quantize(fmt, rounding=ROUND_DOWN)
    return result


def quantize_price(price: Decimal, price_place: int) -> Decimal:
    """按 pricePlace 向下处理价格小数位"""
    if price_place < 0:
        price_place = 0
    fmt = Decimal(10) ** -price_place
    return price.quantize(fmt, rounding=ROUND_DOWN)


def validate_order_size(
    size: Decimal, mark_price: Decimal, contract_info: dict
) -> tuple[bool, str]:
    """
    验证下单数量是否满足合约要求。
    返回 (is_valid, error_message)
    """
    min_trade_num = to_decimal(contract_info.get("minTradeNum", 0))
    size_multiplier = to_decimal(contract_info.get("sizeMultiplier", 1))
    min_trade_usdt = to_decimal(contract_info.get("minTradeUSDT", 0))
    max_market_qty = to_decimal(contract_info.get("maxMarketOrderQty", 0))

    if size < min_trade_num:
        return False, f"size {size} < minTradeNum {min_trade_num}"

    if size_multiplier > 0:
        remainder = size % size_multiplier
        if remainder != 0:
            return False, f"size {size} 不是 sizeMultiplier {size_multiplier} 的倍数"

    notional = size * mark_price
    if notional < min_trade_usdt:
        return False, f"名义价值 {notional} < minTradeUSDT {min_trade_usdt}"

    if max_market_qty > 0 and size > max_market_qty:
        return False, f"size {size} > maxMarketOrderQty {max_market_qty}"

    return True, ""
