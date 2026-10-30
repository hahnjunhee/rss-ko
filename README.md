# rss-ko

외국어 RSS 피드를 한글 제목·요약으로 통합한 피드 (하루 6회 자동 갱신: 00/04/08/12/16/20시 KST, GitHub Actions `update-feed.yml`).

- 구독 주소: https://hahnjunhee.github.io/rss-ko/feed.xml
- 원본 피드 목록: `feeds.txt` (한 줄에 하나)
- `seen.json`: 처리 이력 / `items.json`: 피드 데이터 / `scripts/update_feed.py`: 빌더

갱신에는 Settings → Secrets and variables → Actions 에 시크릿 `ANTHROPIC_API_KEY` 등록이 필요하다. 수동 실행: Actions → Update feed → Run workflow.
