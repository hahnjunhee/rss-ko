# rss-ko

외국어 RSS 피드를 한글 제목·요약으로 통합한 피드 (하루 6회 자동 갱신: 00/04/08/12/16/20시 KST, GitHub Actions `update-feed.yml`).

- 구독 주소: https://hahnjunhee.github.io/rss-ko/feed.xml
- 원본 피드 목록: `feeds.txt` (한 줄에 하나)
- `seen.json`: 처리 이력 / `items.json`: 피드 데이터 / `scripts/update_feed.py`: 빌더

API 키가 필요 없다. 피드 본문에서 핵심 문장 3개를 고르고(추출 요약) Google 번역으로 한글화한다. 번역은 비공식 엔드포인트라 가끔 실패할 수 있고, 실패한 글은 다음 실행에서 재시도한다. 수동 실행: Actions → Update feed → Run workflow.

## 번역 품질·안정성 처리 (`scripts/fetch_and_summarize.py`)
- **요약 선택:** 푸터·구독/공유 문구·해시태그를 제거하고, 단어 빈도·제목 일치·앞 문장·숫자 포함으로 문장 3개를 고른다. 일본어·중국어는 문자 2-gram으로 점수를 매긴다. 문장이 3개 이하이거나 발췌가 짧으면 줄이지 않고 그대로 번역한다.
- **재시도:** 429·5xx·타임아웃·빈 응답은 2번까지 5초·15초(429의 Retry-After가 있으면 그 값, 최대 60초) 대기 후 다시 시도한다. 호출 사이에는 1.5초 쉰다. 글이 연속 3건 실패하면 서비스 장애로 보고 그 실행의 나머지를 건너뛰며, 맨 뒤에서 연속 실패한 글은 게재하지 않고 다음 실행에서 다시 한다.
- **원문 임시 게재:** 같은 실행에서 다른 글은 번역에 성공했는데 한 글만 실패하면 그 글은 원문 제목·요약으로 올리고(`untranslated`), 다음 실행에서 최대 3번까지 다시 번역한다. 한 글만 계속 실패하고 그 실행에 성공한 글이 없으면 시도 횟수는 늘지 않는다.
- **번역 캐시:** `translations.json`에 `sha1(처리 버전 + 원문)` → 번역을 저장한다(최대 1000개). 요약·번역 규칙을 바꾸면 `PROCESS_VERSION`을 올린다. 과거 글을 자동으로 다시 번역하지는 않는다.
- **품질 비교(dry-run):** Actions → Update feed → Run workflow에서 `dry_run`을 켜면 이미 처리한 글까지 최대 30건을 요약·번역하되 피드·items·seen은 바꾸지 않고, 결과를 `dry-run-items` 아티팩트로 저장한다. 숫자 누락·빈 번역·한글 비율 낮음은 로그 경고로 표시한다. 로컬: `python scripts/fetch_and_summarize.py --dry-run`.
