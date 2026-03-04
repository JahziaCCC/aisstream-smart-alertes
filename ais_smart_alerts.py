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

REPORT_SPEED_MAX = float(os.getenv("REPORT_SPEED_MAX", "35"))
ALERT_SLOW_KN = float(os.getenv("ALERT_SLOW_KN", "1"))
CLUSTER_DECIMALS = int(os.getenv("CLUSTER_DECIMALS", "3"))

STATE_FILE = "state.json"
KSA_TZ = dt.timezone(dt.timedelta(hours=3))

# =========================
# BOUNDING BOXES
# =========================
RED_SEA_BBOX = [[12, 32], [30, 44]]
GULF_BBOX = [[22, 47], [31, 57]]
BOUNDING_BOXES = [RED_SEA_BBOX, GULF_BBOX]

FILTER_MESSAGE_TYPES = ["PositionReport", "StandardClassBPositionReport"]

# =========================
# HELPERS
# =========================
def now_ksa():
    return dt.datetime.now(tz=KSA_TZ)

def fmt(x, nd=4):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def send_telegram(text: str):
    MAX_LEN = 3500
    chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    for part in chunks:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id": CHAT, "text": part},
            timeout=25
        )

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

# =========================
# REPORT
# =========================
def speed_bucket(sog_f):
    if sog_f is None:
        return "unknown"
    if sog_f == 0:
        return "stopped"
    if 0 < sog_f < 1:
        return "very_slow"
    if 1 <= sog_f < 15:
        return "medium"
    if 15 <= sog_f <= REPORT_SPEED_MAX:
        return "fast_ok"
    if sog_f > REPORT_SPEED_MAX:
        return "anomaly"
    return "unknown"

def bucket_label(key):
    return {
        "stopped": "🟥 متوقفة (0 kn)",
        "very_slow": "🟧 بطيئة (0–1 kn)",
        "medium": "🟨 متوسطة (1–15 kn)",
        "fast_ok": f"🟩 سريعة (15–{REPORT_SPEED_MAX:.0f} kn)",
        "anomaly": f"⚫ شاذة (>{REPORT_SPEED_MAX:.0f} kn)",
        "unknown": "⚪ غير مكتمل"
    }.get(key, key)

def cluster_key(lat, lon):
    try:
        return (round(float(lat), CLUSTER_DECIMALS), round(float(lon), CLUSTER_DECIMALS))
    except Exception:
        return None

