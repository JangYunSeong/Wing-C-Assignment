"""builder 설정 — API 의 Settings 와 따로 둔다.

builder 는 공고에서 나온 공통 과제만 다룬다 (불변식 3) — 지원자 데이터 경로(DATA_DIR)·온톨로지는 받지 않는다.
운영은 builder 전용 DB 역할(common_task 읽기, 빌드 표 쓰기)의 DATABASE_URL 을 준다 (#129 PR 2).
비용 상한은 qwen 기준 실측(2026-11-11~12 운영 서버 알파 54건: 빌드 1건 평균 $0.05·최대 $0.14)으로 정했다 —
모델을 바꾸면 같이 올린다 (예: gpt-4.1 이면 작업당 $1.0·하루 $10~20) [확정: 2026-11-12 사용자].
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BuilderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    database_url: SecretStr
    openai_api_key: SecretStr
    opencode_api_key: SecretStr
    opencode_base_url: str
    llm_timeout_seconds: float = Field(default=300.0, gt=0)
    prompts_dir: Path = Path("prompts")
    incidents_dir: Path = Path("incidents/public_v1")
    # 과제 전달 묶음 — API 와 같은 호스트 경로 (docs/deploy.md 2-7)
    images_dir: Path = Path("data/images")
    # 빌드 작업 폴더 (계약·생성 코드·검증 로그·이미지 빌드 맥락 — git 밖). podman 소켓을 거쳐도 경로가 맞게 절대 경로로 쓴다
    build_work_dir: Path = Path("data/builder")
    build_worker: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    build_budget_usd: float = Field(default=0.5, gt=0)  # 작업당 — 넘으면 다음 단계로 가지 않고 실패
    build_daily_budget_usd: float = Field(default=5.0, gt=0)  # 하루 — 넘으면 큐에서 가져가지 않음
    build_lease_seconds: int = Field(default=900, ge=60)  # heartbeat 가 이만큼 없으면 죽은 작업
    build_poll_seconds: float = Field(default=30.0, gt=0)
    build_max_attempts: int = Field(
        default=2, ge=1
    )  # lease 만료 뒤 다시 대기열로 (인프라 오류 1회 재시도)
    # 참조 구현을 만들 허용 설계 수 — 비우면 명세의 허용 설계 전부 (#171: d1 만 돌려 숨은 테스트가 d2·d3 에 너무 엄격한 것을 못 걸렀다)
    build_designs: int | None = Field(default=None, ge=1)
    build_keep_failed_work: bool = True  # 실패한 빌드의 작업 폴더를 남긴다 (원인 확인용, git 밖)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    @property
    def work_root(self) -> Path:
        return self.build_work_dir.resolve()
