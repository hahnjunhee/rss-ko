#!/usr/bin/env python3
"""fetch_and_summarize.py 오프라인 테스트. 레포 루트에서: python3 scripts/test_fetch_and_summarize.py

scripts/ 와 데이터 파일을 임시 폴더에 복사해 그쪽 모듈을 불러오므로 실제 feed.xml/items.json/seen.json 은 바뀌지 않는다.
http_get / summarize 는 가짜로 바꾼다.
"""
import importlib.util, json, os, shutil, tempfile, unittest
import unittest.mock
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

RSS_A = """<?xml version="1.0"?><rss version="2.0"><channel><title>A</title>
<item><title>A1</title><link>https://note.com/u/n/a1</link><pubDate>Wed, 28 Oct 2026 10:00:00 +0900</pubDate><description>{body}</description></item>
<item><title>A2</title><link>https://note.com/u/n/a2</link><pubDate>Wed, 28 Oct 2026 09:00:00 +0900</pubDate><description>{body}</description></item>
<item><title>Old</title><link>https://note.com/u/n/old</link><pubDate>Wed, 28 Oct 2026 08:00:00 +0900</pubDate><description>{body}</description></item>
</channel></rss>""".format(body="x" * 60)

ATOM_B = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>B</title>
<entry><title>B1</title><link rel="self" href="https://example.org/self"/><link rel="alternate" href="https://example.org/b1"/>
<published>2026-10-28T12:00:00Z</published><content>{body}</content></entry>
<entry><title>Dup</title><link href="https://note.com/u/n/a1"/><updated>2026-10-28T11:00:00Z</updated><summary>{body}</summary></entry>
</feed>""".format(body="y" * 60)

FEED_A, FEED_B = "https://a.test/rss", "https://b.test/atom"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(REPO / "scripts", self.tmp / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        for n in ("feed.xml", "items.json", "seen.json"):
            shutil.copy(REPO / n, self.tmp / n)
        # 테스트용 상태: note 출처 이름은 items.json 에서 오고, old 링크는 이미 처리됨
        items = json.loads((self.tmp / "items.json").read_text(encoding="utf-8"))
        items.append({"source": "note", "link": "https://note.com/u/n/old", "pubDate": "Wed, 28 Oct 2026 08:00:00 +0900",
                      "title_orig": "Old", "title_ko": "옛글", "summary_ko": "옛 요약"})
        (self.tmp / "items.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        seen = json.loads((self.tmp / "seen.json").read_text(encoding="utf-8"))
        seen["https://note.com/u/n/old"] = "2026-10-29"
        (self.tmp / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        (self.tmp / "feeds.txt").write_text(f"# 주석\n{FEED_A}\n{FEED_B}\n#off https://off.test/rss\n", encoding="utf-8")

        spec = importlib.util.spec_from_file_location("fas_under_test", self.tmp / "scripts" / "fetch_and_summarize.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.assertEqual(self.m.ROOT, self.tmp)
        self.pages = {FEED_A: RSS_A, FEED_B: ATOM_B}
        self.summarized = []
        self.m.http_get = lambda url, timeout=30: self._get(url)
        self.real_summarize = self.m.summarize
        self.m.summarize = self._summarize
        self.out = self.tmp / "new_items.json"
        self.env = {"NEW_ITEMS_PATH": str(self.out)}
        for k in ("GITHUB_STEP_SUMMARY",):
            os.environ.pop(k, None)
        patcher = unittest.mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, url):
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        return v.encode("utf-8")

    def _summarize(self, item):
        self.summarized.append(item["link"])
        return "한글 " + item["title"], "요약 " + item["title"]

    def run_main(self):
        self.m.main()
        return json.loads((self.tmp / "items.json").read_text(encoding="utf-8"))


class ParseTest(Base):
    def test_rss(self):
        got = self.m.parse_feed(RSS_A.encode())
        self.assertEqual([g["link"] for g in got], ["https://note.com/u/n/a1", "https://note.com/u/n/a2", "https://note.com/u/n/old"])
        self.assertEqual(got[0]["title"], "A1")
        self.assertTrue(got[0]["pubDate"].startswith("Wed, 28 Oct 2026"))

    def test_atom_alternate_link(self):
        got = self.m.parse_feed(ATOM_B.encode())
        self.assertEqual(got[0]["link"], "https://example.org/b1")
        self.assertEqual(got[0]["pubDate"], "2026-10-28T12:00:00Z")
        self.assertEqual(got[1]["link"], "https://note.com/u/n/a1")


class SummarizeTest(Base):
    WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet".split()
    BODY = " ".join(f"Sentence about rocket engines and {w} launches number{i} today." for i, w in enumerate(WORDS))

    def test_extract_picks_n_sentences_and_caps(self):
        s = self.m.extract_summary("rocket", self.BODY, 2, 500)
        self.assertEqual(s.count("Sentence about"), 2)
        self.assertLessEqual(len(self.m.extract_summary("x", self.BODY, 5, 100)), 100)
        self.assertEqual(self.m.extract_summary("t", "짧음", 3, 50), "짧음")

    def test_summarize_translates_title_and_summary(self):
        seen = []
        self.m.translate = lambda x, *a: seen.append(x) or "KO:" + x[:5]
        t, s = self.real_summarize({"title": "Rocket &amp; Co", "body": self.BODY})
        self.assertTrue(t.startswith("KO:Rocke") and s.startswith("KO:Sente"))
        self.assertEqual(seen[0], "Rocket & Co")

    def test_summarize_without_body_translates_title_only(self):
        self.m.translate = lambda x, *a: "KO:" + x
        t, s = self.real_summarize({"title": "Hi", "body": "짧"})
        self.assertEqual(t, "KO:Hi")
        self.assertIn("제목만", s)

    def test_translate_skips_korean_and_parses_response(self):
        self.assertEqual(self.m.translate("안녕하세요 반갑습니다"), "안녕하세요 반갑습니다")
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def read(s, *a): return json.dumps([[["안녕 ", "Hello ", None], ["세상", "world", None]], None, "en"]).encode()
        with unittest.mock.patch("urllib.request.urlopen", return_value=R()), unittest.mock.patch("time.sleep"):
            self.assertEqual(self.m.translate("Hello world"), "안녕 세상")

    def test_japanese_short_sentences_are_summarized(self):
        ja = "今日は映画を観てきました。とても良い作品でした。主演の演技が素晴らしかったです。次はもう一度観たいです。"
        s = self.m.extract_summary("映画の感想", ja, 2, 500)
        self.assertEqual(s.count("。"), 2)  # 20자 미만 문장을 버려 전체가 그대로 나오던 문제

    def test_junk_removed_and_decimal_not_split(self):
        text = "Version v1.2 is out with 3.5x faster builds. #news #tech Subscribe to our newsletter! The post X appeared first on Y."
        self.assertEqual(self.m.split_sentences(text), ["Version v1.2 is out with 3.5x faster builds."])

    def test_short_excerpt_is_translated_not_dropped(self):
        self.m.translate = lambda x, *a: "KO:" + x
        t, s = self.real_summarize({"title": "Hi", "body": "Short note"})
        self.assertEqual(s, "KO:Short note")

    def test_translate_retries_then_succeeds(self):
        calls = []
        def gtx(text):
            calls.append(text)
            if len(calls) < 3:
                raise self.m.urllib.error.HTTPError("u", 429, "slow", {}, None)
            return "OK"
        self.m.gtx = gtx
        with unittest.mock.patch("time.sleep") as sl:
            self.assertEqual(self.m.translate("hello"), "OK")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list][:2], list(self.m.BACKOFF))

    def test_translate_fatal_error_not_retried_and_exhaustion_raises(self):
        n = []
        def bad(text):
            n.append(1)
            raise self.m.urllib.error.HTTPError("u", 400, "bad", {}, None)
        self.m.gtx = bad
        with unittest.mock.patch("time.sleep"), self.assertRaises(self.m.urllib.error.HTTPError):
            self.m.translate("hello")
        self.assertEqual(len(n), 1)
        self.m.gtx = lambda t: (_ for _ in ()).throw(TimeoutError("t"))
        with unittest.mock.patch("time.sleep"), self.assertRaises(TimeoutError):
            self.m.translate("hello again")

    def test_translate_uses_cache_and_version_key(self):
        calls = []
        self.m.gtx = lambda t: calls.append(t) or "KO"
        with unittest.mock.patch("time.sleep"):
            self.m.translate("same text")
            self.m.translate("same text")
            self.assertEqual(len(calls), 1)
            self.m.PROCESS_VERSION = "2"
            self.m.translate("same text")
        self.assertEqual(len(calls), 2)

    def test_check_item_flags(self):
        self.assertEqual(self.m.check_item("t", "Sales rose 12.5% to 3,400", "t", "매출이 늘었다"), ["요약 숫자 누락 ['12.5', '3,400']"])
        self.assertEqual(self.m.check_item("t", "売上は１２％増", "t", "매출 12% 증가"), [])
        self.assertIn("요약 비어 있음", self.m.check_item("t", "x y z", "t", " "))


class MainTest(Base):
    def test_seen_excluded_and_dedupe_and_source_and_feed(self):
        items = self.run_main()
        # old 는 seen 이라 제외, a1 은 두 피드에 있어도 한 번만 요약
        self.assertEqual(sorted(self.summarized), ["https://example.org/b1", "https://note.com/u/n/a1", "https://note.com/u/n/a2"])
        self.assertEqual(self.summarized.count("https://note.com/u/n/a1"), 1)
        by_link = {i["link"]: i for i in items}
        self.assertEqual(by_link["https://note.com/u/n/a1"]["source"], "note")   # items.json 의 이름 재사용
        self.assertEqual(by_link["https://example.org/b1"]["source"], "Example")  # 처음 보는 호스트는 추측
        self.assertEqual(by_link["https://note.com/u/n/a1"]["title_ko"], "한글 A1")
        saved = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 3)
        ET.parse(self.tmp / "feed.xml")
        self.assertIn("https://note.com/u/n/a1", json.loads((self.tmp / "seen.json").read_text(encoding="utf-8")))

    def test_max_new_cap_keeps_newest(self):
        self.m.MAX_NEW = 2
        self.run_main()
        self.assertEqual(self.summarized, ["https://example.org/b1", "https://note.com/u/n/a1"])

    def test_partial_feed_failure_is_warning(self):
        self.pages[FEED_B] = OSError("boom")
        self.run_main()
        self.assertEqual(sorted(self.summarized), ["https://note.com/u/n/a1", "https://note.com/u/n/a2"])

    def test_all_feeds_fail_exits(self):
        self.pages[FEED_A] = OSError("x")
        self.pages[FEED_B] = OSError("y")
        with self.assertRaises(SystemExit):
            self.m.main()

    def test_summary_failure_skipped_and_step_summary(self):
        def flaky(item):
            if item["link"].endswith("a2"):
                raise ValueError("bad json")
            return self._summarize(item)
        self.m.summarize = flaky
        self.pages[FEED_B] = OSError("boom")
        sp = self.tmp / "step.md"
        with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(sp)}):
            items = self.run_main()
        self.assertNotIn("https://note.com/u/n/a2", [i["link"] for i in items])
        text = sp.read_text(encoding="utf-8")
        self.assertIn(FEED_B, text)
        self.assertIn("boom", text)
        self.assertIn("요약 실패 1건", text)
        self.assertIn("신규 2건", text)

    def test_item_failure_falls_back_then_retranslates(self):
        def flaky(item):
            if item["link"].endswith("a1"):
                raise ValueError("boom")
            return self._summarize(item)
        self.m.summarize = flaky
        self.pages[FEED_B] = OSError("boom")
        items = self.run_main()
        a1 = next(i for i in items if i["link"].endswith("a1"))
        self.assertTrue(a1["untranslated"])
        self.assertEqual((a1["attempts"], a1["title_ko"]), (1, "A1"))
        self.assertIn("자동 번역에 실패", (self.tmp / "feed.xml").read_text(encoding="utf-8"))
        self.m.summarize = self._summarize
        items = self.run_main()
        a1 = next(i for i in items if i["link"].endswith("a1"))
        self.assertNotIn("untranslated", a1)
        self.assertEqual(a1["title_ko"], "한글 A1")
        self.assertEqual(len([i for i in items if i["link"].endswith("a1")]), 1)

    def test_attempts_exhausted_not_retried(self):
        def flaky(item):
            if item["link"].endswith("a1"):
                raise ValueError("boom")
            return self._summarize(item)
        self.m.summarize = flaky
        self.pages[FEED_B] = OSError("boom")
        self.run_main()
        ip = self.tmp / "items.json"
        items = json.loads(ip.read_text(encoding="utf-8"))
        for i in items:
            if i.get("untranslated"):
                i["attempts"] = self.m.MAX_ATTEMPTS
        ip.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        calls = []
        self.m.summarize = lambda item: calls.append(item["link"]) or self._summarize(item)
        self.run_main()
        self.assertNotIn("https://note.com/u/n/a1", calls)

    def test_breaker_stops_and_publishes_nothing_on_outage(self):
        self.m.BREAKER = 2
        def down(item):
            self.summarized.append(item["link"])
            raise OSError("down")
        self.m.summarize = down
        items = self.run_main()
        self.assertEqual(len(self.summarized), 2)
        self.assertFalse(any(i["link"] in ("https://note.com/u/n/a1", "https://note.com/u/n/a2", "https://example.org/b1") for i in items))
        self.assertNotIn("https://note.com/u/n/a1", json.loads((self.tmp / "seen.json").read_text(encoding="utf-8")))

    def test_dry_run_changes_nothing(self):
        before = {n: (self.tmp / n).read_bytes() for n in ("feed.xml", "items.json", "seen.json")}
        out = self.tmp / "dry.json"
        with unittest.mock.patch.dict(os.environ, {"DRY_RUN_OUT": str(out)}), unittest.mock.patch("sys.argv", ["x", "--dry-run"]):
            self.m.main()
        for n, b in before.items():
            self.assertEqual((self.tmp / n).read_bytes(), b)
        self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), 4)  # 이미 처리한 old 글도 포함
        self.assertFalse((self.tmp / "translations.json").exists())

    def test_real_data_untouched(self):
        before = {n: (REPO / n).read_bytes() for n in ("feed.xml", "items.json", "seen.json")}
        self.run_main()
        for n, b in before.items():
            self.assertEqual((REPO / n).read_bytes(), b)


if __name__ == "__main__":
    unittest.main()