def build_summary(run_stats, vessels):

    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    total_msgs = run_stats["messages"]
    total_pos = run_stats["pos_reports"]
    unique = len(vessels)

    red_count = sum(1 for v in vessels.values() if v.get("region") == "البحر الأحمر")
    gulf_count = sum(1 for v in vessels.values() if v.get("region") == "الخليج العربي")

    buckets = {"stopped":0,"very_slow":0,"medium":0,"fast_ok":0,"anomaly":0,"unknown":0}

    for v in vessels.values():
        buckets[speed_bucket(v.get("sog_f"))] += 1

    clusters = {}

    for v in vessels.values():

        ck = cluster_key(v.get("lat"), v.get("lon"))

        if not ck:
            continue

        c = clusters.setdefault(ck, {"count":0,"sum_sog":0.0,"n_sog":0,"region":v.get("region","")})

        c["count"] += 1

        if v.get("sog_f") is not None:
            c["sum_sog"] += float(v["sog_f"])
            c["n_sog"] += 1

    top_clusters = sorted(clusters.items(), key=lambda kv: kv[1]["count"], reverse=True)[:3]

    rows = list(vessels.values())
    rows.sort(key=lambda x: x.get("last_ts",0), reverse=True)

    clean_rows = [r for r in rows if (r.get("sog_f") is not None and r["sog_f"] <= REPORT_SPEED_MAX)]
    clean_top = clean_rows[:REPORT_TOP_N]

    lines = [
        "📡 تقرير حركة السفن (AIS) — ملخص تشغيلي",
        f"🕒 {t}",
        "════════════════════",
        f"📨 إجمالي الرسائل: {total_msgs}",
        f"📍 تقارير المواقع: {total_pos}",
        f"🚢 سفن فريدة (MMSI): {unique}",
        f"🌊 البحر الأحمر: {red_count} | الخليج العربي: {gulf_count}",
        "════════════════════",
        "📊 تحليل الحركة البحرية (توزيع السرعات)",
        f"{bucket_label('stopped')}: {buckets['stopped']}",
        f"{bucket_label('very_slow')}: {buckets['very_slow']}",
        f"{bucket_label('medium')}: {buckets['medium']}",
        f"{bucket_label('fast_ok')}: {buckets['fast_ok']}",
        f"{bucket_label('anomaly')}: {buckets['anomaly']}",
        "════════════════════",
        f"📍 نقاط الكثافة (تقريب {CLUSTER_DECIMALS} أرقام)"
    ]

    if not top_clusters:
        lines.append("• لا توجد نقاط كثافة واضحة خلال نافذة التشغيل.")
    else:
        for i,(ck,meta) in enumerate(top_clusters,1):

            latR,lonR = ck
            avg = (meta["sum_sog"]/meta["n_sog"]) if meta["n_sog"] else None
            avg_txt = f"{fmt(avg,1)} kn" if avg else "غير متاح"
            region = meta.get("region","")

            lines.append(
                f"{i}️⃣ {fmt(latR,CLUSTER_DECIMALS)},{fmt(lonR,CLUSTER_DECIMALS)} | {region} — {meta['count']} سفن | متوسط السرعة: {avg_txt}"
            )

    lines.append("════════════════════")
    lines.append(f"🔎 أعلى {len(clean_top)} سفن (فلتر عرض ≤ {REPORT_SPEED_MAX:.0f} kn)")

    for i,v in enumerate(clean_top,1):

        lines.append(
            f"{i}️⃣ MMSI {v['mmsi']} | {v.get('region','')}\n"
            f"   📍 {fmt(v.get('lat'))},{fmt(v.get('lon'))} | SOG {fmt(v.get('sog_f'),1)} kn"
        )

    return "\n".join(lines)

# =========================
# RUN
# =========================
def run():

    state = load_state()

    run_stats = {"messages":0,"pos_reports":0}

    vessels = {}

    def on_open(ws):

        sub = {
            "APIKey": API_KEY,
            "BoundingBoxes": BOUNDING_BOXES,
            "FilterMessageTypes": FILTER_MESSAGE_TYPES
        }

        ws.send(json.dumps(sub))

    def on_message(ws,message):

        run_stats["messages"] += 1

        try:
            data = json.loads(message)
        except Exception:
            return

        msg = data.get("Message")

        if not isinstance(msg,dict):
            return

        ship = None

        if isinstance(msg.get("PositionReport"),dict):
            ship = msg["PositionReport"]

        elif isinstance(msg.get("StandardClassBPositionReport"),dict):
            ship = msg["StandardClassBPositionReport"]

        if not isinstance(ship,dict):
            return

        run_stats["pos_reports"] += 1

        mmsi = ship.get("UserID") or ship.get("Mmsi")
        lat = ship.get("Latitude")
        lon = ship.get("Longitude")
        sog = ship.get("Sog")

        if mmsi and lat is not None and lon is not None:

            sog_f = safe_float(sog)
            region = guess_region(lat,lon)

            vessels[str(mmsi)] = {
                "mmsi":str(mmsi),
                "lat":lat,
                "lon":lon,
                "sog_f":sog_f,
                "region":region,
                "last_ts":time.time()
            }

        # تم تعطيل التنبيهات الفردية هنا
        # evaluate_and_alert(state, ship)

    def on_close(ws,code,reason):
        save_state(state)

    ws = websocket.WebSocketApp(
        "wss://stream.aisstream.io/v0/stream",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close
    )

    def stop_ws():
        time.sleep(RUN_SECONDS)
        ws.close()

    threading.Thread(target=stop_ws,daemon=True).start()

    ws.run_forever()

    if SEND_SUMMARY_REPORT:
        send_telegram(build_summary(run_stats,vessels))

if __name__ == "__main__":
    run()
