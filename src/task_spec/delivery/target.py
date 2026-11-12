"""SFTP 받는 곳 설정 — config 가 불러오므로 pydantic 만 쓴다 (asyncssh·저장소를 끌어오지 않게)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class SftpTarget(BaseModel):
    """받는 곳 하나 — `.env` 의 `SFTP_TARGETS__<이름>__*`. DB(delivery_target)에는 이름·역할만 둔다."""

    model_config = ConfigDict(frozen=True)

    host: SecretStr  # 로그·repr 에 찍히지 않게
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1)
    key_path: Path
    known_hosts: Path
    remote_dir: str = Field(min_length=1, pattern=r"^/")
