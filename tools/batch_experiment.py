"""예선 배치 크기를 실측으로 정하기 위한 실험.

같은 후보 집합을 batch_size 만 바꿔 여러 번 채점하고 다음을 비교한다.
  - 호출 수 / 토큰 / 소요시간
  - 응답 누락률 (배치가 커질수록 LLM 이 일부 idx 를 빼먹는다)
  - batch=4 결과 대비 상위 12건 집합 일치율(Jaccard)과 평균 점수 차이
  - 배치 내 위치별 평균 점수 (뒤쪽 기사가 체계적으로 낮으면 위치 편향)

결과: store/batch_experiment.json
"""
from __future__ import annotations

import copy
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nl.config import STORE, load_audience, load_env  # noqa: E402
from nl import screen as S  # noqa: E402
from nl.llm import USAGE  # noqa: E402

SIZES = [4, 8, 12, 16]


def load_items() -> list:
    runs = sorted((STORE / "runs").glob("*/01_collected.json"))
    if not runs:
        raise SystemExit("먼저 run.py 를 한 번 실행하세요.")
    return json.loads(runs[-1].read_text(encoding="utf-8"))["kept"]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    load_env()
    audience = load_audience()
    items = load_items()
    print(f"후보 {len(items)}건으로 배치 크기 {SIZES} 비교\n")

    results = {}
    for bs in SIZES:
        cfg = copy.deepcopy(audience)
        cfg["scoring"]["prescreen"]["batch_size"] = bs
        before = copy.deepcopy(USAGE.by_stage.get("prescreen", {"calls": 0, "prompt_tokens": 0,
                                                               "completion_tokens": 0}))
        t0 = time.time()
        scored = S.prescreen(items, cfg, lambda m: print("   " + m))
        elapsed = time.time() - t0
        after = USAGE.by_stage["prescreen"]

        missing = [s for s in scored if "llm_failed" in s.get("prescreen_labels", [])]
        by_pos = {}
        for i, s in enumerate(scored):
            by_pos.setdefault(i % bs, []).append(s["prescreen_score"])

        results[bs] = {
            "batch_size": bs,
            "calls": after["calls"] - before["calls"],
            "tokens": (after["prompt_tokens"] + after["completion_tokens"]
                       - before["prompt_tokens"] - before["completion_tokens"]),
            "seconds": round(elapsed, 1),
            "missing_responses": len(missing),
            "missing_rate": round(len(missing) / max(1, len(scored)), 3),
            "mean_score": round(statistics.mean(s["prescreen_score"] for s in scored), 2),
            "score_by_position": {k: round(statistics.mean(v), 2) for k, v in sorted(by_pos.items())},
            "scores": {s["id"]: s["prescreen_score"] for s in scored},
            "top12": [s["id"] for s in sorted(scored, key=lambda x: -x["prescreen_score"])[:12]],
        }
        r = results[bs]
        print(f"batch={bs:2d} | 호출 {r['calls']:2d} | 토큰 {r['tokens']:6,} | {r['seconds']:5.1f}s | "
              f"누락 {r['missing_responses']} | 평균 {r['mean_score']} | 위치별 {r['score_by_position']}\n")

    base_scores = dict(results[SIZES[0]]["scores"])
    base_top12 = set(results[SIZES[0]]["top12"])
    for bs, r in results.items():
        a, b = base_top12, set(r["top12"])
        r["top12_jaccard_vs_base"] = round(len(a & b) / len(a | b), 2)
        diffs = [abs(base_scores[k] - r["scores"][k]) for k in base_scores if k in r["scores"]]
        r["mean_abs_score_diff_vs_base"] = round(statistics.mean(diffs), 2) if diffs else None
        r.pop("scores")

    (STORE / "batch_experiment.json").write_text(
        json.dumps({"n_items": len(items), "base_batch": SIZES[0], "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n| batch | 호출 | 토큰 | 초 | 응답누락 | 상위12 일치(vs 4) | 평균점수차(vs 4) |")
    print("|---|---|---|---|---|---|---|")
    for bs, r in results.items():
        print(f"| {bs} | {r['calls']} | {r['tokens']:,} | {r['seconds']} | {r['missing_responses']} | "
              f"{r['top12_jaccard_vs_base']} | {r['mean_abs_score_diff_vs_base']} |")
    print("\n-> store/batch_experiment.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
