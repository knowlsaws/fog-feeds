#!/usr/bin/env python3
"""北海道の釣果情報を集めて RSS にする（Fog アプリ用）。北海道全域と函館周辺の 2 本。

- 取得元は collector/sources.json（RSS / HTML のリンク一覧 / 自動判定）
- 函館周辺の記事だけ残す（函館・道南・近くの港名や地名を含む。ニュース検索は釣りの語も必要）
- 新しい記事はページを 1 回読んで、説明（meta description か本文の冒頭）と日付を補う
- 場所と魚種を文中から拾い、説明の末尾に「場所: … / 魚種: …」と書く（アプリの地図ピンが使う）
- 状態は data/items.json に貯め、docs/hakodate.xml と docs/hakodate.json に書く
標準ライブラリだけで動く。
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Fog-feeds/1.0 (+https://github.com/sawswork/fog-feeds; personal fishing log aggregator)"
TZ = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(TZ)
KEEP_DAYS = 90
MAX_ITEMS = 1500
MAX_ENRICH = 40
MAX_ENRICH_PER_SOURCE = 12
MAX_NEW_PER_SOURCE = 30
FISHING_WORDS = ["釣", "アイナメ", "アブラコ", "ソイ", "ホッケ", "カレイ", "ヒラメ", "ブリ", "イカ", "サバ", "サケ", "ロックフィッシュ", "ジギング", "エギング", "投げ", "ルアー", "船", "漁港", "堤防", "岸壁", "沖堤"]
REGION_WORDS = ["北海道", "道南", "道央", "道東", "道北", "渡島", "檜山", "噴火湾", "津軽海峡", "オホーツク", "石狩湾", "宗谷"]
HAKODATE_WORDS = ["函館", "道南", "渡島", "檜山", "噴火湾", "津軽海峡"]
NAV_WORDS = ["トップ", "ホーム", "会社", "お問い合わせ", "お問合せ", "問い合わせ", "プライバシー", "サイトマップ", "ログイン", "カート", "採用", "アクセス", "店舗案内",
             "利用規約", "特定商取引", "home", "contact", "privacy", "login", "cart", "sitemap", "about", "/category/", "/tag/", "page/", "feed", "wp-login", "javascript:"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_json(path, default):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


PLACES = load_json("collector/places.json", [])
SPECIES = sorted(load_json("collector/species.json", []), key=len, reverse=True)
PLACE_BY_NAME = {p["name"]: p for p in PLACES}


# ---------- 取得

def fetch(url, timeout=25, retries=2):
    """1 回失敗したら少し待って取り直す（一時的な 429 / 5xx / 切断のため）"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,*/*;q=0.8"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404, 410):
                raise
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(3 * (attempt + 1))
    raise last


def decode(data, ctype=""):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    cands = [m.group(1)] if m else []
    head = data[:4096].decode("ascii", "ignore")
    m2 = re.search(r"charset=[\"']?([\w-]+)", head, re.I)
    if m2:
        cands.append(m2.group(1))
    cands += ["utf-8", "cp932", "euc_jp"]
    for c in cands:
        try:
            return data.decode(c)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "ignore")


# ---------- 文字列

def strip_tags(s, limit=None):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] + "…" if limit and len(s) > limit else s


def parse_date(text):
    if not text:
        return None
    t = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            d = dt.datetime.strptime(t.replace("Z", "+0000") if fmt.endswith("Z") else t, fmt)
            return d if d.tzinfo else d.replace(tzinfo=TZ)
        except ValueError:
            continue
    try:
        d = email.utils.parsedate_to_datetime(t)
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        pass
    m = re.search(r"(20\d\d)[-/年.](\d{1,2})[-/月.](\d{1,2})", t)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TZ)
        except ValueError:
            return None
    return None


def date_in_url(url):
    for p in (r"(20\d\d)[-/_](\d{1,2})[-/_](\d{1,2})", r"(20\d\d)(\d\d)(\d\d)"):
        m = re.search(p, url)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                try:
                    return dt.datetime(y, mo, d, tzinfo=TZ)
                except ValueError:
                    pass
    return None


# ---------- RSS / Atom

def parse_feed(text, source):
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None
    def local(tag):
        return tag.split("}")[-1]
    items = []
    feed_title = None
    for el in root.iter():
        if local(el.tag) == "title" and feed_title is None and el.text:
            feed_title = el.text.strip()
        if local(el.tag) in ("item", "entry"):
            title = link = guid = summary = date = None
            for c in el:
                n = local(c.tag)
                if n == "title":
                    title = (c.text or "").strip()
                elif n == "link":
                    link = c.get("href") if c.get("href") else (c.text or "").strip()
                elif n in ("guid", "id"):
                    guid = (c.text or "").strip()
                elif n in ("description", "summary", "content", "encoded") and not summary:
                    summary = strip_tags(c.text or "", 400)
                elif n in ("pubDate", "published", "updated", "date") and not date:
                    date = c.text
            if not title and not link:
                continue
            items.append({"title": title or link, "link": link or guid, "summary": summary or "", "date": parse_date(date), "source": source["name"]})
    return feed_title, items


