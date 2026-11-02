# P1 과제 명세 저장소 — PostgreSQL 16 + pgvector
# (sql_generator docker/log 와 같은 방식: 공식 pgvector 이미지 + 첫 부팅 initdb 훅)
#
# 첫 부팅(빈 볼륨)에만 /docker-entrypoint-initdb.d/*.sql|*.sh 를 알파벳순으로 실행한다.
# 이미 초기화된 볼륨에는 다시 돌지 않는다 — 스키마 변경은 앱 마이그레이션으로.
#
# 빌드:  podman build -f docker/db/Dockerfile -t task-db:1 docker/db
# 실행:  docker/db/run.sh
FROM docker.io/pgvector/pgvector:pg16

COPY init/ /docker-entrypoint-initdb.d/
