DEFAULT_AMAZON_FEE_RATE = 15.4  # Amazon手数料（%）
SHIPPING_TO_FBA = 600           # FBA納品送料概算（円）


def calculate_profit(
    buy_price: int,
    sell_price: int,
    amazon_fee_rate: float = DEFAULT_AMAZON_FEE_RATE,
) -> dict:
    """
    刈り取り利益を計算する。
    buy_price:  仕入れ値（値下がり後の現在価格）
    sell_price: 販売価格（通常価格・90日平均）
    """
    fee_rate = amazon_fee_rate / 100
    amazon_fee = int(sell_price * fee_rate)
    profit = sell_price - buy_price - amazon_fee - SHIPPING_TO_FBA
    profit_rate = (profit / sell_price * 100) if sell_price > 0 else 0

    return {
        "profit_amount": int(profit),
        "profit_rate": round(profit_rate, 1),
        "amazon_fee": amazon_fee,
        "amazon_fee_rate": amazon_fee_rate,
    }
