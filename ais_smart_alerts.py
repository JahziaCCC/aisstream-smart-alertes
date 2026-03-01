import os
import json
import time
import math
import hashlib
import datetime as dt
import requests
import websocket

# =========================
# ENV
# =========================
API_KEY = os.environ["AISSTREAM_API_KEY"]
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

# تشغيل محدود (مهم لـ GitHub Actions)
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "360"))  # 6 دقائق افتراضياً

# منع تكرار التنبيه لنفس السفينة خلال X دقائق
DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "30"))

STATE_FILE = "state.json"

KSA_TZ = dt.timezone(dt.timedelta(hours=3))


# =========================
# GEOFENCE (Bounding Boxes)
# صيغة AISstream: كل BoundingBox = 4 نقاط [lat, lon]
# =========================
RED_SEA = [[12, 32], [30, 32], [30, 44], [12, 44]]
GULF    = [[22, 47], [31, 47], [31, 57], [22, 57]]

BOUNDING_BOXES = [RED_SEA, GULF]


# =========================
# Helpers
# =========================
def now_ksa():
    return dt.datetime.now(tz=KSA_TZ)

def send_telegram(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=20
    )
    r.raise_for_status()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"dedup": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"dedup": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def dedup_key(mmsi: str, kind: str):
    raw = f"{mmsi}|{kind}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def should_alert(state, mmsi: str, kind: str) -> bool:
    k = dedup_key(mmsi, kind)
    last = state["dedup"].get(k)
    if not last:
        return True
    last_dt = dt.datetime.fromisoformat(last)
    return (now_ksa() - last_dt) > dt.timedelta(minutes=DEDUP_MINUTES)

def mark_alert(state, mmsi: str, kind: str):
    k = dedup_key(mmsi, kind)
    state["dedup"][k] = now_ksa().isoformat()

def fmt(x, nd=4):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)

def safe_get(d, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


# =========================
# Smart rules (B)
# =========================
# نرسل تنبيه عند:
# 1) سرعة منخفضة جداً (Loitering/Drifting) SOG < 1
# 2) توقف فعلي SOG == 0 (أكثر حساسية)
# 3) تغير سرعة مفاجئ (اختياري بسيط)
# ملاحظة: AISstream قد يرسل أنواع رسائل مختلفة - احنا نركز على PositionReport
# =========================

def build_message(kind, mmsi, lat, lon, sog, cog=None, heading=None, region=""):
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    lines = [
        "🚢 تنبيه بحري ذكي (AIS)",
        f"🕒 {t}",
        f"📍 الموقع: {fmt(lat)},{fmt(lon)}",
        f"🆔 MMSI: {mmsi}",
        f"🏷️ المنطقة: {region}" if region else None,
        f"⚓ السرعة: {sog} knots",
        f"🧭 COG: {cog}" if cog is not None else None,
        f"🧭 Heading: {heading}" if heading is not None else None,
        f"⚠️ السبب: {kind}",
        "✅ التوصية: يحتاج متابعة"
    ]
    return "\n".join([x for x in lines if x])

def guess_region(lat, lon):
    # تخمين خفيف فقط للعرض
    try:
        lat = float(lat); lon = float(lon)
    except Exception:
        return ""
    if 12 <= lat <= 30 and 32 <= lon <= 44:
        return "البحر الأحمر"
    if 22 <= lat <= 31 and 47 <= lon <= 57:
        return "الخليج العربي"
    return ""

def evaluate_and_alert(state, ship):
    mmsi = ship.get("UserID") or ship.get("Mmsi") or ship.get("MMSI") or ship.get("mmsi")
    if not mmsi:
        return

    lat = ship.get("Latitude")
    lon = ship.get("Longitude")
    sog = ship.get("Sog")

    if lat is None or lon is None or sog is None:
        return

    try:
        sog_f = float(sog)
    except Exception:
        return

    cog = ship.get("Cog")
    heading = ship.get("TrueHeading")

    region = guess_region(lat, lon)

    # Rule 1: توقف كامل
    if sog_f == 0.0:
        kind = "توقف كامل داخل النطاق"
        if should_alert(state, str(mmsi), kind):
            send_telegram(build_message(kind, mmsi, lat, lon, sog_f, cog, heading, region))
            mark_alert(state, str(mmsi), kind)
        return

    # Rule 2: سرعة منخفضة جداً
    if sog_f < 1.0:
        kind = "سرعة منخفضة جداً (Loitering/Drifting)"
        if should_alert(state, str(mmsi), kind):
            send_telegram(build_message(kind, mmsi, lat, lon, sog_f, cog, heading, region))
            mark_alert(state, str(mmsi), kind)
        return


# =========================
# WebSocket
# =========================
def run():
    state = load_state()

    start = time.time()
    stop_at = start + RUN_SECONDS

    def on_open(ws):
        sub = {
            "APIKey": API_KEY,
            "BoundingBoxes": BOUNDING_BOXES
        }
        ws.send(json.dumps(sub))

    def on_message(ws, message):
        # وقف بعد مدة محددة (مهم لـ Actions)
        if time.time() >= stop_at:
            ws.close()
            return

        try:
            data = json.loads(message)
        except Exception:
            return

        msg = data.get("Message")
        if not isinstance(msg, dict):
            return

        # PositionReport
        pr = msg.get("PositionReport")
        if isinstance(pr, dict):
            evaluate_and_alert(state, pr)
            return

        # بعض الرسائل قد تكون "StandardClassBPositionReport"
        prb = msg.get("StandardClassBPositionReport")
        if isinstance(prb, dict):
            evaluate_and_alert(state, prb)
            return

    def on_error(ws, error):
        # لا نفجر التشغيل بسبب خطأ واحد
        pass

    def on_close(ws, code, reason):
        save_state(state)

    ws = websocket.WebSocketApp(
        "wss://stream.aisstream.io/v0/stream",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    # ping للحفاظ على الاتصال
    ws.run_forever(ping_interval=20, ping_timeout=10)


if __name__ == "__main__":
    run()
