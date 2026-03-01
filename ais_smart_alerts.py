import os
import json
import time
import hashlib
import datetime as dt
import threading
import requests
import websocket

# =========================
# ENV
# =========================
API_KEY = os.environ["AISSTREAM_API_KEY"]
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "240"))
DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "30"))
SEND_SUMMARY_REPORT = os.getenv("SEND_SUMMARY_REPORT", "1") == "1"
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", "12"))

STATE_FILE = "state.json"
KSA_TZ = dt.timezone(dt.timedelta(hours=3))

# =========================
# GEOFENCE
# =========================
RED_SEA = [[12, 32], [30, 32], [30, 44], [12, 44]]
GULF = [[22, 47], [31, 47], [31, 57], [22, 57]]

BOUNDING_BOXES = [RED_SEA, GULF]

# =========================
# HELPERS
# =========================
def now_ksa():
    return dt.datetime.now(tz=KSA_TZ)

# 🔥 حل مشكلة Telegram 400 (تقسيم الرسائل)
def send_telegram(text: str):
    MAX_LEN = 3500
    chunks = [text[i:i+MAX_LEN] for i in range(0, len(text), MAX_LEN)]

    for part in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id": CHAT, "text": part},
            timeout=25
        )
        try:
            r.raise_for_status()
        except:
            pass

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"dedup": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"dedup": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def dedup_key(mmsi, kind):
    return hashlib.sha1(f"{mmsi}|{kind}".encode()).hexdigest()

def should_alert(state, mmsi, kind):
    k = dedup_key(mmsi, kind)
    last = state["dedup"].get(k)
    if not last:
        return True
    last_dt = dt.datetime.fromisoformat(last)
    return (now_ksa() - last_dt) > dt.timedelta(minutes=DEDUP_MINUTES)

def mark_alert(state, mmsi, kind):
    state["dedup"][dedup_key(mmsi, kind)] = now_ksa().isoformat()

def guess_region(lat, lon):
    try:
        lat = float(lat); lon = float(lon)
    except:
        return ""
    if 12 <= lat <= 30 and 32 <= lon <= 44:
        return "البحر الأحمر"
    if 22 <= lat <= 31 and 47 <= lon <= 57:
        return "الخليج العربي"
    return ""

def build_alert(kind, mmsi, lat, lon, sog, region):
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    return f"""🚢 تنبيه بحري ذكي
🕒 {t}
📍 {lat},{lon}
🆔 MMSI: {mmsi}
🏷️ المنطقة: {region}
⚓ السرعة: {sog} knots
⚠️ السبب: {kind}
"""

# =========================
# SMART RULES
# =========================
def evaluate_and_alert(state, ship):
    mmsi = ship.get("UserID")
    lat = ship.get("Latitude")
    lon = ship.get("Longitude")
    sog = ship.get("Sog")

    if not mmsi or lat is None or lon is None or sog is None:
        return

    try:
        sog = float(sog)
    except:
        return

    region = guess_region(lat, lon)

    if sog == 0:
        kind = "توقف كامل"
    elif sog < 1:
        kind = "سرعة منخفضة جداً"
    else:
        return

    if should_alert(state, str(mmsi), kind):
        send_telegram(build_alert(kind, mmsi, lat, lon, sog, region))
        mark_alert(state, str(mmsi), kind)

# =========================
# REPORT
# =========================
def build_summary(stats, vessels):
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")

    lines = [
        "📡 تقرير حركة السفن (AIS)",
        f"🕒 {t}",
        "════════════════════",
        f"📨 الرسائل: {stats['messages']}",
        f"🚢 سفن فريدة: {len(vessels)}",
        "════════════════════"
    ]

    top = list(vessels.values())[:REPORT_TOP_N]

    if not top:
        lines.append("لا توجد بيانات سفن خلال فترة التشغيل.")
        return "\n".join(lines)

    for i, v in enumerate(top, 1):
        lines.append(
            f"{i}️⃣ MMSI {v['mmsi']} | {v['region']} | "
            f"{v['lat']},{v['lon']} | SOG {v['sog']}"
        )

    return "\n".join(lines)

# =========================
# RUNNER
# =========================
def run():
    state = load_state()

    stats = {"messages": 0}
    vessels = {}

    def on_open(ws):
        ws.send(json.dumps({
            "APIKey": API_KEY,
            "BoundingBoxes": BOUNDING_BOXES
        }))

    def on_message(ws, message):
        stats["messages"] += 1

        try:
            data = json.loads(message)
        except:
            return

        msg = data.get("Message", {})
        ship = msg.get("PositionReport") or msg.get("StandardClassBPositionReport")

        if not ship:
            return

        mmsi = ship.get("UserID")
        lat = ship.get("Latitude")
        lon = ship.get("Longitude")
        sog = ship.get("Sog")

        if mmsi and lat and lon:
            vessels[str(mmsi)] = {
                "mmsi": mmsi,
                "lat": lat,
                "lon": lon,
                "sog": sog,
                "region": guess_region(lat, lon)
            }

        evaluate_and_alert(state, ship)

    def on_close(ws, code, reason):
        save_state(state)

    ws = websocket.WebSocketApp(
        "wss://stream.aisstream.io/v0/stream",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close
    )

    def stop():
        time.sleep(RUN_SECONDS)
        ws.close()

    threading.Thread(target=stop, daemon=True).start()
    ws.run_forever()

    if SEND_SUMMARY_REPORT:
        send_telegram(build_summary(stats, vessels))

if __name__ == "__main__":
    run()
