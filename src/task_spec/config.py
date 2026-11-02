"""애플리케이션 설정 (fastapi-service 스킬 "설정" 절).

`.env` 는 앱이 여기서 직접 로드한다. 셸에서 열지 않는다.
비밀 값(API 키, 비밀번호가 들어 있는 DB URL)은 `SecretStr` — repr·로그·예외 메시지에 값이 찍히지 않는다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """프로세스 설정 하나. 필드 목록은 `.env.example` 이 SSOT."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Postgres (task_db). URL 에 비밀번호가 들어 있으므로 SecretStr.
    database_url: SecretStr
    # 통합 테스트 전용 (task_db_test). 운영 URL 과 같으면 conftest 가 거부한다.
    test_database_url: SecretStr | None = None
    # LangGraph 체크포인터 종류. 단위 테스트만 memory.
    checkpointer: Literal["memory", "postgres"] = "postgres"

    # 온톨로지 버전 고정 산출물 (불변식 4).
    # TODO(ontology-artifact): 형식(onto-vYYYY.MM.DD)·산출물 존재 검증은 산출물 로더가 기동 시 한다.
    ontology_version: str = Field(min_length=1)
    ontology_artifact_dir: Path
    # 지원자 데이터 루트 (git 밖, 불변식 1)
    data_dir: Path

    # LLM 프로바이더 (llm-client 스킬). 값은 로그·provenance·/healthz 에 넣지 않는다.
    openai_api_key: SecretStr
    opencode_api_key: SecretStr
    opencode_base_url: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스당 한 번 로드한다."""
    return Settings()  # type: ignore[call-arg]  # 필수 값은 환경/.env 에서 채워진다
