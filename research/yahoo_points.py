"""
Yahoo!ショッピングのポイント還元・クーポンを実務の計算式どおりに算出する。

実務モデルケース（エプソン EW-M638T / 2026-07 実測・日曜）:
    表示価格      41,272円
    クーポン      -3,100円   （ヤマダグループ等でよく出る）
    支払額        38,172円
    ポイント27%    9,292円   ← 税抜価格ベース（38,172 ÷ 1.1 × 27%）
    実質価格      28,880円 ≒ 29,000円

重要: キャンペーンごとに個別の付与上限があるため、高額商品ほど
実効還元率が下がる。上限は「キャンペーン単位」で効くので、合算してから
一括で頭打ちにするのではなく、1つずつ上限を当ててから合計する。
"""
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
TAX_RATE = 1.10  # ポイント算定は税抜ベースのため税込価格から割り戻す


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


# ── キャンペーン定義（rate=付与率%, cap=付与上限ポイント。cap=0は上限なし）──
# 上限値の出典: Yahoo!ショッピング公表値（2026年時点）
def campaigns() -> dict:
    return {
        "store": {
            # 倍倍ストア等の店舗独自分。実測モデルケース（日曜に総27%）に
            # 合わせて既定15%。APIから実額が取れる商品ではそちらを優先する
            "label": "ストア独自ポイント",
            "rate": _f("YAHOO_STORE_RATE", 15.0),
            "cap": _i("YAHOO_STORE_CAP", 0),          # 上限なし
        },
        "base": {
            "label": "PayPay基本付与",
            "rate": _f("YAHOO_BASE_RATE", 1.0),
            "cap": _i("YAHOO_BASE_CAP", 0),           # 上限なし
        },
        "lyp": {
            "label": "LYPプレミアム",
            "rate": _f("YAHOO_LYP_RATE", 2.0),
            "cap": _i("YAHOO_LYP_CAP", 5000),         # 月5,000pt（25万円で到達）
        },
        "line": {
            "label": "LINE連携",
            "rate": _f("YAHOO_LINE_RATE", 4.0),
            "cap": _i("YAHOO_LINE_CAP", 5000),        # 月5,000pt
        },
        "five_day": {
            "label": "5のつく日",
            "rate": _f("YAHOO_FIVEDAY_RATE", 4.0),
            "cap": _i("YAHOO_FIVEDAY_CAP", 1000),     # 1,000pt（税抜25,000円で到達）
        },
        "sunday": {
            "label": "プレミアムな日曜日",
            "rate": _f("YAHOO_SUNDAY_RATE", 5.0),
            "cap": _i("YAHOO_SUNDAY_CAP", 2000),      # 2,000pt（税抜40,000円で到達）
        },
        "matsuri": {
            "label": "超PayPay祭",
            "rate": _f("YAHOO_MATSURI_RATE", 7.0),
            "cap": _i("YAHOO_MATSURI_CAP", 7000),     # 7,000pt（10万円で到達）
        },
    }


# 常時適用されるキャンペーン（日付に関係なく付く分）
ALWAYS_ON = ["store", "base", "lyp", "line"]

IS_PREMIUM = os.getenv("YAHOO_USE_PREMIUM", "true").lower() == "true"
ASSUMED_COUPON = int(os.getenv("YAHOO_ASSUMED_COUPON", "0"))

# 画面のプルダウンに対応するシナリオ
SCENARIOS = {
    "auto":       None,                              # 実行日の日付から判定
    "normal":     [],                                # 日付キャンペーンなし
    "five_day":   ["five_day"],
    "five_sun":   ["five_day", "sunday"],
    "matsuri":    ["matsuri"],
}


def today_jst() -> datetime:
    return datetime.now(JST)


def _date_campaigns(dt: datetime) -> list:
    """その日に自動適用される日付キャンペーンのキー一覧"""
    keys = []
    if dt.day in (5, 15, 25):
        keys.append("five_day")
    if dt.weekday() == 6 and IS_PREMIUM:
        keys.append("sunday")
    return keys


