import os
import requests
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")


def send_line_notification(line_user_id: str, deals: list):
    print(
        f"[LINE] 通知試行: user_id={line_user_id} token有={bool(LINE_CHANNEL_ACCESS_TOKEN)} 件数={len(deals)}",
        flush=True,
    )
    if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
        print("[LINE] トークンまたはユーザーIDが未設定のためスキップ", flush=True)
        return

    message = _format_message(deals)
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": line_user_id,
        "messages": [{"type": "text", "text": message}],
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[LINE] 送信結果: status={response.status_code} body={response.text[:200]}", flush=True)
    except Exception as e:
        print(f"[LINE] 送信エラー: {e}", flush=True)


def _format_message(deals: list) -> str:
    msg = f"🔥【Amazon刈り取り】{len(deals)}件見つかりました！\n\n"

    for d in deals[:5]:
        drop = d.get("price_drop_rate", 0)
        profit = d.get("profit_amount", 0)
        profit_rate = d.get("profit_rate", 0)
        buy = d.get("current_price", 0)
        sell = d.get("regular_price", 0)
        asin = d.get("amazon_asin", "")

        msg += f"▼ {d['product_name'][:25]}\n"
        msg += f"🛒 仕入: ¥{buy:,} → 💰 転売: ¥{sell:,}\n"
        msg += f"📉 値下がり: {drop}%  利益: ¥{profit:,}（{profit_rate}%）\n"
        msg += f"🏆 ランク: {d.get('amazon_rank', 0):,}位\n"
        msg += f"👉 https://www.amazon.co.jp/dp/{asin}\n\n"

    if len(deals) > 5:
        msg += f"他 {len(deals) - 5}件はアプリでご確認ください。"

    return msg.strip()
