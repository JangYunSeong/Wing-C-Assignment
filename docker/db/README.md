# task_db — PostgreSQL 16 + pgvector

LangGraph 체크포인터(`AsyncPostgresSaver`)와 실행 데이터 저장소. 구성은 sql_generator `docker/log` 와 같은 방식.

| 항목 | 값 |
|---|---|
| 컨테이너 | `task_db` (이미지 `task-db:1`, base `pgvector/pgvector:pg16`) |
| 포트 | `127.0.0.1:5434 → 5432` (로컬 전용 바인드) |
| 사용자 | `task` (superuser) |
| DB | `task_db` (운영·로컬), `task_db_test` (통합 테스트 `-m pg`) |
| 볼륨 | `task_db_data` |
| 확장 | `vector` (두 DB 모두) |

## 기동

```bash
TASK_DB_PASSWORD=<pw> docker/db/run.sh
podman exec task_db psql -U task -d task_db -c '\l'
```

`.env`:

```
DATABASE_URL=postgresql://task:<pw>@127.0.0.1:5434/task_db
TEST_DATABASE_URL=postgresql://task:<pw>@127.0.0.1:5434/task_db_test
```

## 네트워크

`docker/db/run.sh` 는 컨테이너를 podman 네트워크 `task-net` 에 붙인다 — `task-api` 컨테이너는 `task_db:5432` 로 접속한다
(deployment 스킬). 이미지를 `podman load` 로 받은 서버에서는 `TASK_DB_SKIP_BUILD=1`.

## 주의

- 체크포인트 테이블에는 LangGraph State 가 들어간다 = **지원자 파생물**. 볼륨·덤프를 레포나 외부로 내보내지 않는다.
- 초기화 스크립트(`init/`)는 **빈 볼륨 첫 부팅에만** 돈다. 다시 돌리려면 `podman rm -f task_db && podman volume rm task_db_data` (데이터 삭제).
- 통합 테스트는 `task_db_test` 에만. conftest 가 `TEST_DATABASE_URL == DATABASE_URL` 이면 거부한다.
