"""설정 로딩. 기준값은 전부 audience.yaml / sources.yaml 에 있고 코드는 읽기만 한다."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "store"
OUT = ROOT / "out"

# 모델 티어 -> 실제 모델명. audience.yaml 은 "cheap"/"smart" 로만 말한다.
MODEL_TIERS = {
    "cheap": os.environ.get("NL_MODEL_CHEAP", "gpt-4.1-mini"),
    "smart": os.environ.get("NL_MODEL_SMART", "gpt-4.1"),
}


def load_env() -> None:
    """.env 가 있으면 읽는다. 없으면 이미 설정된 환경변수를 그대로 쓴다."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        pass


def load_audience() -> dict:
    return yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))


def load_sources() -> dict:
    return yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))


def now_local(cfg: dict) -> datetime:
    tz = ZoneInfo(cfg["newsletter"].get("timezone", "Asia/Seoul"))
    return datetime.now(timezone.utc).astimezone(tz)


def run_dir(run_id: str) -> Path:
    d = STORE / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d
