#!/usr/bin/env python3
"""feeds.txt의 피드를 가져와 새 글을 한글 제목·요약으로 만든 뒤 update_feed.py add 로 반영한다.

API 키 없이 동작한다: 피드 본문에서 핵심 문장을 골라(추출 요약) Google 번역(비공식 엔드포인트)으로 한글화한다.
환경변수: MAX_NEW (한 번에 처리할 최대 글 수, 기본 20), SUMMARY_SENTENCES (요약 문장 수, 기본 3),
NEW_ITEMS_PATH (요약 결과를 남길 JSON 경로, 없으면 임시 파일), GITHUB_STEP_SUMMARY (있으면 실행 요약을 덧붙임),
DRY_RUN_OUT (--dry-run 결과 경로, 기본 dry-run-items.json)
--dry-run: 이미 처리한 글까지 포함해 요약·번역하되 피드·items·seen·번역 캐시는 건드리지 않고 결과와 자동 검사 경고만 낸다.
표준 라이브러리만으로 동작하며, 원문 수집(fulltext.txt에 적은 호스트만)은 선택 의존성 trafilatura가 있을 때만 쓴다.
"""
import hashlib, html, json, os, re, subprocess, sys, tempfile, time, unicodedata
import urllib.error, urllib.parse, urllib.request, urllib.robotparser
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
PROCESS_VERSION = "1"  # 요약·번역 규칙을 바꾸면 올린다(번역 캐시 키에 들어가 이전 결과를 재사용하지 않는다)
RETRIES = 2        # 번역 일시 오류 재시도 횟수
BACKOFF = (5, 15)  # 재시도 전 대기(초). 429 응답에 Retry-After가 있으면 그 값(최대 MAX_WAIT초)을 따른다
MAX_WAIT = 60
BREAKER = 3        # 글이 연속으로 이만큼 실패하면 서비스 장애로 보고 이번 실행의 나머지를 건너뛴다
MAX_ATTEMPTS = 3   # 번역 실패로 원문을 임시 게재한 글을 다시 번역해 보는 최대 횟수
CACHE_KEEP = 1000  # 번역 캐시 보관 개수
TCACHE = {}        # 번역 캐시: sha1(버전+원문) -> 한글
RETRY_CODES = {429, 500, 502, 503, 504}
MAX_TEXT = 4000    # 요약 대상 본문 최대 길이
UA = "Mozilla/5.0 (compatible; rss-ko/1.0; +https://github.com/hahnjunhee/rss-ko)"

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "table", "section", "article", "blockquote",
              "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "figcaption"}
ZERO_WIDTH = re.compile("[\u200b-\u200d\u2060\ufeff]")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, d):
        self.parts.append(d)

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self.parts.append(" ")  # 문단·제목 사이에 공백이 없으면 "them.One Game"처럼 문장이 붙는다

    handle_endtag = handle_startendtag = lambda self, tag, *a: self.parts.append(" ") if tag in BLOCK_TAGS else None


def plain(s):
    p = _Text()
    p.feed(s or "")
    return re.sub(r"\s+", " ", ZERO_WIDTH.sub("", "".join(p.parts))).strip()


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


STRONG_JUNK = re.compile(r"article url:|comments url:|appeared first on", re.I)
WEAK_JUNK = re.compile(
    r"subscribe|follow us|share this|read more|continue reading|click here|all rights reserved|sign up for|newsletter|"
    r"フォロー|シェア|購読|チャンネル登録|ハッシュタグ|いいね|구독|공유하기|팔로우|"
    r"続きをみる|続きを読む|続きはこちら|もっと見る|계속 보기|더 보기", re.I)
HASHTAGS = re.compile(r"(?:[#＃]\S+\s*)+")
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")
WORD = re.compile(r"[0-9A-Za-z\u00c0-\u024f\uac00-\ud7af]{2,}")


