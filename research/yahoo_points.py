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

# 日付で確定する定期キャンペーンの上乗せ（いずれも付与上限の対象）
FIVE_DAY_RATE = 4.0
SUNDAY_RATE = 5.0

# ── 還元は「上限なし」と「上限あり」の2層に分けて計算する ──
# 5のつく日は1,000ポイント上限（税抜25,000円で頭打ち）等、
# キャンペーン系には付与上限があるため、高額商品ほど実効還元率が下がる。
#
# 上限なし: ストア独自ポイント（倍倍ストア等）＋PayPay基本付与
UNCAPPED_RATE = float(os.getenv("YAHOO_UNCAPPED_RATE", "13.0"))
# 上限あり: LYPプレミアム・LINE連携・各種エントリーの常時分
CAPPED_BASE_RATE = float(os.getenv("YAHOO_CAPPED_BASE_RATE", "9.0"))
# 上限ありキャンペーンの付与上限合計（円）
CAMPAIGN_CAP = int(os.getenv("YAHOO_CAMPAIGN_CAP", "5000"))

IS_PREMIUM = os.getenv("YAHOO_USE_PREMIUM", "true").lower() == "true"

# 想定クーポン（ヤマダ・キムラヤ・ベスト電機等で恒常的に出る割引）
ASSUMED_COUPON = int(os.getenv("YAHOO_ASSUMED_COUPON", "0"))


def today_jst() -> datetime:
    return datetime.now(JST)


def campaign_for_date(dt: datetime = None) -> dict:
    """指定日の還元率を「上限なし分」「上限あり分」に分けて返す"""
    dt = dt or today_jst()
    parts = [f"ストア独自+基本 {UNCAPPED_RATE:g}%（上限なし）"]
    capped_rate = CAPPED_BASE_RATE
    capped_labels = [f"会員・LINE等 {CAPPED_BASE_RATE:g}%"]

    if dt.day in (5, 15, 25):
        capped_rate += FIVE_DAY_RATE
        capped_labels.append(f"5のつく日 +{FIVE_DAY_RATE:g}%")

    if dt.weekday() == 6 and IS_PREMIUM:
        capped_rate += SUNDAY_RATE
        capped_labels.append(f"日曜 +{SUNDAY_RATE:g}%")

    parts.append(f"{' / '.join(capped_labels)}（合計上限¥{CAMPAIGN_CAP:,}）")

    return {
        "date": dt.strftime("%Y-%m-%d"),
        "uncapped_rate": UNCAPPED_RATE,
        "capped_rate": capped_rate,
        "rate": UNCAPPED_RATE + capped_rate,   # 上限に当たらない場合の総還元率
        "cap": CAMPAIGN_CAP,
        "labels": parts,
        "summary": " ＋ ".join(parts),
    }


def effective_cost(price: int, store_point: int = 0, coupon: int = None,
                   dt: datetime = None, extra_rate: float = 0.0) -> dict:
    """実質仕入れ値を実務の計算式で算出する。

    ポイントは税抜価格ベースで付与されるため 1.1 で割り戻す。
    キャンペーン系には付与上限（5のつく日=1,000pt等）があるため、
    高額商品では上限で頭打ちになり実効還元率が下がる。

    price:       Yahoo!の表示価格（税込）
    store_point: APIから取れたストア独自ポイント（参考表示用）
    coupon:      クーポン割引額。Noneなら環境変数の想定値
    extra_rate:  超PayPay祭など不定期キャンペーンの上乗せ率（上限対象）
    """
    coupon = ASSUMED_COUPON if coupon is None else coupon
    coupon = min(coupon, price)
    payment = price - coupon
    taxable_base = int(payment / TAX_RATE)

    camp = campaign_for_date(dt)
    uncapped_point = int(taxable_base * camp["uncapped_rate"] / 100)
    capped_raw = int(taxable_base * (camp["capped_rate"] + extra_rate) / 100)
    capped_point = min(capped_raw, CAMPAIGN_CAP) if CAMPAIGN_CAP > 0 else capped_raw
    cap_hit = capped_raw > capped_point

    total_point = uncapped_point + capped_point
    effective = payment - total_point
    actual_rate = round(total_point / taxable_base * 100, 1) if taxable_base else 0

    return {
        "price": price,
        "coupon": coupon,
        "payment": payment,
        "store_point": store_point,
        "uncapped_point": uncapped_point,
        "capped_point": capped_point,
        "capped_lost": capped_raw - capped_point,   # 上限で取り逃した分
        "cap_hit": cap_hit,
        "total_point": total_point,
        "total_rate": actual_rate,                  # 上限適用後の実効還元率
        "nominal_rate": round(camp["rate"] + extra_rate, 1),
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
        if c["capped_rate"] > CAPPED_BASE_RATE:
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
