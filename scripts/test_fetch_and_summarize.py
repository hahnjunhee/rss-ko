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

    def test_fulltext_summary_skips_title_echo_and_list_markers(self):
        title = "全部言葉でできた世界に，色がついていく"
        body = "\n".join([
            "「全部言葉でできた世界に，色がついていく」",
            "全部言葉でできた世界に，どんどん色がついていく感じがする．",
            "あれを言葉でやっている．",
            "◆小室さんの来訪",
            "）今日は，小室哲哉さんが横浜のスタジオに遊びに来てくれた夜の話．",
            "27日は夕方にTBSで収録があって，そのあと横浜のランドマークプラザの5階に向かった．",
            "小室さんは音楽の話から，言葉が先にあって色が後からついてくる感覚について語ってくれた．"])
        s = self.m.extract_summary(title, body, 3, 500)
        self.assertNotIn("どんどん色が", s)      # 제목 되풀이
        self.assertNotIn("あれを言葉で", s)      # 짧은 문장
        self.assertNotIn("◆", s)                # 소제목
        self.assertNotIn("）", s)                # 앞에 남은 닫는 기호
        self.assertIn("小室哲哉", s)

    def test_cut_at_sentence_boundary(self):
        cut = self.m.cut_at_sentence
        self.assertEqual(cut("짧은 글입니다.", 100), "짧은 글입니다.")
        text = "First sentence is here. Second sentence is here. Third sentence that gets cut in the mid"
        self.assertEqual(cut(text, 60), "First sentence is here. Second sentence is here.")
        self.assertEqual(cut("x" * 100, 50), "x" * 50)  # 문장 끝이 없으면 그냥 자른다
        self.assertEqual(cut("가나다。라마바。사아자차카타파하", 12), "가나다。라마바。")

    def test_long_rss_body_does_not_end_with_a_fragment(self):
        body = " ".join(f"This is sentence number {i} of a very long feed body." for i in range(400))
        s = self.m.source_summary({"title": "T", "body": body})
        self.assertTrue(s.endswith("."))
        self.assertNotIn("larg", s)

    def test_numbered_headings_are_not_picked(self):
        text = "\n".join(["1.マイページへのログイン方法", "このマイページは、単なる求人検索ページではありません。", "2) Getting started",
                          "マイページに登録すると、希望条件に合った求人がメールで届きます。", "登録は無料で、スマートフォンからも手続きできます。"])
        s = self.m.extract_summary("マイページ活用術", text, 3, 500)
        self.assertNotIn("ログイン方法", s)
        self.assertNotIn("Getting started", s)
        self.assertTrue(self.m.heading_like("1.マイページへのログイン方法"))
        self.assertFalse(self.m.heading_like("2026年は売上が伸びました。"))
        self.assertFalse(self.m.heading_like("1. この機能は便利です。"))  # 문장부호로 끝나면 소제목이 아니다

    def test_only_leading_sentences_are_candidates(self):
        lead = [f"Lead sentence number {i} talks about alpha{i} topic here today." for i in range(12)]
        late = ["Late sentence mentions the title words rocket engines rocket engines rocket engines rocket engines 99."]
        s = self.m.extract_summary("rocket engines", " ".join(lead + late), 3, 500)
        self.assertNotIn("Late sentence", s)

    def test_list_items_are_not_picked_unless_nothing_else(self):
        only = "◆ 첫째 항목입니다\n◆ 둘째 항목입니다"
        self.assertTrue(self.m.extract_summary("제목", only, 3, 500))  # 후보가 전부 제외되면 모두 후보로 둔다
        mixed = "◆ 첫째 항목입니다\n본문 문장이 여기에 있습니다. 두 번째 본문 문장입니다. 세 번째 본문 문장입니다. 네 번째 본문 문장입니다."
        self.assertNotIn("◆", self.m.extract_summary("제목", mixed, 3, 500))

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

    def test_translate_honors_retry_after_with_cap(self):
        seq = iter(["7", "999"])
        def gtx(text):
            try:
                raise self.m.urllib.error.HTTPError("u", 429, "slow", {"Retry-After": next(seq)}, None)
            except StopIteration:
                return "OK"
        self.m.gtx = gtx
        with unittest.mock.patch("time.sleep") as sl:
            self.assertEqual(self.m.translate("hello"), "OK")
        self.assertEqual([c.args[0] for c in sl.call_args_list][:2], [7, self.m.MAX_WAIT])

    def test_gtx_falls_back_to_next_engine(self):
        def down(text):
            raise self.m.urllib.error.HTTPError("u", 429, "slow", {}, None)
        self.m.ENGINES = [down, lambda text: " 안녕 "]
        self.m.ENGINES[1].__name__ = "second"
        self.assertEqual(self.m.gtx("hello"), "안녕")
        self.assertEqual(dict(self.m.ENGINE_STATS), {"second": 1})

    def test_gtx_raises_first_engine_error_and_rejects_empty(self):
        def e429(text):
            raise self.m.urllib.error.HTTPError("u", 429, "slow", {}, None)
        def e403(text):
            raise self.m.urllib.error.HTTPError("u", 403, "no", {}, None)
        self.m.ENGINES = [e429, e403, lambda text: "  "]
        with self.assertRaises(self.m.urllib.error.HTTPError) as c:
            self.m.gtx("hello")
        self.assertEqual(c.exception.code, 429)  # 재시도 여부는 첫 엔진 오류로 판단

    def test_failing_engine_skipped_after_streak_and_reported(self):
        calls = []
        def bad(text):
            calls.append(1)
            raise self.m.urllib.error.HTTPError("u", 429, "slow", {}, None)
        def good(text):
            return "OK"
        self.m.ENGINES = [bad, good]
        for _ in range(5):
            self.assertEqual(self.m.gtx("x"), "OK")
        self.assertEqual(len(calls), self.m.SKIP_AFTER)  # 연속 3번 실패 뒤로는 호출하지 않음
        self.assertEqual(self.m.ENGINE_FAILS["bad"], 3)
        self.assertIn("429", self.m.ENGINE_LAST_ERR["bad"])
        sp = self.tmp / "step.md"
        with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(sp)}), unittest.mock.patch("builtins.print") as pr:
            self.m.report_engines()
        text = sp.read_text(encoding="utf-8")
        self.assertIn("good", text)
        self.assertIn("bad: 3회", text)
        self.assertTrue(pr.called)

    def test_all_engines_skipped_are_retried(self):
        n = []
        def flaky(text):
            n.append(1)
            if len(n) <= 3:
                raise ValueError("down")
            return "OK"
        self.m.ENGINES = [flaky]
        for _ in range(3):
            with self.assertRaises(ValueError):
                self.m.gtx("x")
        self.assertEqual(self.m.gtx("x"), "OK")  # 모두 건너뛰는 상황이면 전부 다시 시도

    def test_report_samples_prints_and_writes_summary(self):
        items = [{"link": f"l{i}", "source": "S", "title_orig": f"T{i}", "title_ko": f"제목{i}", "summary_ko": f"요약{i}"} for i in range(25)]
        by_link = {f"l{i}": {"title": f"T{i}", "body": "A sentence for the test body here."} for i in range(25)}
        sp = self.tmp / "step.md"
        with unittest.mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(sp)}), unittest.mock.patch("builtins.print") as pr:
            self.m.report_samples(items, by_link)
        text = sp.read_text(encoding="utf-8")
        self.assertEqual(text.count("### "), 10)
        self.assertIn("- 원문 요약: A sentence for the test body here.", text)
        self.assertIn("::group::", pr.call_args[0][0])

    def test_plain_adds_space_between_blocks_and_strips_zero_width(self):
        self.assertEqual(self.m.plain("<p>them.</p><h2>One Game</h2>x\u200by<br>z"), "them. xy z")  # 소제목(h1~h6) 텍스트는 뺀다

    def test_read_more_and_fullwidth_period(self):
        text = "落合陽一です．今日は夜の話．1か月くらい，これしかやっていない．続きをみる"
        self.assertEqual(self.m.split_sentences(text), ["落合陽一です．", "今日は夜の話．", "1か月くらい，これしかやっていない．"])
        self.assertEqual(self.m.split_sentences("倍率は３．５倍になった。"), ["倍率は３．５倍になった。"])  # 숫자 사이 전각 점은 나누지 않음
        self.assertEqual(self.m.split_sentences("Great post. 계속 보기"), ["Great post."])

    def test_parse_chrome_joins_all_segments(self):
        pc = self.m.parse_chrome
        self.assertEqual(pc([["「우리는 우주인」", "a"], ["남자아이의 부모 입장에서 보니", "b"]]), "「우리는 우주인」남자아이의 부모 입장에서 보니")
        self.assertEqual(pc(["안녕", "세상"]), "안녕세상")
        self.assertEqual(pc(["안녕", "ja"]), "안녕")  # 끝의 언어 코드는 제외

    def test_too_short_detection(self):
        ts = self.m.too_short
        self.assertTrue(ts("『我々は宇宙人』 男児の親目線で観たら、思わぬところで抉られた話", "「우리는 우주인」"))
        self.assertFalse(ts("『我々は宇宙人』 男児の親目線で観たら", "『우리는 우주인』 남자아이의 부모 눈으로 보면"))
        self.assertFalse(ts("How to Clean Up Old Screen Recordings Without Losing Quality", "품질 저하 없이 오래된 화면 녹화를 정리하는 방법"))
        self.assertFalse(ts("Hi", ""))  # 너무 짧은 원문은 판단하지 않음

    def test_gtx_prefers_longer_when_first_is_truncated(self):
        src = "『我々は宇宙人』 男児の親目線で観たら、思わぬところで抉られた話"
        self.m.ENGINES = [lambda t: "「우리는 우주인」", lambda t: "『우리는 우주인』 남자아이 부모의 눈으로 보니 뜻밖의 곳에서 마음이 아팠던 이야기"]
        self.m.ENGINES[0].__name__, self.m.ENGINES[1].__name__ = "cut", "full"
        self.assertTrue(self.m.gtx(src).endswith("이야기"))
        self.assertEqual(self.m.ENGINE_SHORT["cut"], 1)
        self.m.ENGINES = [lambda t: "「우리는 우주인」"]  # 다른 엔진도 짧고 나눌 곳도 없으면 짧은 쪽이라도 쓴다
        self.m.ENGINES[0].__name__ = "only"
        self.assertEqual(self.m.gtx("男児の親目線で観たら思わぬところで抉られた話"), "「우리는 우주인」")

    def test_gtx_splits_into_parts_when_all_engines_truncate(self):
        src = "『我々は宇宙人』 男児の親目線で観たら、思わぬところで抉られた話"
        calls = []
        def eng(t):
            calls.append(t)
            if t == src or t.startswith('"'):
                return "「우리는 우주인」"  # 전체를 보내면(괄호를 따옴표로 바꿔도) 뒤쪽이 잘린다
            return {"『我々は宇宙人』": "『우리는 우주인』", "男児の親目線で観たら、思わぬところで抉られた話": "남자아이 부모의 눈으로 보니 뜻밖의 곳에서 마음이 아팠던 이야기"}[t]
        eng.__name__ = "e"
        self.m.ENGINES = [eng]
        out = self.m.gtx(src)
        self.assertEqual(out, "『우리는 우주인』 남자아이 부모의 눈으로 보니 뜻밖의 곳에서 마음이 아팠던 이야기")
        self.assertEqual(self.m.ENGINE_FIXED["split"], 1)
        self.assertEqual(len(calls), 4)  # 전체 1회 + 괄호 변환 1회 + 조각 2회

    def test_gtx_retries_with_brackets_normalized_before_splitting(self):
        src = "『我々は宇宙人』 男児の親目線で観たら、思わぬところで抉られた話"
        calls = []
        def eng(t):
            calls.append(t)
            if "『" in t:
                return "「우리는 우주인」"  # 괄호가 있으면 뒤가 잘린다
            return '"우리는 우주인" 남자아이 부모의 눈으로 보니 뜻밖의 곳에서 마음이 아팠던 이야기'
        eng.__name__ = "e"
        self.m.ENGINES = [eng]
        self.assertEqual(self.m.gtx(src), "『우리는 우주인』 남자아이 부모의 눈으로 보니 뜻밖의 곳에서 마음이 아팠던 이야기")
        self.assertEqual(dict(self.m.ENGINE_FIXED), {"bracket": 1})
        self.assertEqual(len(calls), 2)  # 조각별 번역까지 가지 않는다

    def test_bracket_helpers(self):
        norm, seq = self.m.normalize_brackets("『A』と「B」")
        self.assertEqual((norm, seq), ('"A"と"B"', ["『", "』", "「", "」"]))
        self.assertEqual(self.m.restore_brackets('"가"와 "나"', seq), "『가』와 「나」")
        self.assertEqual(self.m.restore_brackets('"가" 나', seq), '"가" 나')  # 개수가 다르면 그대로

    def test_split_parts_only_at_brackets_and_punctuation(self):
        sp = self.m.split_parts
        self.assertEqual(sp("『我々は宇宙人』 男児の親目線で観たら"), ["『我々は宇宙人』", "男児の親目線で観たら"])
        self.assertEqual(sp("How to Clean Up Old Videos Without Losing Quality"), [])  # 영어 단어 단위로는 나누지 않음
        self.assertEqual(sp("First sentence here. Second one follows."), ["First sentence here.", "Second one follows."])
        self.assertEqual(sp("a. b. c. d. e. f. g."), [])  # 7조각 이상이면 나누지 않음

    def test_summary_prefers_informative_lead_over_greeting(self):
        ja = ("落合陽一です．今日は，小室哲哉さんが横浜のnull²ⁿ（ヌルヌルネクサス）に遊びに来てくれた夜の話．"
              "27日は夕方にTBSで収録があって，そのあと横浜のランドマークプラザの5階に向かった．"
              "お客さんがもう誰もいない館で小室さんを迎えて，部屋を見てもらってから，ワインを開けて1時間あまり喋った．"
              "1か月くらい，ほぼこれしかやっていない．続きをみる")
        s = self.m.extract_summary("全部言葉でできた世界に，色がついていく", ja, 3, 500)
        self.assertIn("小室哲哉", s)
        self.assertNotIn("落合陽一です", s)
        self.assertNotIn("続きをみる", s)

    def test_gtx_strips_zero_width(self):
        self.m.ENGINES = [lambda t: "용도를\u200b\u200b변경"]
        self.m.ENGINES[0].__name__ = "zw"
        self.assertEqual(self.m.gtx("repurpose it"), "용도를변경")

    def test_check_item_flags_short_title(self):
        self.assertIn("제목 번역이 원문보다 비정상적으로 짧음",
                      self.m.check_item("『我々は宇宙人』 男児の親目線で観たら、思わぬところで抉られた話", "", "「우리는 우주인」", ""))

    def test_parse_chrome_and_mobile_shapes(self):
        pc = self.m.parse_chrome
        self.assertEqual(pc({"sentences": [{"trans": "안녕 "}, {"trans": "세상"}]}), "안녕 세상")
        self.assertEqual(pc(["안녕"]), "안녕")
        self.assertEqual(pc([["안녕", "hello"]]), "안녕")
        self.assertEqual(pc({}), "")
        self.assertEqual(self.m.parse_mobile('<div class="result-container">안녕 &amp; 세상</div>'), "안녕 & 세상")
        self.assertEqual(self.m.parse_mobile("<html></html>"), "")

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


