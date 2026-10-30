# rss-ko

외국어 RSS 피드를 한글 제목·요약으로 통합한 피드 (하루 6회 자동 갱신: 00/04/08/12/16/20시 KST, GitHub Actions `update-feed.yml`).

- 구독 주소: https://hahnjunhee.github.io/rss-ko/feed.xml
- 원본 피드 목록: `feeds.txt` (한 줄에 하나)
- `seen.json`: 처리 이력 / `items.json`: 피드 데이터 / `scripts/update_feed.py`: 빌더

API 키가 필요 없다. 피드 본문에서 핵심 문장 3개를 고르고(추출 요약) Google 번역으로 한글화한다. 번역은 비공식 엔드포인트라 가끔 실패할 수 있고, 실패한 글은 다음 실행에서 재시도한다. 수동 실행: Actions → Update feed → Run workflow.
