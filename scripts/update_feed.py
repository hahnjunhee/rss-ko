#!/usr/bin/env python3
"""rss-ko feed builder.

Usage:
  update_feed.py check <candidates.json>   # prints JSON list of links not yet in seen.json
  update_feed.py add <new_items.json>      # merges items, rewrites feed.xml / items.json / seen.json

new_items.json: [{"source","link","pubDate","title_orig","title_ko","summary_ko"}, ...]
"""
import json, sys, html
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime, format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEN, ITEMS, FEED = ROOT / "seen.json", ROOT / "items.json", ROOT / "feed.xml"
MAX_ITEMS = 200
SEEN_KEEP_DAYS = 120
FEED_URL = "https://hahnjunhee.github.io/rss-ko/feed.xml"


def load(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def parse_date(s):
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def cmd_check(path):
    seen = load(SEEN, {})
    cands = json.loads(Path(path).read_text(encoding="utf-8"))
    links = [c["link"] if isinstance(c, dict) else c for c in cands]
    print(json.dumps([l for l in links if l not in seen], ensure_ascii=False, indent=1))


def cmd_add(path):
    now = datetime.now(timezone.utc)
    seen, items = load(SEEN, {}), load(ITEMS, [])
    new = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {i["link"] for i in items}
    added = 0
    for n in new:
        if not n.get("link") or n["link"] in known:
            continue
        for k in ("source", "pubDate", "title_orig", "title_ko", "summary_ko"):
            n.setdefault(k, "")
        items.append(n)
        known.add(n["link"])
        seen[n["link"]] = now.date().isoformat()
        added += 1
    items.sort(key=lambda i: parse_date(i["pubDate"]), reverse=True)
    items = items[:MAX_ITEMS]
    cutoff = (now - timedelta(days=SEEN_KEEP_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    save(ITEMS, items)
    save(SEEN, seen)
    write_feed(items, now)
    print(f"added={added} total={len(items)}")


def write_feed(items, now):
    e = lambda s: html.escape(s or "", quote=True)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>RSS-KO 한글 요약 피드</title>",
        "<link>https://github.com/hahnjunhee/rss-ko</link>",
        f'<atom:link href="{FEED_URL}" rel="self" type="application/rss+xml"/>',
        "<description>외국어 RSS 피드를 한글 제목·요약으로 통합한 피드</description>",
        "<language>ko</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for i in items:
        body = (
            f"<p>{e(i['summary_ko'])}</p>"
            f"<p>원제: {e(i['title_orig'])}<br/>출처: {e(i['source'])}</p>"
        )
        out += [
            "<item>",
            f"<title>[{e(i['source'])}] {e(i['title_ko'])}</title>",
            f"<link>{e(i['link'])}</link>",
            f'<guid isPermaLink="true">{e(i["link"])}</guid>',
            f"<pubDate>{format_datetime(parse_date(i['pubDate']))}</pubDate>",
            f"<description>{e(body)}</description>",
            "</item>",
        ]
    out += ["</channel>", "</rss>", ""]
    FEED.write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    {"check": cmd_check, "add": cmd_add}[sys.argv[1]](sys.argv[2])
