# 오늘의 기후 브리핑 — 환경·기후 뉴스레터 에이전트

환경/기후 분야 뉴스를 매일 아침 수집 → 선별 → 요약 → 자동 검수 → 이메일 발행까지
한 번에 돌리는 LangGraph 파이프라인입니다.

- 수집: RSS 10개 소스 (실측으로 채택, 근거는 [docs/source_audit.md](docs/source_audit.md))
- 선별: 예선(제목·요약, 8건 배치) → 본선(본문, 4건 배치) 2단계
- 요약: 사실 요약 + 독자 관점 인사이트 + 실무 체크
- 검수: 인용문 대조 · 숫자 대조 · LLM 근거성 판정 3중, 실패 시 재생성 → 폐기 → 대기 후보 교체
- 발행: SMTP 이메일 + `out/` 에 HTML/Markdown 아카이브

자세한 설계 판단과 실행 기록은 [REPORT.md](REPORT.md) 를 보세요.

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env     # 키와 SMTP 정보를 채운다
python run.py --dry-run  # 메일 없이 전체 파이프라인 확인
python run.py            # 실제 발행
```

보조 스크립트

```bash
python tools/source_probe.py     # 소스 후보 실측 -> store/source_audit.json, docs/source_audit.md
python tools/verify_selftest.py  # 검수 노드가 할루시네이션을 잡는지 확인 -> store/verify_selftest.json
python run.py --graph            # 그래프 구조(mermaid) 출력
```

## 파일 구조

```
graph.py                  LangGraph 워크플로우 (노드 · 상태 · 라우팅)
run.py                    실행 스크립트
audience.yaml             타깃 독자 · 중요도 기준 · 제외 조건 · 검수 기준
sources.yaml              채택 소스와 채택/탈락 사유
nl/collect.py             RSS 수집 + 규칙 필터 + 중복 제거
nl/extract.py             기사 본문 추출 (trafilatura -> BeautifulSoup 폴백)
nl/screen.py              예선 · 본선 채점 · 소스 다양성 상한
nl/summarize.py           요약 + 인사이트 + 검증용 claims 생성
nl/verify.py              3중 자동 검수
nl/render.py              이메일 HTML / 아카이브 Markdown
nl/publish.py             SMTP 발송 + 로컬 저장
tools/source_probe.py     소스 후보 실측
tools/verify_selftest.py  검수 노드 자체 테스트
store/metrics.jsonl       실행별 지표 누적
store/runs/<run_id>/      단계별 상세 덤프 (01~06, run.log)
out/YYYY-MM-DD.html|md    발행물 아카이브
```

## 환경변수

`.env.example` 참고. `OPENAI_API_KEY` 는 필수이고, SMTP 값이 없으면 메일 전송은
`skipped_no_credentials` 로 기록되고 로컬 사본만 남습니다(파이프라인은 중단되지 않습니다).