# ---------- HTML のリンク一覧

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []
        self.title = None
        self._in_title = False
        self.feeds = []
        self.meta = {}
        self.times = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._href = a["href"]
            self._text = []
        elif tag == "title":
            self._in_title = True
        elif tag == "link" and "alternate" in (a.get("rel") or "") and ("rss" in (a.get("type") or "") or "atom" in (a.get("type") or "")):
            if a.get("href"):
                self.feeds.append(a["href"])
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text)))
            self._href = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
        if self._in_title and self.title is None:
            self.title = data.strip() or None


def parse_html_links(text, base_url, source):
    p = LinkParser()
    try:
        p.feed(text)
    except Exception:
        pass
    host = urllib.parse.urlparse(base_url).netloc.replace("www.", "")
    filt = source.get("filter") or ""
    seen = set()
    items = []
    for href, label in p.links:
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urllib.parse.urljoin(base_url, href).split("#")[0]
        u = urllib.parse.urlparse(url)
        if u.netloc.replace("www.", "") != host:
            continue
        if u.path.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "gif", "pdf", "zip", "mp4", "webp"):
            continue
        if filt and filt not in url:
            continue
        label = re.sub(r"\s+", " ", html.unescape(label)).strip()
        if len(label) < 6:
            continue
        # 月別アーカイブ（「2025年4月(14)」「/2025/04」）やページ送りは記事ではない
        if re.match(r"^\d{4}年\s*\d{1,2}月", label) or re.search(r"/20\d\d/\d{2}/?$", u.path) or re.match(r"^(次|前|Next|Prev|»|«|\d+)$", label, re.I):
            continue
        low = (label + " " + u.path).lower()
        if any(w.lower() in low for w in NAV_WORDS) if not filt else any(w.lower() in label.lower() for w in NAV_WORDS):
            continue
        if url in seen:
            continue
        seen.add(url)
        # 日付は URL から。無ければリンクの文字から（「2026-11-04 18:28 ツツイカ」のような一覧）
        date = date_in_url(url)
        if not date:
            m = re.search(r"20\d\d[-/年.]\d{1,2}[-/月.]\d{1,2}", label)
            if m:
                date = parse_date(m.group(0))
        items.append({"title": label[:120], "link": url, "summary": "", "date": date, "source": source["name"]})
    return p, items


# ---------- 記事ページを読んで補う

def enrich(item):
    try:
        data, ctype = fetch(item["link"], timeout=20)
    except Exception as e:  # noqa: BLE001
        log("  enrich failed", item["link"], e)
        return
    text = decode(data, ctype)
    p = LinkParser()
    try:
        p.feed(text)
    except Exception:
        pass
    desc = p.meta.get("og:description") or p.meta.get("description") or p.meta.get("twitter:description")
    if not desc or len(desc.strip()) < 40 or "続きを読む" in desc:
        body = re.sub(r"<(header|nav|footer|aside)\b.*?</\1>", " ", text, flags=re.S | re.I)
        m = re.search(r"<(article|main)\b.*?</\1>", body, flags=re.S | re.I)
        desc = strip_tags(m.group(0) if m else body, 300)
    if desc:
        item["summary"] = strip_tags(desc, 400)
    if not item.get("date"):
        for k in ("article:published_time", "og:published_time", "datepublished", "pubdate", "date"):
            if p.meta.get(k):
                item["date"] = parse_date(p.meta[k])
                break
        if not item.get("date") and p.times:
            item["date"] = parse_date(p.times[0])
    if not item.get("date"):
        m = re.search(r"(20\d\d)[年/.-](\d{1,2})[月/.-](\d{1,2})", strip_tags(text[:20000]))
        if m:
            item["date"] = parse_date(m.group(0))


# ---------- 絞り込みと抽出

def extract_places(text):
    found = []
    t = text
    for p in PLACES:
        if p["name"] in t:
            found.append(p)
            t = t.replace(p["name"], "　" * len(p["name"]))
    return found


def extract_species(text):
    found = []
    t = text
    for w in SPECIES:
        i = t.find(w)
        if i >= 0:
            found.append((i, w))
            t = t.replace(w, "　" * len(w))
    return [w for _, w in sorted(found)]


def is_regional(text):
    """北海道の記事か（道名・地域名・道内の港名や地名を含む）"""
    if any(w in text for w in REGION_WORDS):
        return True
    return bool(extract_places(text))


def is_hakodate(text, places):
    """函館周辺（道南）の記事か"""
    if any(w in text for w in HAKODATE_WORDS):
        return True
    return any(p.get("hakodate") for p in places)


