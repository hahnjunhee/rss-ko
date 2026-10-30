#!/usr/bin/env python3
"""feeds.txt의 피드를 가져와 새 글을 한글 제목·요약으로 만든 뒤 update_feed.py add 로 반영한다.

환경변수: ANTHROPIC_API_KEY (필수), SUMMARY_MODEL (기본 claude-haiku-5-5), MAX_NEW (한 번에 요약할 최대 글 수, 기본 20),
NEW_ITEMS_PATH (요약 결과를 남길 JSON 경로, 없으면 임시 파일), GITHUB_STEP_SUMMARY (있으면 실행 요약을 덧붙임)
표준 라이브러리만 사용한다.
"""
import html, json, os, re, subprocess, sys, tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PER_FEED = 15
MAX_NEW = int(os.environ.get("MAX_NEW", "20"))
MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-5-5")
MIN_TEXT = 40      # 본문이 이보다 짧으면 제목만으로 요약한다
MAX_TEXT = 4000    # 모델에 보내는 본문 최대 길이
UA = "Mozilla/5.0 (compatible; rss-ko/1.0; +https://github.com/hahnjunhee/rss-ko)"

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

SYSTEM = (
    "너는 외국어 RSS 글을 한국어로 요약하는 편집자다. 입력으로 원제와 피드에 있는 본문 발췌가 주어진다. "
    'JSON 객체 하나만 출력한다: {"title_ko": "...", "summary_ko": "..."}. '
    "title_ko는 자연스러운 한국어 제목, summary_ko는 한국어 2~3문장이다. "
    "피드에 있는 정보 범위 안에서만 쓰고, 원문 문장을 그대로 번역·복사하지 말고 요지만 요약한다. "
    "본문이 비어 있으면 제목만 근거로 짧게 쓰고, 본문이 없어 제목 기준이라는 사실을 한 문장 덧붙인다."
)


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


def summarize(item):
    body = plain(item["body"])
    body = "" if len(body) < MIN_TEXT else body[:MAX_TEXT]
    user = f"원제: {item['title']}\n본문 발췌: {body or '(없음)'}"
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": MODEL, "max_tokens": 600, "system": SYSTEM,
                         "messages": [{"role": "user", "content": user}]}).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    obj = json.loads(m.group(0))
    title_ko, summary_ko = str(obj["title_ko"]).strip(), str(obj["summary_ko"]).strip()
    if not title_ko or not summary_ko:
        raise ValueError("empty summary")
    return title_ko, summary_ko


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY가 없습니다. 레포 Settings → Secrets and variables → Actions 에 등록하세요.")

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