NOTE_HTML = """<html><head><title>t</title></head><body><nav>メニュー ログイン</nav><article><h1>映画の感想</h1>
<p>今日は映画を観てきました。主演の演技が素晴らしく、音楽も印象に残りました。帰り道にずっとサントラを聴いていました。</p>
<p>特に終盤の長回しのシーンは息をのむほどで、劇場全体が静まり返っていたのを覚えています。もう一度観に行きたいです。</p>
</article><div class="comments"><p>コメント: 最高でした!</p></div><footer>(c) site</footer></body></html>"""


class FulltextTest(Base):
    EXCERPT = "大学時代、アルバイトをしていた。続きをみる"

    def setUp(self):
        super().setUp()
        (self.tmp / "fulltext.txt").write_text("# 원문 수집 허용 호스트\nnote.com  # 일본어 블로그\n", encoding="utf-8")
        self.item = {"title": "T", "link": "https://note.com/u/n/x1", "body": self.EXCERPT}
        self.long = "\n".join(["今日は映画を観てきました。主演の演技が素晴らしかったです。" * 3, "終盤の長回しのシーンは息をのむほどでした。" * 3])
        self.m.robots_allows = lambda url: True
        self.m.fetch_html = lambda url: "<html/>"
        self.m.extract_text = lambda page: self.long

    def test_wants_fulltext_only_for_allowed_truncated_posts(self):
        w = self.m.wants_fulltext
        self.assertTrue(w(self.item, self.EXCERPT))
        self.assertFalse(w({"link": "https://other.com/a"}, self.EXCERPT))  # 허용 호스트가 아님
        self.assertFalse(w(self.item, "x" * 400))  # 잘림 표시도 없고 충분히 길다
        self.assertTrue(w(self.item, "짧음"))  # 짧으면 가져온다
        self.assertTrue(w({"link": "https://www.note.com/a"}, self.EXCERPT))  # 하위 도메인
        self.assertFalse(w({"link": "https://evilnote.com/a"}, self.EXCERPT))
        self.assertFalse(w({"link": "ftp://note.com/a"}, self.EXCERPT))

    def test_feature_off_without_hosts_file(self):
        (self.tmp / "fulltext.txt").unlink()
        self.assertEqual(self.m.get_fulltext(self.item), "")
        self.assertEqual(dict(self.m.FULLTEXT_STATS), {})

    def test_success_uses_fulltext_for_summary(self):
        self.item["fulltext"] = self.m.get_fulltext(self.item)
        self.assertTrue(self.item["fulltext"].startswith("今日は映画"))
        self.assertEqual(self.m.FULLTEXT_STATS["ok"], 1)
        self.assertIn("長回し", self.m.source_summary(self.item))
        self.assertNotIn("続きをみる", self.m.source_summary(self.item))

    def test_failures_fall_back_to_excerpt(self):
        self.m.robots_allows = lambda url: False
        self.assertEqual(self.m.get_fulltext(self.item), "")
        self.assertEqual(self.m.FULLTEXT_STATS["robots"], 1)
        self.m.robots_allows = lambda url: True
        def boom(url):
            raise self.m.urllib.error.HTTPError(url, 403, "no", {}, None)
        self.m.fetch_html = boom
        self.assertEqual(self.m.get_fulltext(self.item), "")
        self.assertEqual(self.m.FULLTEXT_STATS["fetch"], 1)
        self.assertIn("403", self.m.FULLTEXT_LAST_ERR["fetch"])
        self.assertTrue(self.m.source_summary(self.item))  # 발췌로 요약은 계속 가능

    def test_no_gain_and_empty_extraction(self):
        self.m.extract_text = lambda page: "短い本文"
        self.assertEqual(self.m.get_fulltext(self.item), "")
        self.m.extract_text = lambda page: ""
        self.assertEqual(self.m.get_fulltext(self.item), "")
        self.assertEqual(self.m.FULLTEXT_STATS["no_gain"], 2)

    def test_paywall_part_is_cut(self):
        pre = "無料で読める部分です。映画の感想を書きました。" * 5
        self.m.extract_text = lambda page: pre + "\nここから先は有料\n" + "有料部分の本文です。" * 20
        out = self.m.get_fulltext(self.item)
        self.assertNotIn("有料", out)
        self.assertEqual(self.m.FULLTEXT_STATS["paywall_cut"], 1)

    def test_per_run_limit(self):
        self.m.FULLTEXT_MAX = 1
        self.assertTrue(self.m.get_fulltext(self.item))
        self.assertEqual(self.m.get_fulltext(self.item), "")
        self.assertEqual(self.m.FULLTEXT_STATS["limit"], 1)

    def test_robots_rules(self):
        self.m.__dict__.pop("robots_allows")  # 설정에서 대체한 가짜를 걷어내고 실제 함수를 쓴다
        spec = importlib.util.spec_from_file_location("fresh", self.tmp / "scripts" / "fetch_and_summarize.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        fresh.fetch_robots = lambda host, scheme: (200, "User-agent: *\nDisallow: /private/\n")
        self.assertTrue(fresh.robots_allows("https://note.com/u/n/x1"))
        self.assertFalse(fresh.robots_allows("https://note.com/private/a"))
        fresh._ROBOTS.clear()
        fresh.fetch_robots = lambda host, scheme: (404, "")
        self.assertTrue(fresh.robots_allows("https://note.com/private/a"))  # robots.txt가 없으면 허용
        fresh._ROBOTS.clear()
        fresh.fetch_robots = lambda host, scheme: (503, "")
        self.assertFalse(fresh.robots_allows("https://note.com/u/n/x1"))  # 서버 오류면 보수적으로 불허
        fresh._ROBOTS.clear()
        fresh.fetch_robots = lambda host, scheme: (0, "")
        self.assertFalse(fresh.robots_allows("https://note.com/u/n/x1"))  # 네트워크 오류도 불허

    def test_newline_separates_headings_from_sentences(self):
        self.assertEqual(self.m.split_sentences("はじめに\n今日は映画を観てきました。"), ["はじめに", "今日は映画を観てきました。"])

    @unittest.skipUnless(importlib.util.find_spec("trafilatura"), "trafilatura 미설치")
    def test_real_trafilatura_extracts_body_without_comments(self):
        spec = importlib.util.spec_from_file_location("real", self.tmp / "scripts" / "fetch_and_summarize.py")
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        out = real.extract_text(NOTE_HTML)
        self.assertIn("長回し", out)
        self.assertNotIn("コメント", out)
        self.assertNotIn("メニュー", out)

    def test_main_summarizes_from_fulltext(self):
        self.m.__dict__.pop("summarize", None)
        self.m.summarize = self.real_summarize
        self.m.translate = lambda x, *a: "KO:" + x[:6]
        self.m.get_fulltext = lambda c: self.long if c["link"].endswith("a1") else ""
        self.run_main()
        s = json.loads((self.tmp / "items.json").read_text(encoding="utf-8"))
        a1 = next(i for i in s if i["link"].endswith("a1"))
        self.assertEqual(a1["summary_ko"], "KO:" + self.m.extract_summary("A1", self.long)[:6])


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
