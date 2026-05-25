import os
import requests
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")


# ────────────────────────────────────────────
# Discord通知（リッチEmbedで即時通知）
# ────────────────────────────────────────────

def send_discord_notification(webhook_url: str, deals: list):
    if not webhook_url:
        return
    print(f"[DISCORD] 通知送信: {len(deals)}件", flush=True)

    for deal in deals[:10]:  # 最大10件
        asin = deal.get("amazon_asin") or deal.get("asin", "")
        buy = deal.get("current_price", 0)
        sell = deal.get("regular_price", 0)
        profit = deal.get("profit_amount", 0)
        profit_rate = deal.get("profit_rate", 0)
        drop = deal.get("price_drop_rate", 0)
        rank = deal.get("amazon_rank", 0)
        name = deal.get("product_name", "")[:100]

        # 利益率で色を変える
        color = 0x00C851 if profit_rate >= 20 else 0xFF8800 if profit_rate >= 15 else 0xFFCC00

        embed = {
            "title": f"🔥 {name}",
            "url": f"https://www.amazon.co.jp/dp/{asin}",
            "color": color,
            "fields": [
                {"name": "🛒 仕入れ値（今すぐ購入）", "value": f"**¥{buy:,}**", "inline": True},
                {"name": "💰 転売価格（通常価格）", "value": f"¥{sell:,}", "inline": True},
                {"name": "📉 値下がり率", "value": f"**{drop}%OFF**", "inline": True},
                {"name": "📈 予想利益額", "value": f"**¥{profit:,}**", "inline": True},
                {"name": "📊 利益率", "value": f"**{profit_rate}%**", "inline": True},
                {"name": "🏆 ランキング", "value": f"{rank:,}位", "inline": True},
            ],
            "footer": {
                "text": "Amazon刈り取りモニター | Keepa連携",
            },
        }

        # KeepaのグラフURLをサムネイルに
        if asin:
            embed["thumbnail"] = {
                "url": f"https://graph.keepa.com/pricehistory.png?asin={asin}&domain=5&range=90&salesrank=1&amazon=1"
            }

        payload = {
            "username": "🔥 Amazon刈り取りbot",
            "embeds": [embed],
        }

        try:
            res = requests.post(webhook_url, json=payload, timeout=10)
            print(f"[DISCORD] status={res.status_code} asin={asin}", flush=True)
        except Exception as e:
            print(f"[DISCORD] エラー: {e}", flush=True)


# ────────────────────────────────────────────
# LINE通知
# ────────────────────────────────────────────

def send_line_notification(line_user_id: str, deals: list):
    if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
        print("[LINE] トークンまたはユーザーIDが未設定のためスキップ", flush=True)
        return

    message = _format_line_message(deals)
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
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[LINE] 送信結果: status={res.status_code}", flush=True)
    except Exception as e:
        print(f"[LINE] エラー: {e}", flush=True)


def _format_line_message(deals: list) -> str:
    msg = f"🔥【刈り取り発見！】{len(deals)}件\n\n"
    for d in deals[:3]:
        asin = d.get("amazon_asin") or d.get("asin", "")
        msg += f"▼ {d['product_name'][:22]}\n"
        msg += f"仕入: ¥{d['current_price']:,} / 転売: ¥{d['regular_price']:,}\n"
        msg += f"値下がり: {d['price_drop_rate']}%  利益: ¥{d['profit_amount']:,}（{d['profit_rate']}%）\n"
        msg += f"👉 https://www.amazon.co.jp/dp/{asin}\n\n"
    if len(deals) > 3:
        msg += f"他 {len(deals) - 3}件はアプリでご確認ください。"
    return msg.strip()


# ────────────────────────────────────────────
# まとめて通知（Discord優先 → LINE補完）
# ────────────────────────────────────────────

def notify_all(setting: dict, deals: list):
    if not deals:
        return

    discord_url = setting.get("discord_webhook_url", "")
    line_user_id = setting.get("line_user_id", "")
    notify_enabled = setting.get("notify_enabled", False)

    if not notify_enabled:
        return

    if discord_url:
        send_discord_notification(discord_url, deals)

    if line_user_id:
        send_line_notification(line_user_id, deals)
