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
# ✅ AISSTREAM BoundingBoxes FORMAT (Two corners ONLY)
# Each bbox: [[lat1, lon1], [lat2, lon2]]
# Docs: BoundingBoxes is REQUIRED.  [oai_citation:2‡aisstream.io](https://aisstream.io/documentation)
# =========================
RED_SEA_BBOX = [[12, 32], [30, 44]]   # SW -> NE (order doesn't matter)
GULF_BBOX    = [[22, 47], [31, 57]]

BOUNDING_BOXES = [RED_SEA_BBOX, GULF_BBOX]

# لتخفيف الضغط: نستقبل فقط رسائل المواقع
FILTER_MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport"]  # اختياري حسب التوثيق  [oai_citation:3‡aisstream.io](https://aisstream.io/documentation)


# =========================
# HELPERS
# =========================
def now_ksa():
    return dt.datetime.now(tz=KSA_TZ)

def send_telegram(text: str):
    # ✅ حل 400: تقسيم الرسائل الطويلة
    MAX_LEN = 3500
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)]

    for part in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id": CHAT, "text": part},
            timeout=25
        )
        # ما نطيح التشغيل لو جزء فشل
        try:
            r.raise_for_status()
        except Exception:
            pass

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

def dedup_key(mmsi: str, kind: str) -> str:
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
    state["dedup"][dedup_key(mmsi, kind)] = now_ksa().isoformat()

def fmt(x, nd=4):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)

def guess_region(lat, lon):
    try:
        lat = float(lat); lon = float(lon)
    except Exception:
        return ""
    if 12 <= lat <= 30 and 32 <= lon <= 44:
        return "البحر الأحمر"
    if 22 <= lat <= 31 and 47 <= lon <= 57:
        return "الخليج العربي"
    return ""

def build_alert(kind, mmsi, lat, lon, sog, region):
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    return (
        "🚢 تنبيه بحري ذكي (AIS)\n"
        f"🕒 {t}\n"
        f"📍 الموقع: {fmt(lat)},{fmt(lon)}\n"
        f"🆔 MMSI: {mmsi}\n"
        f"🏷️ المنطقة: {region}\n"
        f"⚓ السرعة: {sog} knots\n"
        f"⚠️ السبب: {kind}\n"
        "✅ التوصية: يحتاج متابعة"
    )

# =========================
# SMART RULES (B)
# =========================
def evaluate_and_alert(state, ship):
    mmsi = ship.get("UserID") or ship.get("Mmsi") or ship.get("MMSI") or ship.get("mmsi")
    lat = ship.get("Latitude")
    lon = ship.get("Longitude")
    sog = ship.get("Sog")

    if not mmsi or lat is None or lon is None or sog is None:
        return

    try:
        sog_f = float(sog)
    except Exception:
        return

    region = guess_region(lat, lon)

    if sog_f == 0.0:
        kind = "توقف كامل داخل النطاق"
    elif sog_f < 1.0:
        kind = "سرعة منخفضة جداً (Loitering/Drifting)"
    else:
        return

    if should_alert(state, str(mmsi), kind):
        send_telegram(build_alert(kind, mmsi, lat, lon, sog_f, region))
        mark_alert(state, str(mmsi), kind)

# =========================
# SUMMARY REPORT
# =========================
def build_summary(run_stats, vessels):
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    total_msgs = run_stats["messages"]
    total_pos = run_stats["pos_reports"]
    unique = len(vessels)

    red_count = sum(1 for v in vessels.values() if v.get("region") == "البحر الأحمر")
    gulf_count = sum(1 for v in vessels.values() if v.get("region") == "الخليج العربي")

    rows = list(vessels.values())
    rows.sort(key=lambda x: (x.get("last_ts", 0), x.get("sog", -1)), reverse=True)
    top = rows[:REPORT_TOP_N]

    lines = [
        "📡 تقرير حركة السفن (AIS) — ملخص تشغيلي",
        f"🕒 {t}",
        "════════════════════",
        f"📨 إجمالي الرسائل: {total_msgs}",
        f"📍 تقارير المواقع: {total_pos}",
        f"🚢 سفن فريدة (MMSI): {unique}",
        f"🌊 البحر الأحمر: {red_count} | الخليج العربي: {gulf_count}",
        "════════════════════",
        f"🔎 أعلى {len(top)} سفن (آخر تحديث داخل النطاق):"
    ]

    if not top:
        lines.append("• لا توجد سفن مستلمة خلال نافذة التشغيل (راجع صلاحية المفتاح/الاشتراك إذا تكرر).")
        return "\n".join(lines)

    for i, v in enumerate(top, 1):
        lines.append(
            f"{i}️⃣ MMSI {v['mmsi']} | {v.get('region','')} | "
            f"{fmt(v.get('lat'))},{fmt(v.get('lon'))} | SOG {v.get('sog','?')} kn"
        )

    return "\n".join(lines)

# =========================
# RUNNER
# =========================
def run():
    state = load_state()

    run_stats = {"messages": 0, "pos_reports": 0}
    vessels = {}

    def on_open(ws):
        # ✅ Subscription must include APIKey + BoundingBoxes (Required)  [oai_citation:4‡aisstream.io](https://aisstream.io/documentation)
        sub = {
            "APIKey": API_KEY,
            "BoundingBoxes": BOUNDING_BOXES,
            "FilterMessageTypes": FILTER_MESSAGE_TYPES
        }
        ws.send(json.dumps(sub))

    def on_message(ws, message):
        run_stats["messages"] += 1

        try:
            data = json.loads(message)
        except Exception:
            return

        msg = data.get("Message")
        if not isinstance(msg, dict):
            return

        ship = None
        if isinstance(msg.get("PositionReport"), dict):
            ship = msg["PositionReport"]
        elif isinstance(msg.get("StandardClassBPositionReport"), dict):
            ship = msg["StandardClassBPositionReport"]

        if not isinstance(ship, dict):
            return

        run_stats["pos_reports"] += 1

        mmsi = ship.get("UserID") or ship.get("Mmsi")
        lat = ship.get("Latitude")
        lon = ship.get("Longitude")
        sog = ship.get("Sog")

        if mmsi and lat is not None and lon is not None and sog is not None:
            try:
                sog_f = float(sog)
            except Exception:
                sog_f = sog
            region = guess_region(lat, lon)
            vessels[str(mmsi)] = {
                "mmsi": str(mmsi),
                "lat": lat,
                "lon": lon,
                "sog": sog_f,
                "region": region,
                "last_ts": time.time()
            }

        evaluate_and_alert(state, ship)

    def on_error(ws, error):
        # لو صار خطأ اتصال، أرسل ملخص صغير
        try:
            send_telegram(f"⚠️ AISstream WebSocket error: {error}")
        except Exception:
            pass

    def on_close(ws, code, reason):
        save_state(state)
        # مفيد للتشخيص لو استمر الصفر
        try:
            send_telegram(f"ℹ️ AISstream connection closed: code={code} reason={reason}")
        except Exception:
            pass

    ws = websocket.WebSocketApp(
        "wss://stream.aisstream.io/v0/stream",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    def stop_ws():
        time.sleep(RUN_SECONDS)
        try:
            ws.close()
        except Exception:
            pass

    threading.Thread(target=stop_ws, daemon=True).start()
    ws.run_forever(ping_interval=20, ping_timeout=10)

    if SEND_SUMMARY_REPORT:
        send_telegram(build_summary(run_stats, vessels))

if __name__ == "__main__":
    run()
