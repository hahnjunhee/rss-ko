#!/usr/bin/env python3
"""feeds.txt의 피드를 가져와 새 글을 한글 제목·요약으로 만든 뒤 update_feed.py add 로 반영한다.

API 키 없이 동작한다: 피드 본문에서 핵심 문장을 골라(추출 요약) Google 번역(비공식 엔드포인트)으로 한글화한다.
환경변수: MAX_NEW (한 번에 처리할 최대 글 수, 기본 20), SUMMARY_SENTENCES (요약 문장 수, 기본 3),
NEW_ITEMS_PATH (요약 결과를 남길 JSON 경로, 없으면 임시 파일), GITHUB_STEP_SUMMARY (있으면 실행 요약을 덧붙임)
표준 라이브러리만 사용한다.
"""
import html, json, os, re, subprocess, sys, tempfile, time
import urllib.parse, urllib.request
from collections import Counter
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PER_FEED = 15
MAX_NEW = int(os.environ.get("MAX_NEW", "20"))
SENTENCES = int(os.environ.get("SUMMARY_SENTENCES", "3"))
MAX_SUMMARY = 500  # 요약 최대 글자 수(번역 전)
MIN_TEXT = 40      # 본문이 이보다 짧으면 제목만으로 요약한다
MAX_TEXT = 4000    # 요약 대상 본문 최대 길이
UA = "Mozilla/5.0 (compatible; rss-ko/1.0; +https://github.com/hahnjunhee/rss-ko)"

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, d):
        self.parts.append(d)


def plain(s):
    p = _Text()
    p.feed(s or "")
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_rfc822(s):
    s = (s or "").strip()
    if not s:
        return format_datetime(datetime.now(timezone.utc))
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return format_datetime(datetime.now(timezone.utc))
    return format_datetime(d if d.tzinfo else d.replace(tzinfo=timezone.utc))


