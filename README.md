# The AI Bill — AI가 사회와 환경에 떠넘기는 비용

AI가 남기는 청구서(데이터센터 전력·용수, 딥페이크·차별·노동 피해, 규제 집행)를 매일 아침
수집 → 선별 → 요약 → 자동 검수 → 이메일 발행까지 한 번에 돌리는 LangGraph 파이프라인.

- 수집: RSS 12개 소스 (후보 35개 실측, 근거는 [docs/source_audit_ai.md](docs/source_audit_ai.md))
- 선별: 규칙 필터 → 예선(제목·요약, 8건 배치) → 본문 확보 → 본선(본문, 4건 배치, 5차원 가중)
- 요약: 사실 + 독자 관점 인사이트 + 실무 체크 + 검증용 claims
- 검수: 인용문 대조 · 숫자 대조 · LLM 근거성 판정 3중, 실패 시 재생성 → 폐기 → 대기 후보 교체
- 발행: **공개 웹 아카이브** https://c-lake864.github.io/the-ai-bill/ + SMTP 이메일 + `out/` 로컬 사본

설계 판단과 실행 기록은 [REPORT.md](REPORT.md) 를 보세요.
이전 주제(환경·기후)의 실측 자료도 [docs/source_audit.md](docs/source_audit.md) 에 남아 있습니다.

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env     # 키와 SMTP 정보를 채운다
python run.py --dry-run  # 메일 없이 전체 파이프라인 확인
python run.py                  # 실제 발행 (메일 + docs/ 아카이브 파일 생성)
python tools/publish_site.py   # 공개 페이지에 반영 (docs/ 커밋·푸시)
```

보조 스크립트

```bash
python tools/setup_env.py          # .env 의 SMTP 값을 대화형으로 채운다 (비밀번호는 화면에 안 보임)
python tools/smtp_check.py         # 메일 서버 접속·로그인만 확인 (발송 없음)
python tools/source_probe.py --candidates tools/source_candidates_ai.yaml --out ai
python tools/topic_feasibility.py  # 주제 후보의 공급량 타당성 측정
python tools/verify_selftest.py    # 검수 노드가 할루시네이션을 잡는지 확인
python tools/batch_experiment.py   # 예선 배치 크기 실측 실험
python run.py --graph              # 그래프 구조(mermaid) 출력
```

## 파일 구조

```
graph.py                     LangGraph 워크플로우 (노드 · 상태 · 라우팅)
run.py                       실행 스크립트
audience.yaml                타깃 독자 · 중요도 기준 · 제외 조건 · 검수 기준
sources.yaml                 채택 소스와 채택/탈락 사유
nl/collect.py                RSS 수집 + 규칙 필터 + 중복 제거 (+ 미사용 TopicGate)
nl/extract.py                기사 본문 추출 (trafilatura -> BeautifulSoup 폴백)
nl/screen.py                 예선 · 본선 채점 · ref 매핑 · 소스/사건 중복 상한
nl/summarize.py              요약 + 인사이트 + 검증용 claims 생성
nl/verify.py                 3중 자동 검수
nl/render.py                 이메일 HTML / 아카이브 Markdown
nl/publish.py                SMTP 발송 + 로컬 저장
nl/site.py                   공개 웹 아카이브(GitHub Pages) 생성
tools/source_probe.py        소스 후보 실측 (주제 키워드는 후보 yaml 에서 읽는다)
tools/topic_feasibility.py   주제 전환 전 공급량 측정
tools/verify_selftest.py     검수 노드 자체 테스트
tools/batch_experiment.py    예선 배치 크기 실측 실험
tools/setup_env.py           .env SMTP 값 대화형 입력
tools/smtp_check.py          SMTP 자격증명 확인
tools/publish_site.py        docs/ 커밋·푸시 (공개 발행)
store/metrics.jsonl          실행별 지표 누적
store/runs/<run_id>/         단계별 상세 덤프 (01~06, run.log)
docs/index.html              공개 아카이브 목차 (GitHub Pages)
docs/issues/YYYY-MM-DD.html  공개 아카이브 호별 페이지
docs/sample_run/             근거용 샘플 실행 1회분
out/YYYY-MM-DD.html|md       발행물 아카이브
```

## 주제를 바꾸려면

코드는 건드리지 않습니다. `audience.yaml`(독자·기준)과 `sources.yaml`(소스)만 교체하면 됩니다.
실제로 이 저장소는 환경·기후 주제로 완주한 뒤 AI 주제로 전환했습니다.

## 환경변수

`.env.example` 참고. `OPENAI_API_KEY` 는 필수이고, SMTP 값이 없으면 메일 전송은
`skipped_no_credentials` 로 기록되고 로컬 사본만 남습니다(파이프라인은 중단되지 않습니다).
