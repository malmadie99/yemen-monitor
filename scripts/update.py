#!/usr/bin/env python3
"""Auto-update the Yemen/KSA monitor data (data/news.json, data/meta.json).

Runs on GitHub Actions every 30 minutes. Standard library only.
1. Pulls RSS feeds (Ajel first, then Google News searches on trusted outlets).
2. Keeps only fresh, relevant, non-UAE items not seen before.
3. Asks a free LLM (Gemini free tier, or GitHub Models via the built-in
   GITHUB_TOKEN) to classify them and write short Arabic titles/summaries.
4. Applies hard dedupe rules in code, prunes old items, writes JSON.

Env:
  GEMINI_API_KEY   optional; if set, Gemini is used first
  GEMINI_MODEL     default gemini-2.5-flash
  GITHUB_TOKEN     provided by Actions; used for GitHub Models
  GH_MODELS_MODEL  default openai/gpt-4.1-mini
  LOOKBACK_HOURS   default 6
  DRY_RUN=1        print candidates, do not call the LLM or write files
"""
import datetime as dt
import difflib
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS = os.path.join(ROOT, "data", "news.json")
META = os.path.join(ROOT, "data", "meta.json")
SEEN = os.path.join(ROOT, "data", "seen.json")
RIYADH = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime.now(RIYADH)
LOOKBACK = float(os.environ.get("LOOKBACK_HOURS", "6"))
DRY = os.environ.get("DRY_RUN") == "1"

# Hard rule: no UAE-based outlets, ever.
UAE = ["wam.ae", "khaleejtimes.com", "gulfnews.com", "thenationalnews.com",
       "alkhaleej.ae", "albayan.ae", "aletihad.ae", "emaratalyoum.com",
       "emirates247.com", "skynewsarabia.com", "eremnews.com", "alroeya.com",
       "arabianbusiness.com", "24.ae", "al-ain.com", "alain.com"]
UAE_NAMES = ["سكاي نيوز", "إرم", "ارم نيوز", "العين الإخبارية", "الخليج",
             "البيان", "الاتحاد", "الإمارات اليوم", "وام", "الرؤية",
             "Sky News", "Khaleej", "Gulf News", "The National", "Al Ain"]
# Trusted outlets (Google News results must come from one of these).
TRUSTED = ["sabq.org", "okaz.com.sa", "alyaum.com", "aawsat.com",
           "alarabiya.net", "alhadath.net", "asharq.com", "spa.gov.sa",
           "alekhbariya.net", "aleqt.com", "arabnews.com", "saudigazette.com.sa",
           "sabanew.net", "qna.org.qa", "reuters.com", "apnews.com", "france24.com",
           "aa.com.tr", "afp.com", "alriyadh.com", "al-madina.com"]

GN = "https://news.google.com/rss/search?hl=ar&gl=SA&ceid=SA:ar&q="
FEEDS = [GN + urllib.parse.quote(q + " when:1d") for q in [
    "التحالف اعتراض وتدمير",
    "الحوثي صاروخ مسيرة السعودية",
    "المنطقة الشرقية الدمام الظهران الجبيل الخبر اعتراض",
    "الدفاع المدني إنذار زوال الخطر",
    "مطار اعتداء الحوثي الطيران المدني",
    "القوات المسلحة اليمنية فجر اليمن تحييد",
    "المتحدث الرسمي للقوات المسلحة اليمنية قتل عنصر تدمير آلية",
    "تعز مأرب لحج الحديدة الحوثي",
]]

KEYWORDS = re.compile(
    "التحالف|اعتراض|اعترض|الحوثي|حوثي|الدفاع المدني|إنذار|زوال الخطر|مطار|صاروخ|"
    "مسيّر|مسير|باليستي|كروز|اليمن|تعز|مأرب|لحج|الحديدة|عدن|صنعاء|المكلا|"
    "باب المندب|فجر اليمن|الشرقية|الدمام|الظهران|الجبيل|الخبر|بقيق|رأس تنورة|المالكي")


