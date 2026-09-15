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


def _parse_env_file(path: Path) -> int:
    """python-dotenv 없이 .env 를 읽는 최소 파서.

    라이브러리 설치 여부와 무관하게 파이프라인이 돌아야 해서 폴백을 둔다.
    - utf-8-sig 로 읽어 메모장이 붙이는 BOM 을 흡수한다
    - KEY=VALUE 만 인식하고 'export ' 접두사를 허용한다
    - 값 양끝의 따옴표만 벗긴다. 줄 끝 주석은 제거하지 않는다
      (비밀번호에 '#' 이 들어갈 수 있어서 자르면 오히려 위험하다)
    """
    n = 0
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ.setdefault(key, value)
            n += 1
    return n


def load_env(verbose: bool = False) -> str:
    """.env 를 읽는다. 어떤 경로로 읽었는지 문자열로 돌려준다.

    반환값: "dotenv" | "builtin" | "no_file" | "no_file_no_lib"
    이미 설정된 환경변수는 덮어쓰지 않는다.
    """
    path = ROOT / ".env"
    try:
        from dotenv import load_dotenv

        if path.exists():
            load_dotenv(path, override=False)
            if verbose:
                print(f"[env] python-dotenv 로 {path} 를 읽었습니다.")
            return "dotenv"
        if verbose:
            print(f"[env] {path} 가 없습니다. 이미 설정된 환경변수를 씁니다.")
        return "no_file"
    except ImportError:
        if path.exists():
            n = _parse_env_file(path)
            if verbose:
                print(f"[env] python-dotenv 가 없어 내장 파서로 {path} 에서 {n}개 값을 읽었습니다.")
            return "builtin"
        if verbose:
            print(f"[env] python-dotenv 도 {path} 도 없습니다. 이미 설정된 환경변수만 씁니다.")
        return "no_file_no_lib"


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
