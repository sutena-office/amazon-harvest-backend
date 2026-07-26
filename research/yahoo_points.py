"""
Yahoo!ショッピングのポイント還元を日付から自動判定する。

還元は3層に分かれる:
  1) ストア独自ポイント … Yahoo! APIのlyLimitedBonusAmountから取得（yahoo_api.py）
  2) 日付で決まる定期キャンペーン … このモジュールが担当（外部取得不要）
  3) 本人の会員ステータス … 環境変数で設定（APIからは取得不可能）

不定期の大型キャンペーン（超PayPay祭等）だけは手動で上乗せ率を指定する。
"""
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

# 定期キャンペーン（2026年時点の公表値）
FIVE_DAY_RATE = 4.0       # 5のつく日: +4%
FIVE_DAY_CAP = 1000       # 付与上限1,000ポイント
SUNDAY_RATE = 5.0         # プレミアムな日曜日: +5%（LYPプレミアム会員向け）
SUNDAY_CAP = 1000         # 付与上限（保守的に1,000で見積もる）

# 本人の会員ステータス由来の上乗せ（通常日でも常時付く分）
# 例: LYPプレミアム+2% / LINE連携 など。実際の値はYahoo!の商品ページで確認して設定する
MEMBERSHIP_RATE = float(os.getenv("YAHOO_MEMBERSHIP_RATE", "2.0"))
IS_PREMIUM = os.getenv("YAHOO_USE_PREMIUM", "true").lower() == "true"


def today_jst() -> datetime:
    return datetime.now(JST)


def campaign_for_date(dt: datetime = None) -> dict:
    """指定日（既定は今日/JST）に自動適用されるキャンペーン還元を返す"""
    dt = dt or today_jst()
    parts = []
    rate = 0.0
    cap = 0

    if dt.day in (5, 15, 25):
        parts.append(f"5のつく日 +{FIVE_DAY_RATE:g}%")
        rate += FIVE_DAY_RATE
        cap += FIVE_DAY_CAP

    if dt.weekday() == 6 and IS_PREMIUM:  # 6=日曜
        parts.append(f"プレミアムな日曜日 +{SUNDAY_RATE:g}%")
        rate += SUNDAY_RATE
        cap += SUNDAY_CAP

    if MEMBERSHIP_RATE > 0:
        parts.append(f"会員特典 +{MEMBERSHIP_RATE:g}%")
        rate += MEMBERSHIP_RATE
        # 会員特典は通常上限なし扱い（上限は日付キャンペーン側で効かせる）

    return {
        "date": dt.strftime("%Y-%m-%d"),
        "rate": rate,
        "cap": cap,
        "labels": parts,
        "summary": " / ".join(parts) if parts else "通常日（追加キャンペーンなし）",
    }


def upcoming_best_days(days_ahead: int = 14) -> list:
    """今後の「仕入れに向いた日」を還元率順に返す（仕入れ計画用）"""
    base = today_jst()
    results = []
    for i in range(days_ahead):
        d = base + timedelta(days=i)
        c = campaign_for_date(d)
        if c["rate"] > MEMBERSHIP_RATE:  # 会員特典だけの日は除外
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