def log(*a):
    print(*a, file=sys.stderr)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def http(url, data=None, headers=None, timeout=40):
    h = {"User-Agent": "Mozilla/5.0 (yemen-monitor bot)"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def host(u):
    try:
        return urllib.parse.urlparse(u).hostname or ""
    except Exception:
        return ""


def is_uae(u, name=""):
    h = host(u)
    if any(h == d or h.endswith("." + d) for d in UAE):
        return True
    return any(n in (name or "") for n in UAE_NAMES)


BLOCKED = ["ajel.sa"]  # user asked to drop this outlet entirely


def is_blocked(u, name=""):
    h = host(u)
    if any(h == d or h.endswith("." + d) for d in BLOCKED):
        return True
    return bool(re.search(r"ajel|عاجل", name or "", re.I))


WEATHER = re.compile("طقس|أمطار|امطار|الأرصاد|الارصاد|رعدية|رعدي|سيول|ضباب|غبار|عواصف|عاصفة|رياح|هطول|الحرارة")
ATTACK = re.compile("صاروخ|صواريخ|مسيّر|مسير|اعتراض|الحوثي|حوثي|باليستي|كروز|اعتداء|هجوم|استهداف")


def is_weather(title):
    return bool(WEATHER.search(title)) and not ATTACK.search(title)


def is_trusted(u):
    h = host(u)
    return any(h == d or h.endswith("." + d) for d in TRUSTED)


def strip(s):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def norm(s):
    s = re.sub(r"[ً-ْـ]", "", s or "")  # tashkeel/tatweel
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"[^\w]+", " ", s).strip()


def fetch_candidates(seen):
    out, keys = [], set()
    for url in FEEDS:
        try:
            root = ET.fromstring(http(url))
        except Exception as e:
            log("feed failed:", url[:80], e)
            continue
        for it in root.iter("item"):
            title = strip(it.findtext("title"))
            link = (it.findtext("link") or "").strip()
            desc = strip(it.findtext("description"))[:500]
            src_el = it.find("source")
            src_url = src_el.get("url", "") if src_el is not None else ""
            src_name = (src_el.text or "") if src_el is not None else ""
            try:
                ts = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone(RIYADH)
            except Exception:
                continue
            if (NOW - ts).total_seconds() > LOOKBACK * 3600 or ts > NOW + dt.timedelta(minutes=10):
                continue
            if "news.google.com" in url:
                # Google News: title is "headline - Outlet"
                if src_name and title.endswith(" - " + src_name):
                    title = title[: -len(src_name) - 3]
                if is_uae(src_url, src_name) or not is_trusted(src_url):
                    continue
            if is_uae(link) or is_blocked(link, src_name) or is_blocked(src_url) or is_weather(title) \
                    or not KEYWORDS.search(title + " " + desc):
                continue
            k = norm(title)
            if k in seen or link in seen or k in keys:
                continue
            keys.add(k)
            out.append({"title": title, "desc": desc, "link": link, "ts": ts.isoformat(timespec="minutes"),
                        "outlet": src_name or host(link), "outlet_url": src_url or link})
    out.sort(key=lambda x: x["ts"])
    return out[-40:]