def is_junk(sent):
    """푸터·구독/공유 문구·해시태그만 있는 줄. 짧은 문장에만 약한 규칙을 적용해 본문 문장을 버리지 않는다."""
    return bool(STRONG_JUNK.search(sent) or HASHTAGS.fullmatch(sent) or (len(sent) < 80 and WEAK_JUNK.search(sent)))


LIST_MARK = re.compile(r"^[\s◆◇■□●○◎▼▽▲△▶►▪・※•*＊#＃\-–—]+")
LEADING_CLOSER = re.compile(r"^[)）\]】」』、，,\s]+")


def split_sentences(text):
    """마침표 뒤에 공백이 있을 때만 영문 문장을 나눠 1.2, U.S. 같은 표기를 쪼개지 않는다. 일본어의 전각 마침표(．)도 숫자 뒤가 아니면 문장 끝으로 본다."""
    parts = re.split(r"(?<=[.!?])\s+|(?<=[。！？])\s*|(?<=[^0-9０-９]．)\s*|\n+", text)
    return [p for p in (LEADING_CLOSER.sub("", x.strip()) for x in parts) if len(p) >= 2 and not is_junk(p)]


def tokens(text):
    """영문·한글은 단어, 공백 없는 일본어·중국어는 문자 2-gram을 토큰으로 쓴다."""
    text = text.lower()
    out = set(WORD.findall(text))
    for run in CJK.findall(text):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


TITLE_ECHO = 0.6    # 문장 토큰의 이 비율 이상이 제목과 겹치면 제목 되풀이로 본다
OVERLAP_CAP = 2     # 제목과 겹치는 토큰에 주는 가산점 상한
LEAD_WEIGHT = 6     # 앞 문장 가산점: 글의 핵심은 대개 앞쪽에 있다
SHORT_PENALTY = 4   # "落合陽一です．" 같은 인사·자기소개성 짧은 문장은 요약에 잘 안 뽑히게 한다


def short_sentence(sent):
    """일본어·중국어는 15자, 그 외는 25자 미만이면 짧은 문장으로 본다."""
    cjk = sum(len(r) for r in CJK.findall(sent)) > len(sent) * 0.5
    return len(sent) < (15 if cjk else 25)


def extract_summary(title, text, n=SENTENCES, max_chars=MAX_SUMMARY):
    """추출 요약: 잡문장을 빼고, 단어 빈도·제목 일치·앞 문장·숫자 포함 여부로 문장 n개를 골라 원래 순서로 잇는다.
    원문 문장만 쓰므로 없는 내용을 지어내지 않고, 문장이 n개 이하이면 줄이지 않는다."""
    sents = split_sentences(text)
    if not sents:
        return ""
    toks = [tokens(x) for x in sents]
    tw = tokens(title)
    # 제목을 되풀이한 문장과 ◆·■·・ 등으로 시작하는 소제목·목록 줄은 요약 후보에서 뺀다(전부 해당하면 모두 후보로 둔다).
    cand = [i for i, x in enumerate(sents)
            if not LIST_MARK.match(x) and not (tw and len(toks[i]) >= 3 and len(toks[i] & tw) / len(toks[i]) >= TITLE_ECHO)]
    cand = cand or list(range(len(sents)))
    if len(cand) <= n:
        picked, score = list(cand), {i: -i for i in cand}
    else:
        freq = Counter(w for t in toks for w in t)
        common = {w for w, c in freq.items() if c == len(sents)} if len(sents) >= 4 else set()
        score = {}
        for i in cand:
            t = toks[i] - common
            base = sum(freq[w] for w in t) / len(t) if t else 0
            score[i] = (base + min(2 * len(t & tw), OVERLAP_CAP) + LEAD_WEIGHT / (i + 1)
                        + (0.5 if re.search(r"\d", sents[i]) else 0) - (SHORT_PENALTY if short_sentence(sents[i]) else 0))
        picked = []
        for i in sorted(score, key=lambda k: (-score[k], k)):
            if len(picked) >= n:
                break
            if any(len(toks[i] & toks[j]) / max(1, len(toks[i] | toks[j])) > 0.7 for j in picked):
                continue  # 이미 고른 문장과 거의 같다
            picked.append(i)
    while len(picked) > 1 and len(" ".join(sents[i] for i in sorted(picked))) > max_chars:
        picked.remove(min(picked, key=lambda k: score[k]))
    out = " ".join(sents[i] for i in sorted(picked))
    if len(out) > max_chars:
        out = out[:max_chars]
        out = out.rsplit(" ", 1)[0] if " " in out else out
    return out


