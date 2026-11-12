"""SFTP 전송기 (#116) — asyncssh 내장 SFTP 서버를 127.0.0.1 임의 포트에 띄운다. 묶음은 가상 파일."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh
import pytest
from pydantic import SecretStr

from task_spec.delivery.sftp import SftpTarget, remote_subdir, send_role_dir
from task_spec.schemas.package import ArtifactFile
from task_spec.starter.store import sha256sums

SUB = remote_subdir("ct-fake0001", 2, "grader")


@dataclass
class Server:
    root: Path
    target: SftpTarget


def _bundle(d: Path) -> list[ArtifactFile]:
    d.mkdir(parents=True)
    files = []
    for name, body in [("ct-fake0001-b1-grader.tar.zst", b"x" * 300_000), ("grader.json", b"{}")]:
        (d / name).write_bytes(body)
        files.append(
            ArtifactFile(name=name, sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))
        )
    (d / "SHA256SUMS").write_text(sha256sums(files), encoding="utf-8")
    return files


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, list[ArtifactFile]]:
    d = tmp_path / "images" / "ct-fake0001-b1" / "grader"
    return d, _bundle(d)


def _keys(tmp_path: Path) -> tuple[asyncssh.SSHKey, Path, Path]:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "client_key"
    client_key.write_private_key(str(key_path))
    auth = tmp_path / "authorized_keys"
    auth.write_bytes(client_key.export_public_key())
    return host_key, key_path, auth


async def _serve(
    tmp_path: Path, sftp_cls: type[asyncssh.SFTPServer], trust_host: bool = True
) -> AsyncIterator[Server]:
    host_key, key_path, auth = _keys(tmp_path)
    root = tmp_path / "remote"
    (root / "inbox").mkdir(parents=True)

    def factory(chan: Any) -> asyncssh.SFTPServer:
        return sftp_cls(chan, chroot=str(root))

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[host_key],
        authorized_client_keys=str(auth),
        sftp_factory=factory,
    )
    port = server.sockets[0].getsockname()[1]
    known = tmp_path / "known_hosts"
    shown = host_key if trust_host else asyncssh.generate_private_key("ssh-ed25519")
    known.write_bytes(f"[127.0.0.1]:{port} ".encode() + shown.export_public_key())
    target = SftpTarget(
        host=SecretStr("127.0.0.1"),
        port=port,
        user="grader",
        key_path=key_path,
        known_hosts=known,
        remote_dir="/inbox",
    )
    try:
        yield Server(root=root, target=target)
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def srv(tmp_path: Path) -> AsyncIterator[Server]:
    async for s in _serve(tmp_path, asyncssh.SFTPServer):
        yield s


def _remote(s: Server) -> Path:
    return s.root / "inbox" / SUB


async def test_sends_all_files_then_done(
    srv: Server, bundle: tuple[Path, list[ArtifactFile]]
) -> None:
    local, files = bundle
    res = await send_role_dir(srv.target, local, files, SUB, timeout_s=30)
    assert res.ok and res.error is None and res.attempts == 1
    out = _remote(srv)
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [f.name for f in files] + ["SHA256SUMS", ".done"]
    )
    for f in files:
        assert hashlib.sha256((out / f.name).read_bytes()).hexdigest() == f.sha256
    assert (out / "SHA256SUMS").read_text(encoding="utf-8") == sha256sums(files)
    assert res.bytes_sent == sum(f.size_bytes for f in files) + (out / "SHA256SUMS").stat().st_size


async def test_remote_path_is_common_id_revision_role() -> None:
    assert SUB == "ct-fake0001-r2/grader"


async def test_resend_is_blocked(srv: Server, bundle: tuple[Path, list[ArtifactFile]]) -> None:
    local, files = bundle
    assert (await send_role_dir(srv.target, local, files, SUB, timeout_s=30)).ok
    res = await send_role_dir(srv.target, local, files, SUB, timeout_s=30)
    assert not res.ok and res.error == "already_delivered" and res.attempts == 1


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: (d / "grader.json").write_bytes(b"{ }"),  # 크기 바뀜
        lambda d: (d / "grader.json").write_bytes(b"[]"),  # 크기 같고 해시 다름
        lambda d: (d / "grader.json").unlink(),
        lambda d: (d / "SHA256SUMS").write_text("", encoding="utf-8"),
    ],
)
async def test_local_mismatch_sends_nothing(
    srv: Server, bundle: tuple[Path, list[ArtifactFile]], tamper: Callable[[Path], Any]
) -> None:
    local, files = bundle
    tamper(local)
    res = await send_role_dir(srv.target, local, files, SUB, timeout_s=30)
    assert not res.ok and res.error == "local_mismatch" and res.attempts == 0
    assert not (srv.root / "inbox" / "ct-fake0001-r2").exists()


async def test_unknown_host_key_refused(
    tmp_path: Path, bundle: tuple[Path, list[ArtifactFile]]
) -> None:
    local, files = bundle
    async for s in _serve(tmp_path, asyncssh.SFTPServer, trust_host=False):
        res = await send_role_dir(s.target, local, files, SUB, timeout_s=30)
        assert not res.ok and res.error == "host_key" and res.attempts == 1
        assert not (s.root / "inbox" / "ct-fake0001-r2").exists()


async def test_wrong_client_key_is_auth(
    srv: Server, tmp_path: Path, bundle: tuple[Path, list[ArtifactFile]]
) -> None:
    local, files = bundle
    other = tmp_path / "other_key"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(other))
    target = srv.target.model_copy(update={"key_path": other})
    res = await send_role_dir(target, local, files, SUB, timeout_s=30)
    assert not res.ok and res.error == "auth" and res.attempts == 1


class _FailFirstRename(asyncssh.SFTPServer):
    """첫 rename 만 실패 — 첫 파일의 .part 가 원격에 남는다."""

    failed = False

    def rename(self, oldpath: bytes, newpath: bytes) -> None:
        if not _FailFirstRename.failed:
            _FailFirstRename.failed = True
            raise OSError("fake failure")
        super().rename(oldpath, newpath)


async def test_retry_cleans_partial_upload(
    tmp_path: Path, bundle: tuple[Path, list[ArtifactFile]]
) -> None:
    local, files = bundle
    _FailFirstRename.failed = False
    async for s in _serve(tmp_path, _FailFirstRename):
        res = await send_role_dir(s.target, local, files, SUB, timeout_s=30)
        assert res.ok and res.attempts == 2
        names = {p.name for p in _remote(s).iterdir()}
        assert ".done" in names and not any(n.endswith(".part") for n in names)


class _SlowStat(asyncssh.SFTPServer):
    async def stat(self, path: bytes) -> Any:
        await asyncio.sleep(5)
        return super().stat(path)


async def test_timeout_retried_once_then_fails(
    tmp_path: Path, bundle: tuple[Path, list[ArtifactFile]]
) -> None:
    local, files = bundle
    async for s in _serve(tmp_path, _SlowStat):
        res = await send_role_dir(s.target, local, files, SUB, timeout_s=0.5)
        assert not res.ok and res.error == "timeout" and res.attempts == 2
        assert not (_remote(s) / ".done").exists()


async def test_logs_and_result_have_no_host_or_paths(
    tmp_path: Path, bundle: tuple[Path, list[ArtifactFile]], caplog: pytest.LogCaptureFixture
) -> None:
    local, files = bundle
    caplog.set_level(logging.DEBUG, logger="task_spec.delivery")
    async for s in _serve(tmp_path, asyncssh.SFTPServer, trust_host=False):
        res = await send_role_dir(s.target, local, files, SUB, timeout_s=30)
        assert res.error == "host_key"
        text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("task_spec"))
        for secret in ("127.0.0.1", str(tmp_path), "ct-fake0001", "/inbox"):
            assert secret not in res.model_dump_json() and secret not in text
        assert "127.0.0.1" not in repr(s.target)  # host 는 SecretStr
