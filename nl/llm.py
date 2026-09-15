"""OpenAI 호출 래퍼.

- 항상 JSON 객체로 받는다 (response_format=json_object).
- 실패하면 지수 백오프로 재시도하고, 끝까지 실패하면 LLMError 를 올린다.
  파이프라인 상위에서 이걸 잡아 '스킵' 경로로 빠진다.
- 호출마다 토큰/지연을 기록해 두고 run 종료 시 metrics 에 합산한다.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from .config import MODEL_TIERS


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    by_stage: dict = field(default_factory=dict)

    def add(self, stage: str, pt: int, ct: int, sec: float) -> None:
        self.calls += 1
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.seconds += sec
        s = self.by_stage.setdefault(stage, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0})
        s["calls"] += 1
        s["prompt_tokens"] += pt
        s["completion_tokens"] += ct
        s["seconds"] = round(s["seconds"] + sec, 2)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 2),
            "by_stage": self.by_stage,
        }


USAGE = Usage()

_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI

        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMError("OPENAI_API_KEY 가 설정되지 않았습니다.")
        _client = OpenAI(api_key=key)
    return _client


def chat_json(stage: str, tier: str, system: str, user: str, *, temperature: float = 0.2,
              max_tokens: int = 4000, retries: int = 3) -> dict:
    """JSON 객체 하나를 반환하는 LLM 호출. 파싱까지 책임진다."""
    model = MODEL_TIERS.get(tier, MODEL_TIERS["cheap"])
    last = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            resp = client().chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            sec = time.time() - t0
            u = resp.usage
            USAGE.add(stage, u.prompt_tokens, u.completion_tokens, sec)
            return json.loads(resp.choices[0].message.content)
        except Exception as ex:  # 네트워크/레이트리밋/JSON 깨짐 모두 여기로
            last = ex
            time.sleep(1.5 * (2 ** attempt))
    raise LLMError(f"{stage} LLM 호출 실패 ({type(last).__name__}: {last})")