RULES = """أنت محرر أخبار دقيق لصفحة عربية ترصد هجمات الحوثيين على السعودية والحرب في اليمن. القارئ يسكن الخبر في المنطقة الشرقية.
ستصلك أخبار مرشحة (candidates) وقائمة بالأخبار الموجودة حاليًا (existing). أعد JSON فقط بهذا الشكل:
{"items":[{"i":رقم المرشح,"region":"east|ksa|yemen","type":"official|report|claim","kind":"intercept|other","missiles":0,"drones":0,"source":"...","title":"...","summary":"..."}],
 "yemen_daily":[{"i":رقم المرشح,"date":"YYYY-MM-DD","neutralized":عدد أو null,"vehicles":عدد أو null}],
 "note":"ملاحظة عربية قصيرة عن هذا التحديث"}
القواعد:
- أضف فقط ما يخص: هجمات/اعتراضات على السعودية، إنذارات وزوال خطر الدفاع المدني، إصابات رسمية، تطورات عسكرية أو سياسية مهمة داخل اليمن. تجاهل الرأي والتحليل والإدانات المكررة والرياضة وغيرها.
- لا تضف خبرًا موجودًا في existing أو مكررًا بين المرشحين (نفس الإعلان من أكثر من موقع = خبر واحد، اختر أوضح نسخة).
- kind="intercept" فقط لإعلان رسمي من التحالف/وزارة الدفاع باعتراض صواريخ أو مسيّرات مع أعداد صريحة. missiles = الصواريخ (باليستي + كروز)، drones = المسيّرات. "صاروخ" بدون رقم = 1. لا تخمّن أعدادًا. التحليل والإدانة ليست اعتراضًا.
- region: "east" إذا ذُكرت المنطقة الشرقية أو مدنها (الدمام، الظهران، الخبر، الجبيل، الأحساء، بقيق، رأس تنورة، القطيف، حفر الباطن، شيبة)؛ "ksa" لباقي المملكة؛ "yemen" لما يحدث داخل اليمن.
- type: "official" لبيان جهة رسمية سعودية أو التحالف أو الدفاع المدني أو الطيران المدني؛ "report" لتقارير الوسائل وبيانات الجيش اليمني (رواية طرف واحد: اكتب في الملخص "حسب الجيش اليمني")؛ "claim" لادعاءات الحوثي واكتب في الملخص "غير مؤكد".
- source: الجهة المعلنة + الوسيلة، مثل "المتحدث باسم التحالف عبر صحيفة عاجل".
- title: عنوان عربي قصير بصياغتك يذكر المكان واليوم (مثل "المنطقة الشرقية: اعتراض وتدمير مسيّرة، فجر الأحد"). استخدم وقت الخبر بتوقيت الرياض لتحديد اليوم والفترة.
- summary: جملة أو جملتان هادئتان بصياغتك، بدون تهويل، ولا تنقل أكثر من 15 كلمة حرفيًا.
- yemen_daily: فقط إذا أعلن متحدث القوات المسلحة اليمنية حصيلة 24 ساعة بأرقام صريحة (عدد المحيَّدين، وعدد الآليات/المعدات المدمرة أو المعطوبة). "عشرات" = null. date = تاريخ يوم الإعلان.
- إذا لا يوجد جديد مهم أعد items فارغة."""