def text_of(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_feed(data):
    root = ET.fromstring(data)
    out = []
    for it in root.iter("item"):
        out.append({
            "title": text_of(it, "title"),
            "link": text_of(it, "link") or text_of(it, "guid"),
            "pubDate": text_of(it, "pubDate") or text_of(it, "{http://purl.org/dc/elements/1.1/}date"),
            "body": text_of(it, CONTENT + "encoded") or text_of(it, "description"),
        })
    for en in root.iter(ATOM + "entry"):
        link = ""
        for l in en.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href")
                break
        out.append({
            "title": text_of(en, ATOM + "title"),
            "link": link,
            "pubDate": text_of(en, ATOM + "published") or text_of(en, ATOM + "updated"),
            "body": text_of(en, ATOM + "content") or text_of(en, ATOM + "summary"),
        })
    return [i for i in out if i["link"]][:PER_FEED]


def read_feeds():
    lines = (ROOT / "feeds.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def source_names():
    """이미 만든 글의 출처 이름을 호스트별로 재사용해 이름이 흔들리지 않게 한다."""
    names = {}
    p = ROOT / "items.json"
    for i in json.loads(p.read_text(encoding="utf-8")) if p.exists() else []:
        names.setdefault(urlparse(i["link"]).netloc, i.get("source", ""))
    return names


def guess_source(link):
    host = urlparse(link).netloc.lower().removeprefix("www.")
    labels = host.split(".")
    return (labels[-2] if len(labels) >= 2 else host).capitalize()


def extract_summary(title, text, n=SENTENCES, max_chars=MAX_SUMMARY):
    """추출 요약: 단어 빈도 + 제목 단어 일치 + 앞 문장 가중치로 문장 n개를 골라 원래 순서로 잇는다.
    원문 문장만 쓰므로 없는 내용을 지어내지 않고 언어에 상관없이 동작한다."""
    sents = [x for x in re.split(r"(?<=[.!?。！？])\s*", text) if len(x) >= 20]
    if not sents:
        return text[:max_chars]
    tok = lambda t: re.findall(r"\w{2,}", t.lower())
    freq = Counter(w for x in sents for w in set(tok(x)))
    tw = set(tok(title))
    scored = []
    for i, x in enumerate(sents):
        ws = set(tok(x))
        if ws:
            scored.append((sum(freq[w] for w in ws) / len(ws) + 2 * len(ws & tw) + 3 / (i + 1), i))
    picked = sorted(i for _, i in sorted(scored, reverse=True)[:n])
    out = " ".join(sents[i] for i in picked)
    return out[:max_chars] if len(out) <= max_chars else out[:max_chars].rsplit(" ", 1)[0]


def translate(text, delay=0.4):
    """Google 번역(비공식 gtx 엔드포인트, POST). 이미 한글이 대부분이면 그대로 둔다. 실패하면 예외."""
    if not text or len(re.findall(r"[가-힣]", text)) > len(text) * 0.3:
        return text
    req = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ko&dt=t",
        data=urllib.parse.urlencode({"q": text[:4500]}).encode(), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = "".join(seg[0] for seg in json.load(r)[0] if seg and seg[0])
    if not out.strip():
        raise ValueError("empty translation")
    time.sleep(delay)
    return out.strip()


def summarize(item):
    body = plain(item["body"])
    body = "" if len(body) < MIN_TEXT else body[:MAX_TEXT]
    title_ko = translate(html.unescape(item["title"]))
    summary_ko = translate(extract_summary(item["title"], body)) if body else "(피드에 본문이 없어 제목만 번역했습니다)"
    return title_ko, summary_ko


def main():
    seen = json.loads((ROOT / "seen.json").read_text(encoding="utf-8")) if (ROOT / "seen.json").exists() else {}
    names = source_names()
    candidates, failed = [], []
    for url in read_feeds():
        try:
            for it in parse_feed(http_get(url)):
                it["feed"] = url
                candidates.append(it)
        except Exception as e:
            failed.append((url, str(e)))
            print(f"::warning::피드 수집 실패 {url}: {e}")

    # 같은 글을 내보내는 피드가 둘 이상이면 먼저 나온 것만 쓴다.
    fresh, in_run = [], set()
    for c in candidates:
        if c["link"] not in seen and c["link"] not in in_run:
            in_run.add(c["link"])
            fresh.append(c)
    # 새 글이 많으면 최신순으로 MAX_NEW개만 처리하고 나머지는 다음 실행에 처리한다.
    fresh.sort(key=lambda c: parse_date_key(c["pubDate"]), reverse=True)
    fresh = fresh[:MAX_NEW]
    print(f"feeds failed={len(failed)} candidates={len(candidates)} new={len(fresh)}")
    if failed and not candidates:
        write_step_summary(failed, 0, 0, [], [])
        sys.exit("모든 피드 수집에 실패했습니다.")

    new_items, sum_failed = [], []
    for c in fresh:
        try:
            title_ko, summary_ko = summarize(c)
        except Exception as e:
            print(f"::warning::요약 실패 {c['link']}: {e}")
            sum_failed.append((c["link"], str(e)))
            continue
        host = urlparse(c["link"]).netloc
        new_items.append({
            "source": names.get(host) or names.setdefault(host, guess_source(c["link"])),
            "link": c["link"],
            "pubDate": to_rfc822(c["pubDate"]),
            "title_orig": html.unescape(c["title"]),
            "title_ko": title_ko,
            "summary_ko": summary_ko,
        })

    write_step_summary(failed, len(candidates), len(fresh), new_items, sum_failed)
    if not new_items:
        print("새 글 없음")
        return
    # 워크플로가 충돌 시 요약을 다시 부르지 않고 이 파일로 add 를 재실행한다.
    path = os.environ.get("NEW_ITEMS_PATH")
    if path:
        Path(path).write_text(json.dumps(new_items, ensure_ascii=False), encoding="utf-8")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(new_items, f, ensure_ascii=False)
        path = f.name
    subprocess.run([sys.executable, str(ROOT / "scripts" / "update_feed.py"), "add", path], check=True)


def write_step_summary(failed, n_cand, n_new, new_items, sum_failed):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if not p:
        return
    lines = ["## Feed update", "",
             f"- 후보 {n_cand}건 / 신규 {n_new}건 / 요약 성공 {len(new_items)}건 / 요약 실패 {len(sum_failed)}건"]
    if failed:
        lines += ["", f"### 수집 실패 피드 ({len(failed)})"] + [f"- {u}: {err}" for u, err in failed]
    if sum_failed:
        lines += ["", f"### 요약 실패 ({len(sum_failed)})"] + [f"- {u}: {err}" for u, err in sum_failed]
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def parse_date_key(s):
    try:
        return parsedate_to_datetime(to_rfc822(s))
    except Exception:
        return datetime.now(timezone.utc)


if __name__ == "__main__":
    main()
