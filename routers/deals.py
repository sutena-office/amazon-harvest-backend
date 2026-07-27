from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query, BackgroundTasks
from database import supabase
from auth import get_current_user

router = APIRouter()

JST = timezone(timedelta(hours=9))
WD = "月火水木金土日"


def _fetch_all(user_id: str) -> list:
    """検知履歴を全件取得（古い順）"""
    return (
        supabase.table("harvest_results")
        .select("*")
        .eq("user_id", user_id)
        .order("found_at", desc=False)
        .execute()
    ).data or []


def _jst(iso: str):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(JST)
    except Exception:
        return None


@router.get("/")
async def get_deals(
    current_user=Depends(get_current_user),
    limit: int = Query(50, le=100),
    offset: int = Query(0),
):
    """刈り取り候補一覧を取得（ASINが有効なもののみ）"""
    response = (
        supabase.table("harvest_results")
        .select("*")
        .eq("user_id", current_user.id)
        .like("amazon_asin", "B%")
        .order("found_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return response.data


@router.get("/export")
async def export_history(current_user=Depends(get_current_user)):
    """検知履歴（Discord通知した商品）をCSVで書き出す"""
    import csv, io
    from fastapi.responses import StreamingResponse

    rows = _fetch_all(current_user.id)
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)

    now = datetime.now(JST)
    w.writerow(["■ 値下がり検知の履歴（Discord通知した商品）"])
    w.writerow(["出力日時", now.strftime(f"%Y-%m-%d（{WD[now.weekday()]}） %H:%M") + " JST"])
    w.writerow(["記録件数", f"{len(rows)}件"])
    if rows:
        first, last = _jst(rows[0].get("found_at")), _jst(rows[-1].get("found_at"))
        if first and last:
            w.writerow(["期間", f'{first.strftime("%Y-%m-%d")} 〜 {last.strftime("%Y-%m-%d")}'])
    w.writerow(["注意", "検知した時点の記録です。値下がりの原因（セール・在庫処分等）までは記録していません"])
    w.writerow([])

    w.writerow([
        "検知日", "曜日", "時刻", "時間帯", "商品名", "ASIN",
        "検知時価格", "通常価格(90日)", "値下がり率(%)", "値下がり額",
        "ランク", "予想利益", "利益率(%)", "Amazonリンク", "Keepaグラフ",
    ])
    for r in rows:
        d = _jst(r.get("found_at"))
        cur = r.get("current_price") or 0
        reg = r.get("regular_price") or 0
        w.writerow([
            d.strftime("%Y-%m-%d") if d else "", WD[d.weekday()] if d else "",
            d.strftime("%H:%M") if d else "", f"{d.hour}時台" if d else "",
            r.get("product_name", ""), r.get("amazon_asin", ""),
            cur, reg, r.get("price_drop_rate", 0), reg - cur,
            r.get("amazon_rank", 0), r.get("profit_amount", 0), r.get("profit_rate", 0),
            f"https://www.amazon.co.jp/dp/{r.get('amazon_asin','')}",
            f"https://keepa.com/#!product/5-{r.get('amazon_asin','')}",
        ])

    buf.seek(0)
    fname = f"harvest_history_{now.strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/analysis")
async def analyze_history(current_user=Depends(get_current_user)):
    """検知履歴から傾向を集計する（曜日・時間帯・リピート商品・下落率）"""
    from collections import Counter, defaultdict

    rows = _fetch_all(current_user.id)
    if not rows:
        return {"count": 0}

    by_weekday = Counter()
    by_hour = Counter()
    by_date = Counter()
    drop_buckets = Counter()
    per_asin = defaultdict(list)
    drops = []

    for r in rows:
        d = _jst(r.get("found_at"))
        if d:
            by_weekday[WD[d.weekday()]] += 1
            by_hour[d.hour] += 1
            by_date[d.strftime("%Y-%m-%d")] += 1
        rate = float(r.get("price_drop_rate") or 0)
        drops.append(rate)
        lo = int(rate // 10) * 10
        drop_buckets[f"{lo}〜{lo + 9}%"] += 1
        per_asin[r.get("amazon_asin")].append({
            "date": d.strftime("%m/%d %H:%M") if d else "",
            "drop": rate,
            "price": r.get("current_price"),
            "name": (r.get("product_name") or "")[:40],
        })

    # 複数回検知された商品＝周期的に値下がりを繰り返す商品
    repeats = sorted(
        [
            {"asin": a, "times": len(v), "name": v[0]["name"], "history": v}
            for a, v in per_asin.items() if len(v) >= 2
        ],
        key=lambda x: -x["times"],
    )

    first, last = _jst(rows[0].get("found_at")), _jst(rows[-1].get("found_at"))
    days = max(1, (last - first).days + 1) if (first and last) else 1

    return {
        "count": len(rows),
        "period": {
            "from": first.strftime("%Y-%m-%d") if first else "",
            "to": last.strftime("%Y-%m-%d") if last else "",
            "days": days,
            "per_day": round(len(rows) / days, 2),
        },
        "unique_asins": len(per_asin),
        "by_weekday": [{"label": w, "count": by_weekday.get(w, 0)} for w in WD],
        "by_hour": [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)],
        "by_date": sorted(
            [{"date": k, "count": v} for k, v in by_date.items()], key=lambda x: x["date"]
        ),
        "drop_distribution": sorted(
            [{"range": k, "count": v} for k, v in drop_buckets.items()],
            key=lambda x: int(x["range"].split("〜")[0]),
        ),
        "drop_stats": {
            "avg": round(sum(drops) / len(drops), 1) if drops else 0,
            "max": round(max(drops), 1) if drops else 0,
            "min": round(min(drops), 1) if drops else 0,
        },
        "repeats": repeats[:20],
    }


@router.delete("/{deal_id}")
async def delete_deal(deal_id: str, current_user=Depends(get_current_user)):
    supabase.table("harvest_results").delete().eq("id", deal_id).eq("user_id", current_user.id).execute()
    return {"message": "削除しました"}


@router.post("/run")
async def run_harvest_now(background_tasks: BackgroundTasks, current_user=Depends(get_current_user)):
    """今すぐ刈り取りスキャンを実行"""
    from scheduler import run_harvest_for_user
    setting_res = supabase.table("harvest_settings").select("*").eq("user_id", current_user.id).execute()
    if setting_res.data:
        setting = setting_res.data[0]
    else:
        setting = {
            "user_id": current_user.id,
            "min_profit_rate": 15,
            "min_profit_amount": 500,
            "min_drop_rate": 20,
            "max_rank": 100000,
            "amazon_fee_rate": 15.4,
            "notify_enabled": False,
            "line_user_id": None,
        }
    background_tasks.add_task(run_harvest_for_user, setting)
    return {"message": "スキャンを開始しました。1〜2分後に結果が表示されます。"}
