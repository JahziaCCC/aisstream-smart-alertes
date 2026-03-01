import os, json, time, threading, datetime as dt
import requests, websocket

API_KEY = os.environ["AISSTREAM_API_KEY"]
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "60"))
KSA_TZ = dt.timezone(dt.timedelta(hours=3))

def now_ksa():
    return dt.datetime.now(tz=KSA_TZ)

def send_telegram(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=25
    )
    r.raise_for_status()

def run():
    stats = {"messages": 0, "pos_reports": 0}
    vessels = set()

    def on_open(ws):
        # ✅ اختبار عالمي بدون BoundingBoxes
        ws.send(json.dumps({"APIKey": API_KEY}))

    def on_message(ws, message):
        stats["messages"] += 1
        try:
            data = json.loads(message)
        except:
            return
        msg = data.get("Message")
        if not isinstance(msg, dict):
            return
        ship = msg.get("PositionReport") or msg.get("StandardClassBPositionReport")
        if not isinstance(ship, dict):
            return
        stats["pos_reports"] += 1
        mmsi = ship.get("UserID") or ship.get("Mmsi")
        if mmsi:
            vessels.add(str(mmsi))

    ws = websocket.WebSocketApp(
        "wss://stream.aisstream.io/v0/stream",
        on_open=on_open,
        on_message=on_message
    )

    def stop():
        time.sleep(RUN_SECONDS)
        try:
            ws.close()
        except:
            pass

    threading.Thread(target=stop, daemon=True).start()
    ws.run_forever(ping_interval=20, ping_timeout=10)

    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    send_telegram(
        "🧪 AISstream Global Test\n"
        f"🕒 {t}\n"
        "════════════════════\n"
        f"📨 إجمالي الرسائل: {stats['messages']}\n"
        f"📍 تقارير المواقع: {stats['pos_reports']}\n"
        f"🚢 سفن فريدة (MMSI): {len(vessels)}\n"
    )

if __name__ == "__main__":
    run()
