"""온톨로지 산출물(ontology.sqlite) → PostgreSQL 적재 (버전별 스키마).

    uv run python -m task_spec.ontology.load_pg --sqlite data/ontology/src/data/build/ontology.sqlite \\
        --schema-sql data/ontology/src/scripts/db/schema_pg.sql [--target test]

- 온톨로지 레포의 `schema_pg.sql`(같은 테이블 + PG 타입·제약)을 그대로 쓴다. 이 레포는 스키마를 고치지 않는다.
- 버전마다 스키마 하나: `onto_<버전 슬러그>`. 스테이징 스키마에 적재 → 행 수 대조 → 이름 교체 (중간 실패해도 기존 버전은 그대로).
- 적재 기록: `public.ontology_registry(version, schema_name, source_sha256, row_counts, loaded_at)`.
- 앱은 이 스키마를 읽기 전용 트랜잭션으로만 연다 (ontology/repository.py). 쓰기는 이 적재기만.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from task_spec.config import Settings

# 새 공고 매칭(경로 2)에 쓰는 규칙 — 결과 테이블과 같은 버전으로 묶어 적재한다 (매처: postings/matcher.py)
RULE_GLOBS = (
    "data/rules/competencies/*.json",
    "data/rules/tools/*.json",
    "data/build/seed/tech_seed.json",
)

RULES_DDL = """
CREATE TABLE IF NOT EXISTS public.ontology_rules (
    version text NOT NULL,
    path text NOT NULL,
    sha256 text NOT NULL,
    content json NOT NULL,  -- jsonb 아님: jsonb 는 키 순서를 바꾼다 (수준 규칙 등 순서 = 우선순위)
    PRIMARY KEY (version, path)
)
"""

REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS public.ontology_registry (
    version text PRIMARY KEY,
    schema_name text NOT NULL UNIQUE,
    source_sha256 text NOT NULL,
    row_counts jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
)
"""


def schema_name(version: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", version.lower()).strip("_")
    return f"onto_{slug}"[:55]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _columns(cur: psycopg.Cursor, schema: str) -> dict[str, list[tuple[str, str]]]:
    cur.execute(
        "SELECT c.table_name, c.column_name, c.udt_name FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE' "  # 뷰 제외
        "ORDER BY c.table_name, c.ordinal_position",
        (schema,),
    )
    cols: dict[str, list[tuple[str, str]]] = {}
    for t, c, udt in cur.fetchall():
        cols.setdefault(t, []).append((c, udt))
    return cols


def _table_order(cur: psycopg.Cursor, schema: str, tables: list[str]) -> list[str]:
    """외래키 의존 순서 (참조되는 테이블 먼저)."""
    cur.execute(
        """
        SELECT tc.table_name, ccu.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
        """,
        (schema,),
    )
    deps: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in cur.fetchall():
        if child != parent:
            deps[child].add(parent)
    order: list[str] = []
    while deps:
        ready = sorted(t for t, d in deps.items() if not d - set(order))
        if not ready:
            raise RuntimeError(f"외래키 순환: {sorted(deps)}")
        order += ready
        for t in ready:
            del deps[t]
    return order


def _analyze_keys(cur: psycopg.Cursor, schema: str) -> None:
    """인덱스에 쓰이는 키 컬럼만 통계 갱신.

    COPY 직후엔 통계가 없어 플래너가 인덱스 대신 전체 스캔을 고른다 (평가 근거 질의가 수 초).
    테이블 전체 ANALYZE 는 긴 한국어 텍스트 컬럼(수행준거·KSA 본문)의 통계 계산이 수십 분 걸려 쓰지 않는다
    (2026-11-02 실측: ncs_unit 1.3만 행에서 10분 이상). 키 컬럼만이면 테이블당 1초 이내.
    """
    cur.execute(
        """
        SELECT c.relname, array_agg(DISTINCT a.attname ORDER BY a.attname)
          FROM pg_index i
          JOIN pg_class c ON c.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
         WHERE n.nspname = %s
         GROUP BY c.relname
        """,
        (schema,),
    )
    for table, cols in cur.fetchall():
        cur.execute(
            sql.SQL("ANALYZE {} ({})").format(
                sql.Identifier(schema, table), sql.SQL(", ").join(map(sql.Identifier, cols))
            )
        )


def _convert(value: object, udt: str) -> object:
    if value is None:
        return None
    if udt == "bool":
        return bool(value)
    if udt == "jsonb":
        return Jsonb(json.loads(value)) if isinstance(value, str) else Jsonb(value)
    if udt.startswith("_"):  # 배열 (text[]) — sqlite 에는 JSON 배열 텍스트
        return json.loads(value) if isinstance(value, str) else value
    return value


def load(
    sqlite_path: Path, schema_sql: Path, database_url: str, rules_root: Path | None = None
) -> dict[str, object]:
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    version = src.execute("SELECT version FROM ontology_version").fetchone()[0]
    target = schema_name(version)
    staging = f"{target}__staging"[:63]
    source_sha = _sha256(sqlite_path)
    counts: dict[str, int] = {}

    with psycopg.connect(database_url, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(REGISTRY_DDL)
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(staging)))
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(staging)))
        cur.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(staging)))
        cur.execute(schema_sql.read_text(encoding="utf-8"))
        cols = _columns(cur, staging)
        tables = sorted(cols)
        for t in _table_order(cur, staging, tables):
            names = [c for c, _ in cols[t]]
            udts = [u for _, u in cols[t]]
            rows = src.execute(f'SELECT {", ".join(names)} FROM "{t}"')  # noqa: S608 — 이름은 PG 카탈로그에서 온 것
            copy = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(staging, t), sql.SQL(", ").join(map(sql.Identifier, names))
            )
            n = 0
            with cur.copy(copy) as cp:
                for row in rows:
                    cp.write_row([_convert(v, u) for v, u in zip(row, udts, strict=True)])
                    n += 1
            counts[t] = n
        # 행 수 대조 (원천 sqlite == 적재 결과)
        for t, n in counts.items():
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(staging, t)))
            got = cur.fetchone()[0]  # type: ignore[index]
            src_n = src.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]  # noqa: S608
            if not (got == n == src_n):
                raise RuntimeError(f"행 수 불일치 {t}: sqlite {src_n}, pg {got}")
        _analyze_keys(cur, staging)
        cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(target)))
        cur.execute(
            sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                sql.Identifier(staging), sql.Identifier(target)
            )
        )
        cur.execute(
            "INSERT INTO public.ontology_registry (version, schema_name, source_sha256, row_counts) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (version) DO UPDATE SET schema_name = EXCLUDED.schema_name, "
            "source_sha256 = EXCLUDED.source_sha256, row_counts = EXCLUDED.row_counts, loaded_at = now()",
            (version, target, source_sha, Jsonb(counts)),
        )
        n_rules = _load_rules(cur, version, rules_root) if rules_root is not None else 0
        conn.commit()
    return {
        "rules": n_rules,
        "version": version,
        "schema": target,
        "tables": len(counts),
        "rows": sum(counts.values()),
        "sha256": source_sha,
    }