def is_fishing(text):
    return any(w in text for w in FISHING_WORDS)


# ---------- 出力

def rfc822(d):
    return email.utils.format_datetime(d)


def write_outputs(all_items):
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    all_items = sorted(all_items, key=lambda i: i.get("date") or i["firstSeen"], reverse=True)
    write_feed(all_items, "hokkaido", "北海道の釣果情報（fog-feeds）", "北海道全域の釣果・釣行情報を複数のサイトから集めたもの。見出しと短い説明、元記事へのリンク。")
    write_feed([i for i in all_items if i.get("hakodate")], "hakodate", "函館周辺の釣果情報（fog-feeds）", "道南・函館周辺の釣果・釣行情報を複数のサイトから集めたもの。見出しと短い説明、元記事へのリンク。")
    rows = "\n".join(f'<li><a href="{html.escape(i["link"])}">{html.escape(i["title"])}</a> <small>{(i.get("date") or i["firstSeen"]).strftime("%m/%d")} {html.escape(i["source"])}'
                     + (" · " + html.escape("・".join(i["places"][:2])) if i.get("places") else "")
                     + (" · " + html.escape("・".join(i["species"])) if i.get("species") else "") + "</small></li>" for i in all_items[:150])
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>北海道の釣果情報</title>
<style>body{{font-family:-apple-system,sans-serif;background:#0B0B0C;color:#eee;margin:16px}}a{{color:#9cc49f}}small{{color:#888}}li{{margin:6px 0}}</style></head>
<body><h1>北海道の釣果情報</h1><p>RSS: <a href="hokkaido.xml">北海道全域</a> ・ <a href="hakodate.xml">函館周辺だけ</a> ・ 更新 {NOW.strftime("%Y-%m-%d %H:%M")} JST ・ {len(all_items)} 件</p><ul>{rows}</ul></body></html>"""
    with open(os.path.join(ROOT, "docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    open(os.path.join(ROOT, "docs", ".nojekyll"), "w").close()


def write_feed(items, name, title, description):
    # fog: 名前空間に場所の座標と魚種を入れる（Fog アプリの地図ピンが直接使う）
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:fog", "https://github.com/sawswork/fog-feeds")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = title
    ET.SubElement(ch, "link").text = "https://github.com/sawswork/fog-feeds"
    ET.SubElement(ch, "description").text = description
    ET.SubElement(ch, "lastBuildDate").text = rfc822(NOW)
    for it in items:
        e = ET.SubElement(ch, "item")
        ET.SubElement(e, "title").text = it["title"]
        ET.SubElement(e, "link").text = it["link"]
        g = ET.SubElement(e, "guid", isPermaLink="true")
        g.text = it["link"]
        d = it.get("date") or it["firstSeen"]
        ET.SubElement(e, "pubDate").text = rfc822(d)
        tail = []
        if it.get("places"):
            tail.append("場所: " + "・".join(it["places"]))
        if it.get("species"):
            tail.append("魚種: " + "・".join(it["species"]))
        desc = it.get("summary") or ""
        if tail:
            desc = (desc + " ｜ " if desc else "") + " / ".join(tail)
        ET.SubElement(e, "description").text = desc + f"（{it['source']}）"
        ET.SubElement(e, "category").text = it["source"]
        if it.get("places"):
            place = PLACE_BY_NAME.get(it["places"][0])
            if place:
                ET.SubElement(e, "fog:place").text = place["name"]
                ET.SubElement(e, "fog:lat").text = f"{place['lat']:.5f}"
                ET.SubElement(e, "fog:lon").text = f"{place['lon']:.5f}"
        if it.get("species"):
            ET.SubElement(e, "fog:species").text = "・".join(it["species"])
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(os.path.join(ROOT, "docs", f"{name}.xml"), encoding="utf-8", xml_declaration=True)
    with open(os.path.join(ROOT, "docs", f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump([{**it, "date": (it.get("date") or it["firstSeen"]).isoformat(), "firstSeen": it["firstSeen"].isoformat()} for it in items], f, ensure_ascii=False, indent=1)


# ---------- 本体

def collect_source(src):
    url = src["url"]
    kind = src.get("type", "auto")
    try:
        data, ctype = fetch(url)
    except Exception as e:  # noqa: BLE001
        if src.get("fallback"):
            log(f"  {url}: {e} → fallback")
            url = src["fallback"]
            data, ctype = fetch(url)
        else:
            raise
    text = decode(data, ctype)
    if kind in ("rss", "auto"):
        parsed = parse_feed(text, src)
        if parsed and parsed[1]:
            return parsed[1]
        if kind == "rss":
            return []
    # HTML：まずページが指す RSS
    p, items = parse_html_links(text, url, src)
    if os.environ.get("FOG_DEBUG"):
        log(f"  [{src['name']}] title={p.title!r} feeds={p.feeds[:3]} raw links={len(p.links)}")
        for href, label in p.links[:25]:
            log("    ", repr(re.sub(r"\s+", " ", label).strip()[:40]), urllib.parse.urljoin(url, href)[:100])
    if kind == "auto" and p.feeds:
        feed_url = urllib.parse.urljoin(url, p.feeds[0])
        try:
            d2, c2 = fetch(feed_url)
            parsed = parse_feed(decode(d2, c2), src)
            if parsed and parsed[1]:
                return parsed[1]
        except Exception as e:  # noqa: BLE001
            log("  discovered feed failed", feed_url, e)
    return items


def main():
    sources = load_json("collector/sources.json", [])
    state = load_json("data/items.json", {"items": []})
    known = {i["link"]: i for i in state["items"]}
    for i in known.values():
        i["firstSeen"] = dt.datetime.fromisoformat(i["firstSeen"])
        i["date"] = dt.datetime.fromisoformat(i["date"]) if i.get("date") else None
    enriched = 0
    stats = []
    for src in sources:
        try:
            items = collect_source(src)
        except Exception as e:  # noqa: BLE001
            log(f"[{src['name']}] failed: {e}")
            stats.append((src["name"], "失敗", 0, 0))
            continue
        kept = 0
        src_enriched = 0
        for it in items:
            if kept >= MAX_NEW_PER_SOURCE:
                break
            if not it.get("link") or not it["link"].startswith("http"):
                continue
            if it["link"] in known:
                continue
            text = it["title"] + " " + it.get("summary", "")
            if src.get("needFishing") and not is_fishing(text):
                continue
            if src.get("needWords") and not any(w in text for w in src["needWords"]):
                continue
            can_enrich = enriched < MAX_ENRICH and src_enriched < MAX_ENRICH_PER_SOURCE and not src.get("noEnrich")
            if src.get("needRegion") and not is_regional(text):
                if not can_enrich:
                    continue
                enrich(it)
                enriched += 1; src_enriched += 1
                text = it["title"] + " " + it.get("summary", "")
                if not is_regional(text):
                    continue
            elif (len(it.get("summary") or "") < 40 or not it.get("date")) and can_enrich:
                enrich(it)
                enriched += 1; src_enriched += 1
                text = it["title"] + " " + it.get("summary", "")
            found = extract_places(text)
            if not found and src.get("defaultPlace") and src["defaultPlace"] in PLACE_BY_NAME:
                found = [PLACE_BY_NAME[src["defaultPlace"]]]
            it["places"] = [p["name"] for p in found]
            it["species"] = extract_species(text)
            it["hakodate"] = bool(src.get("hakodateDefault")) or is_hakodate(text, found)
            it["firstSeen"] = NOW
            known[it["link"]] = it
            kept += 1
            time.sleep(0.5)
        stats.append((src["name"], "OK", len(items), kept))
        log(f"[{src['name']}] {len(items)} links, {kept} new")
    # 既存の記事も今の規則で場所・魚種・函館周辺を付け直す（辞書や取得元の設定を変えたとき用）
    by_name = {s["name"]: s for s in sources}
    for it in known.values():
        src = by_name.get(it["source"], {})
        text = it["title"] + " " + (it.get("summary") or "")
        found = extract_places(text)
        if not found and src.get("defaultPlace") and src["defaultPlace"] in PLACE_BY_NAME:
            found = [PLACE_BY_NAME[src["defaultPlace"]]]
        it["places"] = [p["name"] for p in found]
        it["species"] = extract_species(text)
        it["hakodate"] = bool(src.get("hakodateDefault")) or is_hakodate(text, found)
    # 古いものを捨てる。ただし取得元ごとに新しい 10 件は残す（更新の少ないサイトが空にならないように）
    cutoff = NOW - dt.timedelta(days=KEEP_DAYS)
    all_items = sorted(known.values(), key=lambda i: i.get("date") or i["firstSeen"], reverse=True)
    per_source = {}
    keep = []
    for i in all_items:
        n = per_source.get(i["source"], 0)
        if (i.get("date") or i["firstSeen"]) >= cutoff or n < 10:
            keep.append(i)
            per_source[i["source"]] = n + 1
    items = keep[:MAX_ITEMS]
    write_outputs(items)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "items.json"), "w", encoding="utf-8") as f:
        json.dump({"updatedAt": NOW.isoformat(), "stats": [{"source": s, "status": st, "links": n, "new": k} for s, st, n, k in stats],
                   "items": [{**i, "date": i["date"].isoformat() if i.get("date") else None, "firstSeen": i["firstSeen"].isoformat()} for i in items]},
                  f, ensure_ascii=False, indent=1)
    log(f"total {len(items)} items; enriched {enriched}")


if __name__ == "__main__":
    main()
