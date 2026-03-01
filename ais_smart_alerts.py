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

RUN_SECONDS = int(os.getenv("RUN_SECONDS", "240"))          # مدة الاستماع
DEDUP_MINUTES = int(os.getenv("DEDUP_MINUTES", "30"))       # منع تكرار التنبيه
SEND_SUMMARY_REPORT = os.getenv("SEND_SUMMARY_REPORT", "0") == "1"
REPORT_TOP_N = int(os.getenv("REPORT_TOP_N", "12"))

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
        timeout=25
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

def build_alert(kind, mmsi, lat, lon, sog, cog=None, heading=None, region=""):
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


# =========================
# Smart rules (B) + Collection for report
# =========================
def extract_ship_fields(ship: dict):
    mmsi = ship.get("UserID") or ship.get("Mmsi") or ship.get("MMSI") or ship.get("mmsi")
    lat = ship.get("Latitude")
    lon = ship.get("Longitude")
    sog = ship.get("Sog")
    cog = ship.get("Cog")
    heading = ship.get("TrueHeading")
    return mmsi, lat, lon, sog, cog, heading

def evaluate_and_alert(state, ship):
    mmsi, lat, lon, sog, cog, heading = extract_ship_fields(ship)
    if not mmsi or lat is None or lon is None or sog is None:
        return

    try:
        sog_f = float(sog)
    except Exception:
        return

    region = guess_region(lat, lon)

    # Rule 1: توقف كامل
    if sog_f == 0.0:
        kind = "توقف كامل داخل النطاق"
        if should_alert(state, str(mmsi), kind):
            send_telegram(build_alert(kind, mmsi, lat, lon, sog_f, cog, heading, region))
            mark_alert(state, str(mmsi), kind)
        return

    # Rule 2: سرعة منخفضة جداً
    if sog_f < 1.0:
        kind = "سرعة منخفضة جداً (Loitering/Drifting)"
        if should_alert(state, str(mmsi), kind):
            send_telegram(build_alert(kind, mmsi, lat, lon, sog_f, cog, heading, region))
            mark_alert(state, str(mmsi), kind)
        return


# =========================
# Summary report builder
# =========================
def build_summary(run_stats, vessels):
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    total_msgs = run_stats["messages"]
    total_pos = run_stats["pos_reports"]
    unique = len(vessels)

    red_count = sum(1 for v in vessels.values() if v.get("region") == "البحر الأحمر")
    gulf_count = sum(1 for v in vessels.values() if v.get("region") == "الخليج العربي")

    # رتب السفن حسب آخر تحديث ثم السرعة
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
        lines.append("• لا توجد سفن مستلمة خلال نافذة التشغيل (قد يكون طبيعيًا حسب التغطية/الوقت).")
        return "\n".join(lines)

    for i, v in enumerate(top, 1):
        lines.append(
            f"{i}️⃣ MMSI {v['mmsi']} | {v.get('region','')} | "
            f"{fmt(v.get('lat'))},{fmt(v.get('lon'))} | SOG {v.get('sog','?')} kn"
        )

    return "\n".join(lines)


# =========================
# WebSocket runner (graceful stop)
# =========================
def run():
    state = load_state()

    # تجميع بيانات للتقرير
    run_stats = {"messages": 0, "pos_reports": 0}
    vessels = {}  # mmsi -> last snapshot

    def on_open(ws):
        sub = {"APIKey": API_KEY, "BoundingBoxes": BOUNDING_BOXES}
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

        mmsi, lat, lon, sog, cog, heading = extract_ship_fields(ship)
        if mmsi and lat is not None and lon is not None and sog is not None:
            try:
                sog_f = float(sog)
            except Exception:
                sog_f = None

            region = guess_region(lat, lon)
            vessels[str(mmsi)] = {
                "mmsi": str(mmsi),
                "lat": lat,
                "lon": lon,
                "sog": sog_f if sog_f is not None else sog,
                "region": region,
                "last_ts": time.time()
            }

        # تنبيهات B
        evaluate_and_alert(state, ship)

    def on_error(ws, error):
        # تجاهل الأخطاء العابرة
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

    # إيقاف نظيف بعد RUN_SECONDS
    def stop_ws():
        time.sleep(RUN_SECONDS)
        try:
            ws.close()
        except Exception:
            pass

    threading.Thread(target=stop_ws, daemon=True).start()
    ws.run_forever(ping_interval=20, ping_timeout=10)

    # إرسال التقرير بعد الإغلاق
    if SEND_SUMMARY_REPORT:
        report = build_summary(run_stats, vessels)
        send_telegram(report)


if __name__ == "__main__":
    run()
