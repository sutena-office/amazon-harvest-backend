DEFAULT_AMAZON_FEE_RATE = 18.0  # Amazon手数料＋送料込み経費（%）


def calculate_profit(
    buy_price: int,
    sell_price: int,
    amazon_fee_rate: float = DEFAULT_AMAZON_FEE_RATE,
) -> dict:
    """
    刈り取り利益を計算する。
    buy_price:  仕入れ値（値下がり後の現在価格）
    sell_price: 販売価格（通常価格・90日平均）
    FBA費用は商品サイズ・重量によって大きく異なるため含めない。
    実際の利益はFBA/FBM費用を別途差し引いて計算すること。
    """
    fee_rate = amazon_fee_rate / 100
    amazon_fee = int(sell_price * fee_rate)
    profit = sell_price - buy_price - amazon_fee
    profit_rate = (profit / sell_price * 100) if sell_price > 0 else 0

    return {
        "profit_amount": int(profit),
        "profit_rate": round(profit_rate, 1),
        "amazon_fee": amazon_fee,
        "amazon_fee_rate": amazon_fee_rate,
    }
