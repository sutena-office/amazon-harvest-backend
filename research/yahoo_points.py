"""
Yahoo!ショッピングのポイント還元・クーポンを実務の計算式どおりに算出する。

実務モデルケース（エプソン EW-M638T / 2026-07 実測）:
    表示価格      41,272円
    クーポン      -3,100円   （ヤマダグループ等でよく出る）
    支払額        38,172円
    ポイント27%    9,292円   ← 税抜価格ベース（38,172 ÷ 1.1 × 0.27）
    実質価格      28,880円 ≒ 29,000円

還元は3層の合算:
  1) ストア独自 … Yahoo! APIのlyLimitedBonusAmountから実測（例12.5%）
  2) 日付キャンペーン … 5のつく日+4% / プレミアムな日曜日+5%（このモジュールが判定）
  3) 会員・決済由来 … LINE連携/PayPayカード/基本付与など（設定値）
"""
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
TAX_RATE = 1.10  # ポイント算定は税抜ベースのため税込価格から割り戻す

# 日付で確定する定期キャンペーンの上乗せ
FIVE_DAY_RATE = 4.0
SUNDAY_RATE = 5.0

# 通常日の総還元率（ストア独自ポイント＋会員＋決済＋常時エントリーの合計）。
# 実務モデルケース: 日曜に27% → 22 + 日曜5 = 27 で一致する。
# 実際の還元は商品ページの「〇%獲得」で確認し、この値を調整する。
BASE_TOTAL_RATE = float(os.getenv("YAHOO_BASE_TOTAL_RATE", "22.0"))
IS_PREMIUM = os.getenv("YAHOO_USE_PREMIUM", "true").lower() == "true"

# 付与上限。実務では複数キャンペーンが重なり上限がほぼ効かないため既定0（無効）。
# 高単価品で上限を効かせたい場合のみ設定する
POINT_CAP = int(os.getenv("YAHOO_POINT_CAP", "0"))

# 想定クーポン（ヤマダ・キムラヤ・ベスト電機等で恒常的に出る割引）
ASSUMED_COUPON = int(os.getenv("YAHOO_ASSUMED_COUPON", "0"))


def today_jst() -> datetime:
    return datetime.now(JST)


def campaign_for_date(dt: datetime = None) -> dict:
    """指定日の総還元率（通常日ぶん＋日付キャンペーン）を返す"""
    dt = dt or today_jst()
    parts = [f"通常還元 {BASE_TOTAL_RATE:g}%"]
    rate = BASE_TOTAL_RATE

    if dt.day in (5, 15, 25):
        parts.append(f"5のつく日 +{FIVE_DAY_RATE:g}%")
        rate += FIVE_DAY_RATE

    if dt.weekday() == 6 and IS_PREMIUM:
        parts.append(f"日曜 +{SUNDAY_RATE:g}%")
        rate += SUNDAY_RATE

    return {
        "date": dt.strftime("%Y-%m-%d"),
        "rate": rate,
        "cap": POINT_CAP,
        "labels": parts,
        "summary": " / ".join(parts),
    }


def effective_cost(price: int, store_point: int = 0, coupon: int = None,
                   dt: datetime = None, extra_rate: float = 0.0) -> dict:
    """実質仕入れ値を実務の計算式で算出する。

    実務モデル: 実質 =（表示価格 − クーポン）−（支払額 ÷ 1.1 × 総還元率）
    ポイントは税抜価格ベースで付与されるため 1.1 で割り戻す。

    price:       Yahoo!の表示価格（税込）
    store_point: APIから取れたストア独自ポイント（参考表示用。総還元率に内包済み）
    coupon:      クーポン割引額。Noneなら環境変数の想定値
    extra_rate:  超PayPay祭など不定期キャンペーンの上乗せ率
    """
    coupon = ASSUMED_COUPON if coupon is None else coupon
    coupon = min(coupon, price)
    payment = price - coupon
    taxable_base = int(payment / TAX_RATE)

    camp = campaign_for_date(dt)
    total_rate = camp["rate"] + extra_rate
    total_point = int(taxable_base * total_rate / 100)
    if POINT_CAP > 0:
        total_point = min(total_point, POINT_CAP)

    effective = payment - total_point

    return {
        "price": price,
        "coupon": coupon,
        "payment": payment,
        "store_point": store_point,   # APIの実測値（参考。総還元率に含まれる想定）
        "total_point": total_point,
        "total_rate": round(total_rate, 1),
        "effective": effective,
        "campaign_summary": camp["summary"],
    }


def upcoming_best_days(days_ahead: int = 14) -> list:
    """今後の仕入れ狙い目日（還元率順）"""
    base = today_jst()
    results = []
    for i in range(days_ahead):
        d = base + timedelta(days=i)
        c = campaign_for_date(d)
        if c["rate"] > BASE_TOTAL_RATE:
            results.append({
                "date": c["date"],
                "weekday": "月火水木金土日"[d.weekday()],
                "rate": c["rate"],
                "cap": c["cap"],
                "summary": c["summary"],
                "days_from_now": i,
            })
    results.sort(key=lambda r: (-r["rate"], r["days_from_now"]))
    return results