def call_llm(prompt):
    gkey = os.environ.get("GEMINI_API_KEY")
    errs = []
    if gkey:
        models = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash"]
        body = {"systemInstruction": {"parts": [{"text": RULES}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                r = json.loads(http(url, json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": gkey}, 120))
                return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
            except Exception as e:
                errs.append(f"gemini {model}: {e}")
    # GitHub Models was retired (2026-07-30), so there is no free built-in LLM anymore.
    # Fall back to a conservative rule-based classifier so the page keeps updating.
    log("no LLM available (" + ("; ".join(errs) or "no GEMINI_API_KEY") + "); using rule-based fallback")
    return rule_based(prompt)


IMPORTANT = re.compile("اعتراض|عترض|تدمير|دمر|إنذار|انذار|زوال الخطر|الدفاع المدني|اعتداء|هجوم|استهداف|إصابة|اصابه|وفاة|مقتل|سقوط|مطار")
EAST = re.compile("الشرقية|الدمام|الظهران|الخبر|الجبيل|الأحساء|الاحساء|بقيق|رأس تنورة|راس تنوره|القطيف|حفر الباطن|شيبة|شيبه")
YEMEN_IN = re.compile("تعز|مأرب|مارب|لحج|الحديدة|عدن|صنعاء|المكلا|فجر اليمن|القوات المسلحة اليمنية|باب المندب")
KSA_WORDS = re.compile("السعودية|المملكة|الرياض|جدة|جازان|نجران|عسير|خميس مشيط|ابها|أبها|التحالف")
OFFICIAL = re.compile("التحالف|وزارة الدفاع|الدفاع المدني|الطيران المدني|وزارة الطاقة|المتحدث الرسمي|المتحدث باسم")
CLAIM = re.compile("الحوثي|الحوثيون|جماعة انصار الله|جماعة أنصار الله")
CLAIM_VERB = re.compile("تعلن|يعلن|اعلن|أعلن|تبنت|تبني|تبنى|زعم|تزعم|ادعى|تدعي|يدعي")
DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _count(text, nouns, dual, single):
    m = re.search(r"(\d+)\s*(?:" + nouns + ")", text)
    if m:
        return int(m.group(1))
    if re.search(dual, text):
        return 2
    if re.search(single, text):
        return 1
    return 0


SPOKES = re.compile("قوات المسلحة اليمنية|المتحدث الرسمي للقوات المسلحة")
KILLED = re.compile(r"قتل\s*(?:أكثر من|اكثر من|نحو|حوالي)?\s*\(?(\d{2,5})\)?\s*(?:عنصر|حوثي)")
VEHICLES = re.compile(r"(?:تدمير|إعطاب|اعطاب)[^\d\n]{0,25}\(?(\d{2,4})\)?\s*(?:آلية|الية|اليه)")


def daily_from(raw, ts):
    """Official 24h tally from the Yemeni Armed Forces spokesman, as re-published by media / X relays."""
    if not SPOKES.search(raw):
        return None
    k, v = KILLED.search(raw), VEHICLES.search(raw)
    if not (k or v):
        return None
    return {"date": ts[:10], "neutralized": int(k.group(1)) if k else None,
            "vehicles": int(v.group(1)) if v else None}


def rule_based(prompt):
    data = json.loads(prompt)
    items, daily = [], []
    for c in data.get("candidates", []):
        title, desc = c.get("title", ""), c.get("desc", "")
        raw = (title + " " + desc).translate(DIG)
        d = daily_from(raw, c.get("ts", ""))
        if d:
            daily.append(dict(i=c["i"], **d))
        if not IMPORTANT.search(raw):
            continue
        t = norm(raw)
        if EAST.search(raw) or EAST.search(t):
            region = "east"
        elif YEMEN_IN.search(raw) and not KSA_WORDS.search(title):
            region = "yemen"
        else:
            region = "ksa"
        official = bool(OFFICIAL.search(raw))
        if CLAIM.search(raw) and CLAIM_VERB.search(raw) and not official:
            typ = "claim"
        elif official and region != "yemen":
            typ = "official"
        else:
            typ = "report"
        # Without an LLM we cannot tell a new announcement from a re-report or a daily total,
        # so interceptions are shown as news but never counted (counts would be inflated).
        kind, missiles, drones = "other", 0, 0
        summary = "نقلاً عن " + (c.get("outlet") or "مصدر إخباري") + "."
        if typ == "claim":
            summary += " غير مؤكد."
        if typ == "report" and region == "yemen":
            summary += " رواية طرف واحد."
        items.append({"i": c["i"], "region": region, "type": typ, "kind": kind, "missiles": missiles,
                      "drones": drones, "source": c.get("outlet") or "", "title": title, "summary": summary})
    return {"items": items, "yemen_daily": daily,
            "note": ""}


def toint(v):
    try:
        return max(0, int(v))
    except Exception:
        return 0


def is_dup(new, news):
    t = dt.datetime.fromisoformat(new["ts"])
    nt = norm(new["title"])
    for x in news:
        try:
            xt = dt.datetime.fromisoformat(x["ts"])
        except Exception:
            continue
        gap = abs((xt - t).total_seconds())
        if new["url"] and x.get("url") == new["url"]:
            return True
        if new["kind"] == "intercept" and x.get("kind") == "intercept" and gap <= 3 * 3600 \
                and toint(x.get("missiles")) == new["missiles"] and toint(x.get("drones")) == new["drones"] \
                and x.get("region") == new["region"]:
            return True
        if gap <= 24 * 3600 and difflib.SequenceMatcher(None, nt, norm(x.get("title", ""))).ratio() > 0.75:
            return True
    return False


def slug(c, item):
    base = re.sub(r"[^a-z0-9]+", "-", urllib.parse.urlparse(c["link"]).path.lower()).strip("-")[-24:] or "item"
    pre = "int-" if item["kind"] == "intercept" else (item["region"] + "-")
    return f'{c["ts"][:10]}-{pre}{base}'


# One-off corrections (verified by hand against the original announcements, 25-26 Oct 2026).
# These bot-added items re-reported or aggregated announcements that were already counted.
NOT_COUNTED = {
    "2026-10-25-int-5vvbdmzllaha5wkposgjwxzc",  # 05:45 re-report of the two earlier Riyadh drones
    "2026-10-25-int-zeevlwdfn3shjdnjhpq1h5uq",  # 09:01 daily total (1 missile + 2 drones) already counted
    "2026-10-25-int-uhhquug2yuviqzdzctzwaldr",  # 12:42 daily total (12 missiles + 2 drones) already counted
    "2026-10-25-int-local-zef52z4nd",           # 16:28 damage/injury story, not an announcement
}
FIX_COUNTS = {  # coalition statement 26 Oct: 10 ballistic + 1 cruise missiles, 1 drone
    "2026-10-26-int-lwjfpbmnecxhkzdh4dtlvdeq": (11, 1),
}


def clean_source(s):
    s = re.sub(r"@?AJELNEWS24|ajel\.sa|Ajel", "", s, flags=re.I)
    s = re.sub(r"عاجل\s*[و/،,]\s*", "", s)
    s = re.sub(r"\s*[و/،,]\s*عاجل", "", s)
    s = re.sub(r"\s*(?:عبر|—|-|/)?\s*(?:صحيفة\s+)?عاجل", "", s)
    s = re.sub(r"\s*(?:عبر|و|—|-|/)\s*$", "", s).strip()
    return s or "مصدر رسمي"


def cleanup(news):
    out = []
    for x in news:
        bot = (x.get("summary") or "").startswith("نقلاً عن")
        blocked = is_blocked(x.get("url", ""), x.get("source", "") if bot else "")
        if bot and (blocked or is_weather(x.get("title", ""))):
            continue  # drop bot items from the blocked outlet and weather items entirely
        if x.get("id") in NOT_COUNTED and x.get("kind") == "intercept":
            x["kind"] = "other"
            x.pop("missiles", None); x.pop("drones", None)
        if x.get("id") in FIX_COUNTS:
            x["missiles"], x["drones"] = FIX_COUNTS[x["id"]]
        if re.search(r"ajel|عاجل", x.get("source", ""), re.I):
            x["source"] = clean_source(x["source"])
        if is_blocked(x.get("url", "")):
            x["url"] = ""
        out.append(x)
    return out


def main():
    news = cleanup(load(NEWS, []))
    meta = load(META, {"status": {}, "casualties": {}})
    seen_list = load(SEEN, [])
    seen = set(seen_list)
    cands = fetch_candidates(seen)
    log(f"{len(cands)} new candidates")
    if DRY:
        for i, c in enumerate(cands):
            print(i, c["ts"], c["outlet"], "|", c["title"])
        return

    added, note = [], None
    if cands:
        recent = [{"ts": x["ts"], "title": x.get("title"), "kind": x.get("kind"), "region": x.get("region"),
                   "missiles": x.get("missiles"), "drones": x.get("drones")}
                  for x in news if x.get("ts", "") >= (NOW - dt.timedelta(hours=48)).isoformat()]
        prompt = json.dumps({"now_riyadh": NOW.isoformat(timespec="minutes"), "existing": recent,
                             "candidates": [dict(i=i, **{k: c[k] for k in ("ts", "outlet", "title", "desc")})
                                            for i, c in enumerate(cands)]}, ensure_ascii=False)
        res = call_llm(prompt)
        note = (res.get("note") or "").strip()[:300] or None
        for it in res.get("items") or []:
            try:
                c = cands[int(it["i"])]
            except Exception:
                continue
            item = {"ts": c["ts"],
                    "region": it.get("region") if it.get("region") in ("east", "ksa", "yemen") else "ksa",
                    "type": it.get("type") if it.get("type") in ("official", "report", "claim") else "report",
                    "kind": "intercept" if it.get("kind") == "intercept" else "other",
                    "source": str(it.get("source") or c["outlet"])[:120],
                    "title": str(it.get("title") or c["title"])[:160],
                    "summary": str(it.get("summary") or "")[:400],
                    "url": c["link"] if c["link"].startswith("https://") else ""}
            item["ts"] = dt.datetime.fromisoformat(c["ts"]).isoformat(timespec="seconds")
            if item["kind"] == "intercept":
                item["missiles"], item["drones"] = toint(it.get("missiles")), toint(it.get("drones"))
                if item["missiles"] + item["drones"] == 0 or item["type"] != "official":
                    item["kind"] = "other"
                    item.pop("missiles"); item.pop("drones")
            if item["type"] == "claim" and "غير مؤكد" not in item["summary"]:
                item["summary"] = (item["summary"] + " (غير مؤكد)").strip()
            if is_uae(item["url"]) or is_dup(item, news + added):
                continue
            item["id"] = slug(c, item)
            added.append(item)

        cas = meta.setdefault("casualties", {})
        for y in res.get("yemen_daily") or []:
            d = str(y.get("date") or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                continue
            try:
                c = cands[int(y["i"])]
            except Exception:
                continue
            if isinstance(y.get("neutralized"), int) and y["neutralized"] > 0:
                cas.setdefault("houthiDays", {})[d] = y["neutralized"]
                cas["houthiAsOf"], cas["houthiUrl"] = c["ts"], c["link"]
            if isinstance(y.get("vehicles"), int) and y["vehicles"] > 0:
                cas.setdefault("vehicleDays", {})[d] = y["vehicles"]
                cas["vehicleAsOf"], cas["vehicleUrl"] = c["ts"], c["link"]

        for c in cands:  # mark everything we showed the model as processed
            seen_list.append(norm(c["title"]))
            seen_list.append(c["link"])

    # prune: keep intercepts forever, other items 21 days
    cutoff = (NOW - dt.timedelta(days=21)).isoformat()
    news = [x for x in news + added if x.get("kind") == "intercept" or x.get("ts", "") >= cutoff]
    news.sort(key=lambda x: x.get("ts", ""), reverse=True)

    ids = set()
    for x in news:  # unique ids
        base, n = x.get("id") or x["ts"][:10], 2
        while x.get("id") in ids or not x.get("id"):
            x["id"] = f"{base}-{n}"; n += 1
        ids.add(x["id"])

    if added:
        east = [a for a in added if a["region"] == "east"]
        default = f"أُضيف {len(added)} خبر" + (f"، منها {len(east)} في الشرقية" if east else "") + "."
    else:
        default = "ما فيه تطورات مهمة جديدة."
    meta["status"] = {"lastRun": NOW.isoformat(timespec="seconds"), "note": note if added and note else default,
                      "added": len(added)}
    save(NEWS, news)
    save(META, meta)
    save(SEEN, seen_list[-4000:])
    print(f"added={len(added)} east={sum(a['region'] == 'east' for a in added)}")
    for a in added:
        print("+", a["ts"], a["region"], a["kind"], a["title"])


if __name__ == "__main__":
    main()
