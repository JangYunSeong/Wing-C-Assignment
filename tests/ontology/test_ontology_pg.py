"""온톨로지 PG 적재·읽기 전용 접근 — task_db_test 에서만 (`uv run pytest -m pg`)."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from task_spec.ontology.load_pg import load, schema_name
from task_spec.ontology.repository import OntologyNotLoaded, OntologyRepository, resolve_version

pytestmark = pytest.mark.pg

MINI_SCHEMA = """
CREATE TABLE ontology_version (version text PRIMARY KEY, git_commit text, train_version text, built_at timestamptz, sources jsonb);
CREATE TABLE fam (id text PRIMARY KEY, label text NOT NULL);
CREATE TABLE sub (id text PRIMARY KEY, fam_id text NOT NULL REFERENCES fam(id), in_pilot boolean NOT NULL, units text[], levels jsonb);
CREATE VIEW v_sub AS SELECT s.id, f.label FROM sub s JOIN fam f ON f.id = s.fam_id;
"""


class _Urls(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None


@pytest.fixture(scope="module")
def test_url() -> str:
    u = _Urls()
    if u.test_database_url is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    url = u.test_database_url.get_secret_value()
    if u.database_url is not None and url == u.database_url.get_secret_value():
        pytest.fail("TEST_DATABASE_URL == DATABASE_URL — 운영 DB 로 테스트하지 않는다")
    return url


@pytest.fixture
def mini(tmp_path: Path, test_url: str):  # type: ignore[no-untyped-def]
    """가상 미니 온톨로지 (sub 가 fam 을 참조 — 외래키 순서 적재 확인)."""
    version = f"vtest-{uuid.uuid4().hex[:8]}"
    db = tmp_path / "mini.sqlite"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE ontology_version (version, git_commit, train_version, built_at, sources);"
        "CREATE TABLE fam (id, label); CREATE TABLE sub (id, fam_id, in_pilot, units, levels);"
    )
    c.execute(
        "INSERT INTO ontology_version VALUES (?, 'abc', 't1', '2026-11-02T00:00:00', '{\"ncs\": \"29차\"}')",
        (version,),
    )
    c.executemany("INSERT INTO fam VALUES (?, ?)", [("f1", "가상 직군"), ("f2", "가상 직군 2")])
    c.executemany(
        "INSERT INTO sub VALUES (?, ?, ?, ?, ?)",
        [("s1", "f1", 1, '["03", "06"]', '{"수행": 1}'), ("s2", "f2", 0, None, None)],
    )
    c.commit()
    c.close()
    schema_sql = tmp_path / "schema_pg.sql"
    schema_sql.write_text(MINI_SCHEMA, encoding="utf-8")
    yield version, db, schema_sql
    with psycopg.connect(test_url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name(version)}" CASCADE')
        conn.execute("DELETE FROM public.ontology_registry WHERE version = %s", (version,))


def test_load_types_order_counts(mini, test_url: str) -> None:  # type: ignore[no-untyped-def]
    version, db, schema_sql = mini
    out = load(db, schema_sql, test_url)
    assert out["version"] == version and out["tables"] == 3 and out["rows"] == 5
    with psycopg.connect(test_url) as conn:
        conn.execute(f'SET search_path TO "{out["schema"]}"')
        rows = conn.execute("SELECT id, in_pilot, units, levels FROM sub ORDER BY id").fetchall()
        assert rows == [("s1", True, ["03", "06"], {"수행": 1}), ("s2", False, None, None)]
        assert (
            conn.execute("SELECT count(*) FROM v_sub").fetchone()[0] == 2
        )  # 뷰는 적재 대상 아님, 조회는 됨
        reg = conn.execute(
            "SELECT row_counts FROM public.ontology_registry WHERE version = %s", (version,)
        ).fetchone()[0]
        assert reg == {"ontology_version": 1, "fam": 2, "sub": 2}
    again = load(db, schema_sql, test_url)  # 같은 버전 재적재 — 스테이징 교체, 오류 없음
    assert again["rows"] == 5


async def test_repository_read_only_and_resolve(mini, test_url: str) -> None:  # type: ignore[no-untyped-def]
    version, db, schema_sql = mini
    load(db, schema_sql, test_url)
    repo = await OntologyRepository.open(test_url, version)
    try:
        assert repo.version.version == version
        rows = await repo.fetch("SELECT id FROM sub WHERE in_pilot = %s", (True,))
        assert rows == [{"id": "s1"}]
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await repo.fetch("INSERT INTO fam VALUES ('f9', 'x') RETURNING id")
        info = await repo.built_info()
        assert info["git_commit"] == "abc"
    finally:
        await repo.close()


async def test_dev_alias_resolves_latest_and_unknown_fails(mini, test_url: str) -> None:  # type: ignore[no-untyped-def]
    version, db, schema_sql = mini
    load(db, schema_sql, test_url)
    assert (await resolve_version(test_url, "dev")).version == version  # 방금 적재한 것이 최신
    with pytest.raises(OntologyNotLoaded):
        await resolve_version(test_url, "v1999-01-01@none")


async def test_real_ontology_views(test_url: str) -> None:
    """test DB 에 적재된 실제 온톨로지 — 뷰·주요 테이블이 조회되는지 (적재 안 됐으면 skip)."""
    try:
        ver = await resolve_version(test_url, "v2026-11-06f@60d5031+dirty")
    except OntologyNotLoaded:
        pytest.skip("실제 온톨로지 미적재 (load_pg --target test)")
    repo = await OntologyRepository.open(test_url, ver.version)
    try:
        n = await repo.fetch("SELECT count(*) AS n FROM competency")
        assert n[0]["n"] > 100
        links = await repo.fetch("SELECT * FROM v_competency_links LIMIT 1")
        assert links
    finally:
        await repo.close()
