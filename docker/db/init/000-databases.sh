#!/usr/bin/env bash
# 운영 DB(POSTGRES_DB=task_db)는 이미지가 만든다. 여기서는 통합 테스트 전용 DB 를 추가하고
# 두 DB 모두에 pgvector 확장을 켠다 (retrieve 벡터 후보용, 판정에는 쓰지 않음 — CLAUDE.md 불변식 5).
# 체크포인트 테이블은 앱이 AsyncPostgresSaver.setup() 으로 만든다 (pipeline-graph 스킬).
set -euo pipefail
TEST_DB="${TASK_TEST_DB:-task_db_test}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE DATABASE ${TEST_DB} OWNER ${POSTGRES_USER};
SQL

for db in "$POSTGRES_DB" "$TEST_DB"; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" \
       -c "CREATE EXTENSION IF NOT EXISTS vector;"
done
