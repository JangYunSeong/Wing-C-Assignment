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
    # 프롬프트 루트 (prompt-registry). CURRENT 는 기동 시 전부 검증한다.
    prompts_dir: Path = Path("prompts")
    # 공개 사례 (부서 결정적 사건 대용, incidents/public_v1/README.md) — git 안, 지원자 데이터 아님
    incidents_dir: Path = Path("incidents/public_v1")
    # 지원자 데이터 루트 (git 밖, 불변식 1)
    data_dir: Path
    # 과제 전달 묶음 루트 (data/images/<build_id>/…, 이슈 #85). 지원자 데이터 볼륨과 나눠 두려면 따로 지정한다 —
    # 비우면 <data_dir>/images. 수백 MB 이미지 파일이라 백업·보관 정책이 지원자 데이터와 다르다
    images_dir: Path | None = None

    @property
    def images_root(self) -> Path:
        return self.images_dir if self.images_dir is not None else self.data_dir / "images"

    # 자기소개서 사용 여부 [확정: 2026-11-03 사용자 — 검증할 주장은 이력서에서만].
    # false 면 API 가 자기소개서를 저장하지 않고, 그래프도 처리하지 않는다 (개인정보 최소 처리).
    use_cover_letters: bool = False
    # 공통 과제 기능 개발형 기본값 규칙 (spec/kind.py) [2026-11-06]. false 면 규칙이 feature 여도 이슈형으로 만든다 —
    # 승인자가 수정 요청에서 기능형을 지정하는 것은 항상 된다. draft_feature_task 평가(설계 §9, PR-4) 통과 뒤 켠다
    feature_task_default: bool = False

    # 업로드 (fastapi-service "업로드"): 파일 1개당 바이트 상한. 허용 형식은 api/uploads.py.
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    # SSE 상태 폴링 간격(초)
    sse_poll_seconds: float = Field(default=0.5, gt=0)
    # 임시 화면 /ui/ (과제 명세 작업대, api/routers/ui.py). 인증이 없으니 운영에서는 nginx Basic 인증 뒤에서만 켠다
    ui_enabled: bool = True

    # LLM 프로바이더 (llm-client 스킬). 값은 로그·provenance·/healthz 에 넣지 않는다.
    openai_api_key: SecretStr
    opencode_api_key: SecretStr
    opencode_base_url: str
    # LLM 요청 1건 제한 시간(초). 공통 과제 생성처럼 출력이 긴 호출은 120초를 넘는다 (2026-11-03 운영)
    llm_timeout_seconds: float = Field(default=300.0, gt=0)

    # 로깅 (logging_setup.py). 원문·키는 어느 수준에서도 남기지 않는다
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스당 한 번 로드한다."""
    return Settings()  # type: ignore[call-arg]  # 필수 값은 환경/.env 에서 채워진다
