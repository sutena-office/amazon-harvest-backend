import threading
import re
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from database import supabase
from auth import get_current_user

router = APIRouter()


class SeedInput(BaseModel):
    asin_or_url: str


def _extract_asin(text: str) -> str:
    """ASIN直接入力にもAmazon URL貼り付けにも対応"""
    text = text.strip()
    m = re.search(r"/dp/([A-Z0-9]{10})", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b([A-Z0-9]{10})\b", text, re.IGNORECASE)
    return m.group(1).upper() if m else ""


@router.post("/seed")
async def register_seed(payload: SeedInput, current_user=Depends(get_current_user)):
    """モデル商品（種）を登録し、類似商品の発掘バッチを開始する"""
    from research.yahoo_api import is_configured
    if not is_configured():
        return {"started": False, "message": "Yahoo! Client IDが未設定です（Render環境変数 YAHOO_CLIENT_ID）"}

    asin = _extract_asin(payload.asin_or_url)
    if not asin:
        return {"started": False, "message": "ASINが読み取れません（例: B08XYZ1234 またはAmazonの商品URL）"}

    try:
        running = (
            supabase.table("sourcing_seeds")
            .select("id")
            .eq("user_id", current_user.id)
            .eq("status", "running")
            .execute()
        )
        if running.data:
            return {"started": False, "message": "発掘ジョブが既に実行中です"}

        from research.sourcing import analyze_seed
        traits = analyze_seed(asin)
        if not traits:
            return {"started": False, "message": "商品情報を取得できません（トークン切れ or ASIN不正）"}

        seed = (
            supabase.table("sourcing_seeds")
            .insert({
                "user_id": current_user.id,
                "asin": asin,
                "title": traits["title"],
                "brand": traits["brand"],
                "root_category": traits["root_category"],
                "reference_price": traits["reference_price"],
                "status": "running",
            })
            .execute()
        )
        seed_id = seed.data[0]["id"]
    except Exception as e:
        print(f"[SOURCING] 種登録エラー: {e}", flush=True)
        return {"started": False, "message": f"エラー: {str(e)[:200]}"}

    from research.sourcing import run_sourcing_job
    thread = threading.Thread(
        target=run_sourcing_job,
        args=(seed_id, current_user.id, traits),
        daemon=True,
    )
    thread.start()

    return {
        "started": True,
        "seed": {"asin": asin, "title": traits["title"], "brand": traits["brand"]},
        "message": f"「{traits['title'][:40]}」を種にして類似商品の発掘を開始しました",
    }


@router.get("/debug-yahoo")
async def debug_yahoo(jan: str, current_user=Depends(get_current_user)):
    """指定JANのYahoo!生レスポンス（ポイント内訳）を確認する診断用"""
    import os, requests
    from research.yahoo_api import API_URL, _effective_price
    from research.yahoo_points import effective_cost
    client_id = os.getenv("YAHOO_CLIENT_ID")
    if not client_id:
        return {"error": "YAHOO_CLIENT_ID未設定"}
    params = {
        "appid": client_id, "jan_code": jan, "condition": "new",
        "in_stock": "true", "results": 3, "sort": "+price",
    }
    try:
        res = requests.get(API_URL, params=params, timeout=15)
        hits = (res.json() or {}).get("hits") or []
        return {
            "status": res.status_code,
            "count": len(hits),
            "items": [
                {
                    "name": (h.get("name") or "")[:60],
                    "price": h.get("price"),
                    "raw_point": h.get("point"),
                    "api_parsed": _effective_price(h),
                    "cost_model": effective_cost(
                        int(h.get("price") or 0),
                        store_point=_effective_price(h)["store_point"],
                    ),
                }
                for h in hits
            ],
        }
    except Exception as e:
        return {"error": str(e)[:300]}


@router.get("/seeds")
async def list_seeds(current_user=Depends(get_current_user)):
    res = (
        supabase.table("sourcing_seeds")
        .select("*")
        .eq("user_id", current_user.id)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    )
    return res.data


@router.get("/candidates")
async def list_candidates(current_user=Depends(get_current_user), limit: int = 100):
    """発掘済み候補を返す。実務基準のグレード（S/A/B/C）順→月間期待利益順。
    グレードは 月販200個以上・ランク1万位以内・出品者10人以上 で判定する。"""
    res = (
        supabase.table("sourcing_candidates")
        .select("*")
        .eq("user_id", current_user.id)
        .order("expected_monthly_profit", desc=True)
        .limit(limit)
        .execute()
    )
    return sort_by_grade(res.data or [])


# S/A/B/C はアルファベット順に並べるとSが最後になるため明示的に順位を持たせる
GRADE_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}


