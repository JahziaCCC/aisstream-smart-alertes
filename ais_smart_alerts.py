import os
import re
import json
import time
import hashlib
import datetime as dt
from urllib.parse import quote_plus

import requests
import feedparser
from bs4 import BeautifulSoup

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

KSA_TZ = dt.timezone(dt.timedelta(hours=3))
STATE_FILE = "news_state.json"

# ===== إعدادات التقرير =====
MAX_ITEMS_PER_BUCKET = int(os.getenv("NEWS_TOP_N", "7"))     # كم خبر نعرض
LOOKBACK_HOURS = int(os.getenv("NEWS_HOURS", "6"))           # كل كم ساعة نبحث
DEDUP_DAYS = int(os.getenv("NEWS_DEDUP_DAYS", "7"))          # منع تكرار الخبر كم يوم

# ===== نطاقات الاهتمام =====
# (أسلوب كلمات مفتاحية - تقدر توسعها لاحقاً)
QUERIES = [
    # Red Sea
    ("البحر الأحمر", [
        '("Red Sea" OR "Bab al-Mandab" OR "Gulf of Aden") (ship OR vessel OR tanker OR bulker OR container) (attack OR incident OR explosion OR fire OR collision OR grounding OR hijack OR piracy OR drone OR missile)',
        'UKMTO "Red Sea" incident',
    ]),
    # Gulf / Hormuz
    ("الخليج العربي", [
        '("Strait of Hormuz" OR "Persian Gulf" OR "Arabian Gulf" OR "Gulf of Oman") (ship OR vessel OR tanker) (incident OR explosion OR attack OR collision OR fire OR grounding OR piracy)',
        'UKMTO "Hormuz" incident',
    ]),
]

# ===== روابط RSS (Google News Search) =====
# صيغة Google News RSS search مع معاملات hl/gl/ceid موثّقة عملياً من Feedly/مراجع RSS  [oai_citation:2‡docs.feedly.com](https://docs.feedly.com/article/375-what-are-some-of-the-advanced-keyword-alerts-google-news-search-parameters?utm_source=chatgpt.com)
def google_news_rss_url(query: str, hours: int) -> str:
    # when:6h يساعد يجيب آخر 6 ساعات تقريباً
    q = f"{query} when:{hours}h"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"

# ===== UKMTO Recent Incidents =====
# صفحة رسمية للحوادث الحديثة  [oai_citation:3‡UKMTO](https://www.ukmto.org/recent-incidents?utm_source=chatgpt.com)
UKMTO_RECENT = "https://www.ukmto.org/recent-incidents"


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
        return {"seen": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def prune_state(state):
    # حذف القديم
    cutoff = now_ksa() - dt.timedelta(days=DEDUP_DAYS)
    seen = state.get("seen", {})
    new_seen = {}
    for k, iso in seen.items():
        try:
            ts = dt.datetime.fromisoformat(iso)
        except Exception:
            continue
        if ts >= cutoff:
            new_seen[k] = iso
    state["seen"] = new_seen

def key_for_item(title: str, link: str) -> str:
    raw = f"{title.strip()}|{link.strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def is_seen(state, k: str) -> bool:
    return k in state.get("seen", {})

def mark_seen(state, k: str):
    state.setdefault("seen", {})[k] = now_ksa().isoformat()

def clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())

def fetch_google_news_items(bucket_name: str, queries: list[str]) -> list[dict]:
    items = []
    for q in queries:
        url = google_news_rss_url(q, LOOKBACK_HOURS)
        feed = feedparser.parse(url)
        for e in getattr(feed, "entries", []):
            title = clean_title(getattr(e, "title", ""))
            link = getattr(e, "link", "")
            published = clean_title(getattr(e, "published", "")) or clean_title(getattr(e, "updated", ""))
            if not title or not link:
                continue
            items.append({
                "bucket": bucket_name,
                "source": "Google News (Media)",
                "title": title,
                "link": link,
                "published": published
            })
    return items

def fetch_ukmto_recent() -> list[dict]:
    items = []
    try:
        html = requests.get(UKMTO_RECENT, timeout=25).text
        soup = BeautifulSoup(html, "lxml")

        # نحاول التقاط روابط الحوادث من الصفحة
        # (هيكل الصفحة قد يتغير، فنجمع بشكل مرن)
        for a in soup.select("a"):
            txt = clean_title(a.get_text(" "))
            href = a.get("href", "")
            if not href:
                continue
            if "incident" in href.lower() or "incidents" in href.lower():
                if not txt or len(txt) < 12:
                    continue
                link = href if href.startswith("http") else f"https://www.ukmto.org{href}"
                items.append({
                    "bucket": "UKMTO",
                    "source": "UKMTO (Official)",
                    "title": txt,
                    "link": link,
                    "published": ""
                })
    except Exception:
        pass
    # إزالة تكرارات بسيطة
    uniq = {}
    for it in items:
        k = key_for_item(it["title"], it["link"])
        uniq[k] = it
    return list(uniq.values())

def build_report(new_items: list[dict]) -> str:
    t = now_ksa().strftime("%Y-%m-%d %H:%M KSA")
    lines = [
        "📰 تقرير الحوادث البحرية (إعلامي + رسمي)",
        f"🕒 {t}",
        f"⏱️ نافذة الرصد: آخر {LOOKBACK_HOURS} ساعات",
        "════════════════════",
    ]

    if not new_items:
        lines.append("✅ لا توجد أخبار/حوادث جديدة مطابقة للنطاق خلال النافذة الحالية.")
        return "\n".join(lines)

    # تصنيف حسب Bucket
    by_bucket = {}
    for it in new_items:
        by_bucket.setdefault(it["bucket"], []).append(it)

    # ترتيب بسيط (كما وصلت)
    for bucket, items in by_bucket.items():
        lines.append(f"📌 {bucket}")
        for i, it in enumerate(items[:MAX_ITEMS_PER_BUCKET], 1):
            pub = f" | {it['published']}" if it.get("published") else ""
            # نطبع العنوان ثم الرابط في سطر مستقل (أوضح بالتيليجرام)
            lines.append(f"{i}️⃣ {it['title']} ({it['source']}{pub})")
            lines.append(f"🔗 {it['link']}")
        lines.append("════════════════════")

    return "\n".join(lines).strip()

def main():
    state = load_state()
    prune_state(state)

    # نجمع عناصر من UKMTO + Google News
    all_items = []
    all_items.extend(fetch_ukmto_recent())

    for bucket_name, queries in QUERIES:
        all_items.extend(fetch_google_news_items(bucket_name, queries))

    # Dedup + فلترة جديد فقط
    new_items = []
    for it in all_items:
        k = key_for_item(it["title"], it["link"])
        if is_seen(state, k):
            continue
        # نعتبره جديد
        new_items.append(it)
        mark_seen(state, k)

    # بناء التقرير وإرساله
    report = build_report(new_items)
    send_telegram(report)

    save_state(state)

if __name__ == "__main__":
    main()