def source_summary(item):
    """원문을 수집한 글이면 그 본문을, 아니면 RSS 본문을 요약 대상으로 쓴다."""
    text = item.get("fulltext") or plain(item["body"])[:MAX_TEXT]
    return extract_summary(html.unescape(item["title"]), text)


FULLTEXT_DELAY = 1.5                 # 같은 호스트에 대한 요청 간격(초)
FULLTEXT_MAX = int(os.environ.get("FULLTEXT_MAX", "30"))  # 한 실행에서 원문을 가져올 최대 글 수
FULLTEXT_MAX_BYTES = 2_000_000       # 응답 크기 상한
FULLTEXT_MIN_GAIN = 50               # 원문이 RSS 발췌보다 이만큼 길 때만 쓴다
ROBOT_TOKEN = "rss-ko"
FULLTEXT_STATS, FULLTEXT_USED = Counter(), [0]
FULLTEXT_LAST_ERR = {}
_ROBOTS, _LAST_HIT = {}, {}
TRUNCATED = re.compile(r"続きをみる|続きを読む|もっと見る|계속 보기|더 보기|read more|continue reading|\[…\]|\[\.\.\.\]|…\s*$|\.\.\.\s*$", re.I)
PAYWALL = re.compile(r"ここから先は有料|ここから有料|この続きは有料|続きは有料|continue reading with a subscription|subscribe to (?:continue|read)", re.I)


def fulltext_hosts():
    """fulltext.txt에 적은 호스트(한 줄에 하나, #은 주석)만 원문을 수집한다. 파일이 없으면 기능이 꺼져 있다."""
    p = ROOT / "fulltext.txt"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    return [l.split("#")[0].strip().lower() for l in lines if l.split("#")[0].strip()]