def sort_by_grade(rows: list) -> list:
    """グレード順（S→A→B→C）→月間利益の大きい順に並べ替える"""
    return sorted(
        rows,
        key=lambda r: (
            GRADE_ORDER.get(r.get("grade"), 9),
            -(r.get("expected_monthly_profit") or 0),
            -(r.get("profit_amount") or 0),
        ),
    )


class RescanInput(BaseModel):
    # auto=実行日の日付から判定 / normal / five_day / five_sun / matsuri
    scenario: str = "auto"


@router.get("/rescan-status")
async def rescan_status(current_user=Depends(get_current_user)):
    """直近の再チェックが成功したか（上限で中断していないか）を返す"""
    from research import sourcing as S
    return S.LAST_RESCAN or {"status": "none"}


@router.get("/campaign")
async def campaign(current_user=Depends(get_current_user)):
    """今日の還元状況と、今後2週間の仕入れ狙い目日を返す"""
    from research.yahoo_points import campaign_for_date, upcoming_best_days
    return {"today": campaign_for_date(), "upcoming": upcoming_best_days(14)}


@router.get("/export")
async def export_csv(current_user=Depends(get_current_user), only_profitable: bool = True):
    """仕入れ候補をCSVで書き出す（Excel/Googleスプレッドシートでそのまま開ける）"""
    import csv, io
    from fastapi.responses import StreamingResponse
    from datetime import datetime, timezone, timedelta

    q = (
        supabase.table("sourcing_candidates")
        .select("*")
        .eq("user_id", current_user.id)
    )
    rows = (q.execute()).data or []
    if only_profitable:
        rows = [r for r in rows if (r.get("profit_amount") or 0) > 0]
    rows = sort_by_grade(rows)   # S→A→B→C の順（アルファベット順ではSが最後になるため）

    from research.yahoo_points import (
        campaign_for_date, campaigns, active_campaign_keys, ASSUMED_COUPON, TAX_RATE,
    )
    from research.sourcing import (
        FEE_RATE, MAX_UNITS_PER_MONTH,
        CRIT_SALES_MIN, CRIT_RANK_IDEAL, CRIT_RANK_MAX, CRIT_SELLERS_MIN,
    )

    COURSE_PRICE = 550000
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    wd = "月火水木金土日"[now.weekday()]

    # 表の数値は「最後に再チェックした条件」で計算されている。
    # 出力日の条件ではなくそちらを表示しないと、ヘッダーと中身が食い違う
    from research import sourcing as S
    last = S.LAST_RESCAN or {}
    used_scenario = last.get("scenario") or "auto"
    camp = campaign_for_date(now, used_scenario)
    SCENARIO_LABEL = {
        "auto": "実行日の日付から自動判定", "normal": "通常日",
        "sunday": "日曜（プレミアムな日曜日）", "five_day": "5のつく日",
        "five_sun": "5のつく日＋日曜", "matsuri": "超PayPay祭",
    }

    buf = io.StringIO()
    buf.write("﻿")  # ExcelでUTF-8を正しく開くためのBOM
    w = csv.writer(buf)

    # ── 条件ヘッダー（この数字がいつ・どんな前提で出たものかを明記する）──
    w.writerow(["■ Yahoo!仕入れ候補リスト"])
    w.writerow(["CSV出力日時", now.strftime(f"%Y-%m-%d（{wd}） %H:%M") + " JST"])
    w.writerow(["★ 計算に使った仕入れ日条件",
                SCENARIO_LABEL.get(used_scenario, used_scenario)
                + "（この条件で実質価格・利益を算出しています）"])
    if last.get("finished_at"):
        try:
            _f = datetime.fromisoformat(last["finished_at"]).astimezone(jst)
            _fw = "月火水木金土日"[_f.weekday()]
            w.writerow(["価格を取得した日時",
                        _f.strftime(f"%Y-%m-%d（{_fw}） %H:%M") + " JST"
                        + f"（{last.get('checked',0)}/{last.get('total',0)}件を更新）"])
        except Exception:
            pass
    if last.get("status") == "aborted":
        w.writerow(["⚠️ 注意", "前回の再チェックは途中で中断しました。"
                    "一部の行は古い条件のままの可能性があります"])
    w.writerow(["名目の総還元率", f'{camp["rate"]:g}%（付与上限に当たらない場合）'])
    w.writerow(["ポイント算定の基準", f"税抜価格ベース（税込 ÷ {TAX_RATE}）"])
    w.writerow(["想定クーポン", f"{ASSUMED_COUPON:,}円" if ASSUMED_COUPON else "見込まない（0円）"])
    w.writerow([])
    w.writerow(["■ 適用キャンペーンと付与上限（上限はキャンペーンごとに個別適用）"])
    w.writerow(["キャンペーン", "付与率", "付与上限", "上限到達の目安（税抜）"])
    _defs = campaigns()
    for _k in active_campaign_keys(now, camp["scenario"]):
        _c = _defs[_k]
        if _c["cap"] > 0:
            _reach = f'{int(_c["cap"] / (_c["rate"] / 100)):,}円'
            _cap = f'{_c["cap"]:,}pt'
        else:
            _reach, _cap = "—", "上限なし"
        w.writerow([_c["label"], f'{_c["rate"]:g}%', _cap, _reach])
    w.writerow([])
    w.writerow(["実質仕入れ値の計算式",
                "（Yahoo!表示価格 − クーポン） − Σ min(各キャンペーンの還元額, そのキャンペーンの上限)"])
    w.writerow(["利益の計算式", f"Amazon売価 ×（1 − 経費{FEE_RATE*100:g}%）− 実質仕入れ値"])
    w.writerow(["月間利益の前提",
                f"1商品あたり月{MAX_UNITS_PER_MONTH}個まで（週1回の仕入れ日に1個ずつ。"
                f"Yahoo!ストアの購入制限とポイント付与上限を考慮）／"
                f"カートで取れる見込み個数（月販 ÷ 出品者数+1）の小さい方"])
    w.writerow(["判定基準",
                f"月販{CRIT_SALES_MIN}個以上／ランク{CRIT_RANK_IDEAL:,}位以内（上限{CRIT_RANK_MAX:,}位）／"
                f"出品者{CRIT_SELLERS_MIN}人以上"])
    w.writerow(["注意",
                "ポイント還元・クーポンは購入時点の条件で変動します。"
                "高額商品の仕入れ前にYahoo!の商品ページで実際の付与ポイントを確認してください"])
    w.writerow([])

    # ── 月30万円までの到達見込み（上位から積み上げ）──
    _ranked = sorted(rows, key=lambda r: -(r.get("expected_monthly_profit") or 0))
    _total = sum(r.get("expected_monthly_profit") or 0 for r in _ranked)
    _acc, _need = 0, 0
    for _r in _ranked:
        if _acc >= 300000:
            break
        _acc += _r.get("expected_monthly_profit") or 0
        _need += 1
    _avg = int(_total / len(_ranked)) if _ranked else 0
    w.writerow(["■ 月30万円までの見込み"])
    w.writerow(["利益が出る候補数", f"{len(_ranked)}件"])
    w.writerow(["1商品あたりの平均月間利益", f"{_avg:,}円（月{MAX_UNITS_PER_MONTH}個前提）"])
    w.writerow(["全候補を回した場合の月間利益", f"{_total:,}円"])
    if _acc >= 300000:
        w.writerow(["月30万円に必要な品目数", f"上位{_need}品目"])
    else:
        w.writerow(["月30万円への不足額", f"{300000 - _total:,}円（現候補では届きません）"])
    w.writerow([])

    w.writerow([
        "グレード", "商品名", "ASIN", "JAN",
        "Amazonランク", "月販数", "出品者数",
        "Yahoo!表示価格", "ポイント還元", "実質仕入れ値",
        "Amazon売価", "利益/個", "利益率(%)",
        f"月間利益(月{MAX_UNITS_PER_MONTH}個前提)", "月の仕入れ可能数",
        "講座ペイ月数", "月30万に必要な商品数",
        "Yahoo!商品URL", "Amazon商品URL", "Keepaグラフ",
        "ストア名", "判定メモ",
    ])
    for r in rows:
        monthly = r.get("student_monthly_profit") or 0
        profit_each = r.get("profit_amount") or 0
        sales = r.get("est_monthly_sales") or 0
        sellers = r.get("seller_count") or 0
        cart_units = sales / max(sellers + 1, 1) if sales else 0
        units = min(MAX_UNITS_PER_MONTH, round(cart_units, 1)) if profit_each > 0 else 0
        payback = round(COURSE_PRICE / monthly, 1) if monthly > 0 else ""
        need = -(-300000 // monthly) if monthly > 0 else ""  # 切り上げ
        w.writerow([
            r.get("grade", ""), r.get("product_name", ""), r.get("asin", ""), r.get("jan", ""),
            r.get("amazon_rank", 0), sales, sellers,
            r.get("yahoo_price", 0), r.get("yahoo_point", 0), r.get("yahoo_effective", 0),
            r.get("amazon_price", 0), profit_each, r.get("profit_rate", 0),
            monthly, units, payback, need,
            r.get("yahoo_url", ""),
            f"https://www.amazon.co.jp/dp/{r.get('asin','')}",
            f"https://keepa.com/#!product/5-{r.get('asin','')}",
            r.get("yahoo_store", ""), r.get("grade_note", ""),
        ])

    buf.seek(0)
    jst = timezone(timedelta(hours=9))
    fname = f"sourcing_{datetime.now(jst).strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/rescan")
