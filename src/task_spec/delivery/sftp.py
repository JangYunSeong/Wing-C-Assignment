"""역할 폴더 하나를 SFTP 로 보낸다 (이슈 #116 1단계).

`<images_root>/<build_id>/<role>/` 의 파일 → `<remote_dir>/<common_id>-r<판>/<role>/`.
1. 로컬 확인 — DB 기록(files)과 크기·sha256, SHA256SUMS 본문이 같아야 보낸다 (디스크에서 바뀐 묶음은 내보내지 않는다).
2. 접속 — known_hosts 고정 + 키 인증만.
3. 원격에 `.done` 이 이미 있으면 멈춘다 — 다시 보내기는 막는다 [확정: 2026-11-12 사용자].
4. 파일마다 `<name>.part` 로 올린 뒤 이름을 바꾸고 원격 크기를 확인한다. SHA256SUMS 는 맨 마지막.
5. 빈 `.done` 을 올린다 — 받는 쪽은 `.done` 이 있는 폴더만 가져간다 (반쯤 올라간 파일을 집지 않게).

실패 사유는 코드만 남긴다 (호스트·경로·예외 원문 없이). 접속·전송 오류(`io`·`timeout`)만 한 번 더 시도한다.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import asyncssh
from pydantic import BaseModel

from task_spec.delivery.target import SftpTarget
from task_spec.schemas.package import ArtifactFile, ArtifactRole
from task_spec.starter.artifacts import sha256
from task_spec.starter.store import sha256sums

logger = logging.getLogger(__name__)

SUMS = "SHA256SUMS"
DONE = ".done"
PART = ".part"

SendError = Literal["local_mismatch", "already_delivered", "host_key", "auth", "timeout", "io"]
_RETRYABLE: tuple[SendError, ...] = ("io", "timeout")


__all__ = ["SendError", "SendResult", "SftpTarget", "remote_subdir", "send_role_dir"]


class SendResult(BaseModel):
    ok: bool
    bytes_sent: int = 0
    attempts: int = 0
    error: SendError | None = None


class _Abort(Exception):
    def __init__(self, code: SendError) -> None:
        super().__init__(code)
        self.code: SendError = code


def remote_subdir(common_id: str, revision: int, role: ArtifactRole) -> str:
    """원격 상대 경로 — `<common_id>-r<판>/<role>` [확정: 2026-11-12 사용자]."""
    return f"{common_id}-r{revision}/{role}"


def _check_local(local_dir: Path, files: Sequence[ArtifactFile]) -> list[tuple[Path, int]]:
    """보낼 (경로, 크기) — files 순서 + SHA256SUMS 맨 끝. 하나라도 다르면 local_mismatch."""
    out: list[tuple[Path, int]] = []
    for f in files:
        p = local_dir / f.name
        if not p.is_file() or p.stat().st_size != f.size_bytes or sha256(p) != f.sha256:
            raise _Abort("local_mismatch")
        out.append((p, f.size_bytes))
    sums = local_dir / SUMS
    if not sums.is_file() or sums.read_text(encoding="utf-8") != sha256sums(files):
        raise _Abort("local_mismatch")
    out.append((sums, sums.stat().st_size))
    return out


async def _upload(target: SftpTarget, local: list[tuple[Path, int]], remote: str) -> int:
    try:
        async with (
            asyncssh.connect(
                target.host.get_secret_value(),
                port=target.port,
                username=target.user,
                client_keys=[str(target.key_path)],
                known_hosts=str(target.known_hosts),
                password=None,
                kbdint_auth=False,
                agent_path=None,
            ) as conn,
            conn.start_sftp_client() as sftp,
        ):
            if await sftp.exists(posixpath.join(remote, DONE)):
                raise _Abort("already_delivered")
            await sftp.makedirs(remote, exist_ok=True)
            # 지난 시도의 반쯤 올라간 파일 정리
            for name in await sftp.listdir(remote):
                if name.endswith(PART):
                    await sftp.remove(posixpath.join(remote, name))
            sent = 0
            for path, size in local:
                final = posixpath.join(remote, path.name)
                part = final + PART
                await sftp.put(str(path), part)
                if (await sftp.stat(part)).size != size:
                    raise _Abort("io")
                if await sftp.exists(final):  # 지난 시도에서 끝까지 못 간 묶음 (.done 없음)
                    await sftp.remove(final)
                await sftp.rename(part, final)
                sent += size
            async with sftp.open(posixpath.join(remote, DONE), "wb"):
                pass
            return sent
    except asyncssh.HostKeyNotVerifiable as e:
        raise _Abort("host_key") from e
    except asyncssh.PermissionDenied as e:
        raise _Abort("auth") from e
    except (asyncssh.Error, OSError) as e:
        raise _Abort("io") from e


async def send_role_dir(
    target: SftpTarget,
    local_dir: Path,
    files: Sequence[ArtifactFile],
    remote_sub: str,
    timeout_s: float,
    retries: int = 1,
) -> SendResult:
    """역할 폴더 하나를 보낸다. 예외를 던지지 않고 결과(사유 코드)를 돌려준다."""
    try:
        local = await asyncio.to_thread(_check_local, local_dir, files)
    except _Abort as e:
        logger.warning("sftp send refused: error=%s files=%d", e.code, len(files))
        return SendResult(ok=False, error=e.code)
    remote = posixpath.join(target.remote_dir, remote_sub)
    attempts = 0
    error: SendError = "io"
    while attempts <= retries:
        attempts += 1
        try:
            async with asyncio.timeout(timeout_s):
                sent = await _upload(target, local, remote)
            logger.info("sftp sent: files=%d bytes=%d attempts=%d", len(local), sent, attempts)
            return SendResult(ok=True, bytes_sent=sent, attempts=attempts)
        except TimeoutError:
            error = "timeout"
        except _Abort as e:
            error = e.code
        logger.warning("sftp send failed: error=%s attempt=%d", error, attempts)
        if error not in _RETRYABLE:
            break
    return SendResult(ok=False, attempts=attempts, error=error)