def host_allowed(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return urlparse(url).scheme in ("http", "https") and any(host == h or host.endswith("." + h) for h in fulltext_hosts())


def wants_fulltext(item, excerpt):
    """허용된 호스트이면서 RSS 발췌가 잘렸거나 짧을 때만 원문을 가져온다(글자 수 하나로만 정하지 않는다)."""
    return host_allowed(item["link"]) and (bool(TRUNCATED.search(excerpt)) or len(excerpt) < 300)


def fetch_robots(host, scheme):
    """(상태코드, 본문). 네트워크 오류는 상태코드 0."""
    try:
        req = urllib.request.Request(f"{scheme}://{host}/robots.txt", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read(500_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def robots_allows(url):
    """robots.txt가 없거나(4xx) 허용하면 True. 서버 오류·네트워크 오류면 보수적으로 False."""
    u = urlparse(url)
    key = (u.scheme, u.netloc)
    if key not in _ROBOTS:
        status, text = fetch_robots(u.netloc, u.scheme)
        rp = urllib.robotparser.RobotFileParser()
        if status == 200:
            rp.parse(text.splitlines())
            _ROBOTS[key] = rp.can_fetch
        else:
            _ROBOTS[key] = (lambda ua, link: True) if 400 <= status < 500 else (lambda ua, link: False)
    return _ROBOTS[key](ROBOT_TOKEN, url)


def fetch_html(url):
    """호스트별 간격을 지키며 HTML을 받는다. 크기 초과·HTML 아님·허용 호스트 밖으로의 리다이렉트는 ValueError."""
    host = urlparse(url).netloc
    wait = FULLTEXT_DELAY - (time.time() - _LAST_HIT.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _LAST_HIT[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if not host_allowed(r.geturl()):
            raise ValueError(f"redirected outside allowed hosts: {r.geturl()}")
        if "html" not in r.headers.get("Content-Type", "html"):
            raise ValueError("not html")
        data = r.read(FULLTEXT_MAX_BYTES + 1)
        if len(data) > FULLTEXT_MAX_BYTES:
            raise ValueError("response too large")
        return data.decode(r.headers.get_content_charset() or "utf-8", "replace")


def extract_text(page):
    """trafilatura로 본문만 추출(댓글 제외). 라이브러리가 없으면 빈 문자열."""
    try:
        import trafilatura
    except ImportError:
        FULLTEXT_STATS["nolib"] += 1
        return ""
    return trafilatura.extract(page, include_comments=False, include_tables=False, favor_precision=True) or ""


def get_fulltext(item):
    """허용된 호스트의 잘린 글이면 공개 원문 본문을 돌려준다. 어떤 이유로든 실패하면 빈 문자열(RSS 발췌를 쓴다)."""
    excerpt = plain(item["body"])
    if not wants_fulltext(item, excerpt):
        return ""
    if FULLTEXT_USED[0] >= FULLTEXT_MAX:
        FULLTEXT_STATS["limit"] += 1
        return ""
    url = item["link"]
    try:
        if not robots_allows(url):
            FULLTEXT_STATS["robots"] += 1
            return ""
        FULLTEXT_USED[0] += 1
        page = fetch_html(url)
    except Exception as e:
        FULLTEXT_STATS["fetch"] += 1
        FULLTEXT_LAST_ERR["fetch"] = f"{type(e).__name__}: {e}"[:200]
        return ""
    text = ZERO_WIDTH.sub("", extract_text(page))
    m = PAYWALL.search(text)
    if m:  # 유료 구간은 가져오지 않는다
        text = text[:m.start()]
        FULLTEXT_STATS["paywall_cut"] += 1
    text = "\n".join(l.strip() for l in text.splitlines() if l.strip())[:MAX_TEXT]
    if len(text) < len(excerpt) + FULLTEXT_MIN_GAIN:
        FULLTEXT_STATS["no_gain"] += 1
        return ""
    FULLTEXT_STATS["ok"] += 1
    return text


def cache_key(text):
    return hashlib.sha1(f"{PROCESS_VERSION}\0{text}".encode()).hexdigest()


def _open(req):
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def gtx_api(text):
    """translate.googleapis.com gtx 엔드포인트(POST)."""
    req = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ko&dt=t",
        data=urllib.parse.urlencode({"q": text[:4500]}).encode(), headers={"User-Agent": UA})
    return "".join(seg[0] for seg in json.loads(_open(req))[0] if seg and seg[0])


SHAPES = Counter()
LANG_CODE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]+)?$")


def parse_chrome(data):
    """clients5 응답 모양이 여러 가지라 알려진 형태를 모두 받고, 여러 문장 구간은 모두 이어 붙인다."""
    if isinstance(data, dict):
        SHAPES["dict"] += 1
        return "".join(x.get("trans", "") for x in data.get("sentences", []) if isinstance(x, dict))
    if isinstance(data, list) and data:
        if all(isinstance(x, str) for x in data):
            SHAPES["list[str]"] += 1
            parts = data[:-1] if len(data) > 1 and LANG_CODE.match(data[-1]) else data  # 끝의 감지 언어 코드 제외
            return "".join(parts)
        if all(isinstance(x, list) and x and isinstance(x[0], str) for x in data):
            SHAPES["list[list]"] += 1
            return "".join(x[0] for x in data)
        SHAPES["other"] += 1
    return ""


def gtx_chrome(text):
    """clients5.google.com 크롬 확장용 엔드포인트(POST). IP 제한이 gtx_api와 따로 걸리는 경우가 많다."""
    req = urllib.request.Request(
        "https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=ko&dt=t",
        data=urllib.parse.urlencode({"q": text[:4500]}).encode(), headers={"User-Agent": UA})
    return parse_chrome(json.loads(_open(req)))


def parse_mobile(page):
    m = re.search(r'class="result-container">(.*?)</div>', page, re.S)
    return html.unescape(m.group(1)) if m else ""


def gtx_mobile(text):
    """translate.google.com/m 모바일 웹(GET). URL 길이 제한 때문에 인코딩 후 6000자를 넘으면 쓰지 않는다."""
    q = urllib.parse.quote(text[:4500])
    if len(q) > 6000:
        raise ValueError("too long for mobile endpoint")
    req = urllib.request.Request(f"https://translate.google.com/m?sl=auto&tl=ko&q={q}",
                                 headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"})
    return parse_mobile(_open(req))


ENGINES = [gtx_api, gtx_chrome, gtx_mobile]
SKIP_AFTER = 3  # 한 엔진이 이만큼 연속 실패하면 이번 실행의 나머지 호출에서는 건너뛴다
ENGINE_STATS, ENGINE_FAILS, ENGINE_STREAK, ENGINE_SHORT, ENGINE_FIXED = Counter(), Counter(), Counter(), Counter(), Counter()
ENGINE_LAST_ERR = {}


def engine_name(engine):
    return getattr(engine, "__name__", repr(engine))


def too_short(src, out):
    """번역이 원문보다 비정상적으로 짧다(구간 누락 의심). 일본어·중국어 원문은 글자 수가 비슷해야 하므로 기준이 더 높다."""
    src, out = src.strip(), out.strip()
    if len(src) < 12:
        return False
    cjk = sum(len(r) for r in CJK.findall(src)) > len(src) * 0.5
    return len(out) < len(src) * (0.45 if cjk else 0.25)


SPLIT_AT = re.compile(r"(?<=[』」）)”\"’])[\s\u3000]+|(?<=[、。，．,.!?！？])[\s\u3000]+")
GTX_ERRORS = (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError)


OPEN_BRACKETS, CLOSE_BRACKETS = "『「【〈《", "』」】〉》"


def normalize_brackets(text):
    """『』「」【】 같은 CJK 괄호를 일반 따옴표로 바꾼다. 번역기가 괄호 뒤를 버리는 경우의 재시도용이다."""
    seq = [c for c in text if c in OPEN_BRACKETS + CLOSE_BRACKETS]
    return text.translate(str.maketrans({c: '"' for c in OPEN_BRACKETS + CLOSE_BRACKETS})), seq


def restore_brackets(out, seq):
    """번역문의 따옴표 개수가 원래 괄호 개수와 같을 때만 원래 괄호로 되돌린다."""
    if out.count('"') != len(seq):
        return out
    it = iter(seq)
    return re.sub('"', lambda m: next(it), out)


def split_parts(text):
    """닫는 괄호·따옴표·문장부호 뒤의 공백에서만 나눈다(영어 단어 단위로 쪼개지 않는다). 2~6조각일 때만 돌려준다."""
    parts = [p for p in SPLIT_AT.split(text.strip()) if p.strip()]
    return parts if 2 <= len(parts) <= 6 else []


def gtx(text, split=True):
    """엔진을 순서대로 시도해 처음 성공한 결과를 돌려준다. 연속 실패한 엔진은 건너뛴다(모두 건너뛰게 되면 전부 다시 시도).
    결과가 비정상적으로 짧으면(구간 누락 의심) 다음 엔진 결과와 비교하고, 그래도 짧으면 먼저 CJK 괄호를 따옴표로 바꿔
    다시 번역하고, 그래도 짧으면 문장부호·괄호 기준 조각별로 따로 번역해 이어 붙인 결과가 더 길 때 그것을 쓴다. 모두 실패하면 처음 시도한 엔진의 예외를 올린다
    (재시도 여부는 translate()가 그 오류 코드로 판단한다)."""
    first, short = None, None
    live = [e for e in ENGINES if ENGINE_STREAK[engine_name(e)] < SKIP_AFTER] or ENGINES
    for engine in live:
        name = engine_name(engine)
        try:
            out = ZERO_WIDTH.sub("", engine(text)).strip()
            if not out:
                raise ValueError("empty translation")
        except GTX_ERRORS as e:
            ENGINE_STREAK[name] += 1
            ENGINE_FAILS[name] += 1
            ENGINE_LAST_ERR[name] = f"{type(e).__name__}: {e}"[:200]
            first = first or e
            continue
        ENGINE_STREAK[name] = 0
        if too_short(text, out):
            ENGINE_SHORT[name] += 1
            if short is None or len(out) > len(short[1]):
                short = (name, out)
            continue
        ENGINE_STATS[name] += 1
        return out
    if short:
        if split:
            norm, seq = normalize_brackets(text)
            if seq:  # 1단계: 괄호를 따옴표로 바꿔 문맥을 유지한 채 한 번에 다시 번역
                try:
                    fixed = gtx(norm, split=False)
                except GTX_ERRORS:
                    fixed = ""
                if fixed and not too_short(norm, fixed):
                    ENGINE_FIXED["bracket"] += 1
                    return restore_brackets(fixed, seq)
            parts = split_parts(text)  # 2단계: 괄호·문장부호 기준 조각별 번역
            if parts:
                try:
                    joined = " ".join(gtx(p, split=False) for p in parts)
                except GTX_ERRORS:
                    joined = ""
                if len(joined) > len(short[1]):
                    ENGINE_FIXED["split"] += 1
                    return joined
        ENGINE_STATS[short[0]] += 1
        return short[1]
    raise first


def append_step_summary(lines):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def report_engines():
    lines = [f"번역 엔진 사용: {dict(ENGINE_STATS)}"]
    if ENGINE_SHORT:
        lines.append(f"번역이 원문보다 비정상적으로 짧아 다른 엔진과 비교한 횟수: {dict(ENGINE_SHORT)}")
    if ENGINE_FIXED:
        lines.append(f"짧은 번역을 복구한 횟수(bracket=괄호를 따옴표로 바꿔 재번역, split=조각별 재번역): {dict(ENGINE_FIXED)}")
    if SHAPES:
        lines.append(f"clients5 응답 모양: {dict(SHAPES)}")
    if FULLTEXT_STATS:
        lines.append(f"원문 수집: {dict(FULLTEXT_STATS)}" + (f", 마지막 오류 {FULLTEXT_LAST_ERR['fetch']}" if "fetch" in FULLTEXT_LAST_ERR else ""))
    lines += [f"번역 엔진 실패 {n}: {c}회, 마지막 오류 {ENGINE_LAST_ERR.get(n, '')}" for n, c in ENGINE_FAILS.items()]
    print("\n".join(lines))
    append_step_summary(["", "### 번역 엔진"] + [f"- {l}" for l in lines])


def report_samples(new_items, by_link, k=10):
    """dry-run 결과에서 k건을 고르게 뽑아 원문 요약과 번역을 나란히 로그와 실행 요약에 남긴다."""
    ok = [i for i in new_items if not i.get("untranslated")]
    picks = ok[::max(1, len(ok) // k)][:k]
    lines = ["## dry-run 샘플", ""]
    for n, i in enumerate(picks, 1):
        src = source_summary(by_link[i["link"]]) or "(본문 없음)"
        c = by_link[i["link"]]
        ft = len(c.get("fulltext") or "")
        origin = f"원문 수집 {ft}자" if ft else f"RSS 발췌 {len(plain(c['body']))}자"
        lines += [f"### {n}. [{i['source']}] {i['title_ko']}", f"- 본문 출처: {origin}", f"- 원제: {i['title_orig']}",
                  f"- 원문 요약: {src}", f"- 번역 요약: {i['summary_ko']}", ""]
    print("::group::dry-run 샘플\n" + "\n".join(lines) + "\n::endgroup::")
    append_step_summary(lines)


def translate(text, delay=1.5):
    """Google 번역(비공식 gtx 엔드포인트). 이미 한글이 대부분이면 그대로 두고, 같은 입력은 캐시를 쓴다.
    429·5xx·타임아웃·빈 응답은 RETRIES번까지 대기(Retry-After 우선) 후 다시 시도하고, 그래도 안 되면 예외."""
    if not text or len(re.findall(r"[가-힣]", text)) > len(text) * 0.3:
        return text
    key = cache_key(text)
    if key in TCACHE:
        return TCACHE[key]
    last = None
    for attempt in range(RETRIES + 1):
        wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
        try:
            out = gtx(text)
            TCACHE[key] = out
            time.sleep(delay)
            return out
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES:
                raise
            last = e
            retry_after = (e.headers.get("Retry-After") if e.headers else None) or ""
            if retry_after.strip().isdigit():
                wait = min(int(retry_after), MAX_WAIT)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
        if attempt < RETRIES:
            time.sleep(wait)
    raise last


def summarize(item):
    body = source_summary(item)
    title_ko = translate(html.unescape(item["title"]))
    summary_ko = translate(body) if body else "(피드에 본문이 없어 제목만 번역했습니다)"
    return title_ko, summary_ko


def fallback(item):
    """번역이 실패한 글: 원문 제목과 원문 요약을 그대로 싣는다(원문 링크는 항목에 이미 있다)."""
    return html.unescape(item["title"]), source_summary(item) or "(본문 없음)"


def check_item(src_title, src_summary, title_ko, summary_ko):
    """번역 결과 자동 검사: 비어 있음, 숫자 누락, 한글 비율이 낮음. 경고 문자열 목록을 돌려준다."""
    norm = lambda t: unicodedata.normalize("NFKC", t)
    flags = []
    for label, src, ko in (("제목", src_title, title_ko), ("요약", src_summary, summary_ko)):
        if not src:
            continue
        if not ko.strip():
            flags.append(f"{label} 비어 있음")
            continue
        if too_short(src, ko):
            flags.append(f"{label} 번역이 원문보다 비정상적으로 짧음")
        missing = sorted({n for n in re.findall(r"\d+(?:[.,]\d+)*", norm(src)) if n not in norm(ko)})
        if missing:
            flags.append(f"{label} 숫자 누락 {missing}")
        letters = re.findall(r"[^\W\d_]", ko)
        if len(re.findall(r"[가-힣]", ko)) < len(letters) * 0.3 and len(letters) >= 10:
            flags.append(f"{label} 한글 비율 낮음")
    return flags


def load_tcache():
    TCACHE.clear()
    p = ROOT / "translations.json"
    if p.exists():
        TCACHE.update(json.loads(p.read_text(encoding="utf-8")))


def save_tcache():
    keep = dict(list(TCACHE.items())[-CACHE_KEEP:])
    (ROOT / "translations.json").write_text(json.dumps(keep, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


def untranslated_items():
    """번역 실패로 원문을 임시 게재했고 재시도 횟수가 남은 글(링크 -> 항목)."""
    p = ROOT / "items.json"
    items = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    return {i["link"]: i for i in items if i.get("untranslated") and i.get("attempts", 0) < MAX_ATTEMPTS}


def build_item(c, names, title_ko, summary_ko):
    host = urlparse(c["link"]).netloc
    return {
        "source": names.get(host) or names.setdefault(host, guess_source(c["link"])),
        "link": c["link"],
        "pubDate": to_rfc822(c["pubDate"]),
        "title_orig": html.unescape(c["title"]),
        "title_ko": title_ko,
        "summary_ko": summary_ko,
    }


def main():
    dry = "--dry-run" in sys.argv
    seen = json.loads((ROOT / "seen.json").read_text(encoding="utf-8")) if (ROOT / "seen.json").exists() else {}
    names = source_names()
    stale = untranslated_items()
    candidates, failed = [], []
    for url in read_feeds():
        try:
            for it in parse_feed(http_get(url)):
                it["feed"] = url
                candidates.append(it)
        except Exception as e:
            failed.append((url, str(e)))
            print(f"::warning::피드 수집 실패 {url}: {e}")

    # 같은 글을 내보내는 피드가 둘 이상이면 먼저 나온 것만 쓴다. 원문 임시 게재 글은 횟수가 남아 있으면 다시 번역한다.
    fresh, in_run = [], set()
    for c in candidates:
        if c["link"] in in_run or (c["link"] in seen and c["link"] not in stale and not dry):
            continue
        in_run.add(c["link"])
        c["attempts"] = stale[c["link"]].get("attempts", 0) if c["link"] in stale else 0
        fresh.append(c)
    # 새 글이 많으면 최신순으로 MAX_NEW개만 처리하고 나머지는 다음 실행에 처리한다.
    fresh.sort(key=lambda c: parse_date_key(c["pubDate"]), reverse=True)
    fresh = fresh[:MAX_NEW]
    print(f"feeds failed={len(failed)} candidates={len(candidates)} new={len(fresh)}")
    if failed and not candidates:
        write_step_summary(failed, 0, 0, [], [])
        sys.exit("모든 피드 수집에 실패했습니다.")

    load_tcache()
    new_items, sum_failed, pending, item_failures, streak = [], [], [], [], 0
    for c in fresh:
        c["fulltext"] = get_fulltext(c)
        try:
            title_ko, summary_ko = summarize(c)
        except Exception as e:
            print(f"::warning::요약 실패 {c['link']}: {e}")
            sum_failed.append((c["link"], str(e)))
            pending.append(c)
            streak += 1
            if streak >= BREAKER:
                print(f"::warning::번역이 연속 {streak}건 실패해 이번 실행의 나머지 글은 다음 실행으로 넘깁니다.")
                break
            continue
        streak = 0
        item_failures += pending  # 뒤 글이 성공했으니 앞의 실패는 글 단위 문제다
        pending = []
        new_items.append(build_item(c, names, title_ko, summary_ko))
    # 맨 뒤에 연속으로 실패한 글(pending)은 서비스 장애일 수 있어 게재하지 않고 다음 실행에서 다시 시도한다.
    for c in item_failures:
        t, sm = fallback(c)
        it = build_item(c, names, t, sm)
        it.update(untranslated=True, attempts=c["attempts"] + 1)
        new_items.append(it)

    write_step_summary(failed, len(candidates), len(fresh), new_items, sum_failed)
    if dry:
        by_link = {c["link"]: c for c in fresh}
        warns = 0
        for it in new_items:
            if it.get("untranslated"):
                continue
            flags = check_item(it["title_orig"], source_summary(by_link[it["link"]]), it["title_ko"], it["summary_ko"])
            warns += bool(flags)
            for f in flags:
                print(f"::warning::검사 {it['link']}: {f}")
        out = Path(os.environ.get("DRY_RUN_OUT") or ROOT / "dry-run-items.json")
        out.write_text(json.dumps(new_items, ensure_ascii=False, indent=1), encoding="utf-8")
        report_engines()
        report_samples(new_items, by_link)
        print(f"dry-run: {len(new_items)}건 → {out} (검사 경고 {warns}건, 실제 파일은 변경하지 않음)")
        return
    save_tcache()
    report_engines()
    if not new_items:
        print("새 글 없음")
        return
    # 워크플로가 충돌 시 번역을 다시 부르지 않고 이 파일로 add 를 재실행한다.
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
    ok = [i for i in new_items if not i.get("untranslated")]
    lines = ["## Feed update", "",
             f"- 후보 {n_cand}건 / 신규 {n_new}건 / 요약 성공 {len(ok)}건 / 요약 실패 {len(sum_failed)}건 / 원문 임시 게재 {len(new_items) - len(ok)}건"]
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