def _load_rules(cur: psycopg.Cursor, version: str, root: Path) -> int:
    cur.execute(RULES_DDL)
    cur.execute(  # 예전 jsonb 컬럼이면 json 으로 (행은 아래에서 다시 넣는다)
        "SELECT data_type FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='ontology_rules' AND column_name='content'"
    )
    if (cur.fetchone() or [""])[0] == "jsonb":
        cur.execute("DELETE FROM public.ontology_rules")
        cur.execute(
            "ALTER TABLE public.ontology_rules ALTER COLUMN content TYPE json USING content::json"
        )
    cur.execute("DELETE FROM public.ontology_rules WHERE version = %s", (version,))
    files = sorted({f for g in RULE_GLOBS for f in root.glob(g)})
    if not files:
        raise RuntimeError(f"규칙 파일이 없습니다: {root}")
    for f in files:
        raw = f.read_bytes()
        cur.execute(
            "INSERT INTO public.ontology_rules (version, path, sha256, content) VALUES (%s, %s, %s, %s)",
            (
                version,
                str(f.relative_to(root)),
                hashlib.sha256(raw).hexdigest(),
                raw.decode("utf-8"),  # 원문 그대로 (json 컬럼이 텍스트를 보존 — 키 순서 유지)
            ),
        )
    return len(files)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--sqlite", type=Path, required=True)
    ap.add_argument("--schema-sql", type=Path, required=True)
    ap.add_argument(
        "--rules-root", type=Path, default=None, help="온톨로지 레포 루트 (새 공고 매칭 규칙 적재)"
    )
    ap.add_argument(
        "--target",
        choices=["main", "test"],
        default="main",
        help="main=DATABASE_URL, test=TEST_DATABASE_URL",
    )
    a = ap.parse_args()
    s = Settings()  # type: ignore[call-arg]
    url = s.database_url if a.target == "main" else s.test_database_url
    if url is None:
        sys.exit("TEST_DATABASE_URL 이 없습니다")
    print(
        json.dumps(
            load(a.sqlite, a.schema_sql, url.get_secret_value(), a.rules_root), ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