async def rescan(payload: RescanInput = None, current_user=Depends(get_current_user)):
    """Yahoo!側の価格・ポイントを再チェック（無料・トークン消費なし）。
    scenarioで「どの仕入れ日として計算するか」を切り替える。
    各キャンペーンの付与上限が個別に適用される。"""
    from research.sourcing import rescan_until_complete
    from research.yahoo_points import campaign_for_date, SCENARIOS

    scenario = (payload.scenario if payload else "auto") or "auto"
    if scenario not in SCENARIOS:
        return {"started": False, "message": f"不正な条件: {scenario}"}

    camp = campaign_for_date(scenario=scenario)
    # レート制限で中断しても時間をおいて自動再開し、全件更新まで粘る
    thread = threading.Thread(
        target=rescan_until_complete,
        kwargs={"user_id": current_user.id, "scenario": scenario},
        daemon=True,
    )
    thread.start()
    return {
        "started": True,
        "message": f"再チェックを開始しました（名目還元 {camp['rate']}%／{camp['summary']}）"
                   "。上限に当たった場合は自動で待機して再開します",
    }


@router.delete("/candidate/{candidate_id}")
async def delete_candidate(candidate_id: str, current_user=Depends(get_current_user)):
    supabase.table("sourcing_candidates").delete().eq("id", candidate_id).eq(
        "user_id", current_user.id
    ).execute()
    return {"ok": True}