def active_campaign_keys(dt: datetime = None, scenario: str = "auto") -> list:
    """適用するキャンペーンのキー一覧（常時分＋日付/シナリオ分）"""
    dt = dt or today_jst()
    extra = SCENARIOS.get(scenario)
    if extra is None:          # auto
        extra = _date_campaigns(dt)
    return ALWAYS_ON + [k for k in extra if k not in ALWAYS_ON]


def campaign_for_date(dt: datetime = None, scenario: str = "auto") -> dict:
    """適用されるキャンペーンの内訳と名目還元率を返す"""
    dt = dt or today_jst()
    defs = campaigns()
    keys = active_campaign_keys(dt, scenario)

    labels = []
    rate = 0.0
    for k in keys:
        c = defs[k]
        rate += c["rate"]
        cap_txt = f"上限{c['cap']:,}pt" if c["cap"] else "上限なし"
        labels.append(f"{c['label']} {c['rate']:g}%（{cap_txt}）")

    return {
        "date": dt.strftime("%Y-%m-%d"),
        "scenario": scenario,
        "keys": keys,
        "rate": round(rate, 1),
        "labels": labels,
        "summary": " ＋ ".join(labels),
    }


def effective_cost(price: int, store_point: int = 0, coupon: int = None,
                   dt: datetime = None, scenario: str = "auto") -> dict:
    """実質仕入れ値を算出する。

    ポイントは税抜価格ベースで付与されるため 1.1 で割り戻す。
    各キャンペーンに個別の付与上限を当ててから合計するので、
    高額商品では上限に当たった分だけ実効還元率が下がる。

    price:       Yahoo!の表示価格（税込）
    store_point: APIから取れたストア独自ポイント（円）。あればストア分に優先採用
    coupon:      クーポン割引額。Noneなら環境変数の想定値
    scenario:    auto / normal / five_day / five_sun / matsuri
    """
    coupon = ASSUMED_COUPON if coupon is None else coupon
    coupon = min(coupon, price)
    payment = price - coupon
    taxable_base = int(payment / TAX_RATE)

    defs = campaigns()
    keys = active_campaign_keys(dt, scenario)

    breakdown = []
    total_point = 0
    total_lost = 0
    nominal_rate = 0.0

    for k in keys:
        c = defs[k]
        nominal_rate += c["rate"]
        if k == "store" and store_point > 0:
            raw = store_point          # API実測値を優先
        else:
            raw = int(taxable_base * c["rate"] / 100)
        granted = min(raw, c["cap"]) if c["cap"] > 0 else raw
        total_point += granted
        total_lost += raw - granted
        breakdown.append({
            "key": k, "label": c["label"], "rate": c["rate"],
            "cap": c["cap"], "raw": raw, "granted": granted,
            "capped": raw > granted,
        })

    effective = payment - total_point
    actual_rate = round(total_point / taxable_base * 100, 1) if taxable_base else 0

    return {
        "price": price,
        "coupon": coupon,
        "payment": payment,
        "store_point": store_point,
        "total_point": total_point,
        "capped_lost": total_lost,
        "cap_hit": total_lost > 0,
        "total_rate": actual_rate,                 # 上限適用後の実効還元率
        "nominal_rate": round(nominal_rate, 1),    # 上限がなければ得られた率
        "effective": effective,
        "breakdown": breakdown,
        "campaign_summary": campaign_for_date(dt, scenario)["summary"],
    }


def upcoming_best_days(days_ahead: int = 14) -> list:
    """今後の仕入れ狙い目日（名目還元率順）"""
    base = today_jst()
    baseline = campaign_for_date(base, "normal")["rate"]
    results = []
    for i in range(days_ahead):
        d = base + timedelta(days=i)
        c = campaign_for_date(d, "auto")
        if c["rate"] > baseline:
            results.append({
                "date": c["date"],
                "weekday": "月火水木金土日"[d.weekday()],
                "rate": c["rate"],
                "summary": c["summary"],
                "days_from_now": i,
            })
    results.sort(key=lambda r: (-r["rate"], r["days_from_now"]))
    return results
