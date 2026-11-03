# 운영 서버 배포 가이드 (podman)

P1 과제 명세 서비스를 운영 서버에 올리는 절차. 인터넷이 막힌 서버는 이미지를 파일로 반입하고, 사내망 밖에서 접속하는 서버는 nginx(2-6, TLS + Basic 인증)를 반드시 앞에 둔다. 규칙·불변식은 `.claude/skills/deployment/SKILL.md` 가 SSOT.

## 0. 준비물

| 항목 | 비고 |
|---|---|
| 운영 서버 | podman, 외부 LLM API(OpenAI·OpenCode) 접근 가능 |
| 반입 묶음 `dist/task-spec-<버전>/` | 빌드 서버에서 `docker/build_release.sh` 로 생성 (images.tar + sha256, docker/, VERSION) |
| DB 덤프 `task_db_<날짜>.dump` (+ `.sha256`) | 빌드 서버 `data/exports/` — 온톨로지 스키마·규칙 포함 |
| API 키 | OpenAI, OpenCode (+ OpenCode base URL) |
| DB 비밀번호 | 운영 서버에서 새로 정한다 |

## 1. 빌드 서버 (인터넷 되는 곳)

```bash
git switch main && git pull
docker/build_release.sh                   # → dist/task-spec-<날짜>-<커밋>/
# DB 덤프 (체크포인트 = 지원자 파생물. 0건인지 확인 후)
podman exec task_db psql -U task -d task_db -tAc "select count(*) from checkpoints"
podman exec task_db pg_dump -U task -d task_db -Fc --no-owner --no-privileges > data/exports/task_db_$(date +%Y%m%d).dump
sha256sum data/exports/task_db_*.dump > data/exports/task_db.dump.sha256
```

`dist/task-spec-<버전>/` 폴더와 덤프 파일을 운영 서버로 옮긴다.

## 2. 운영 서버

```bash
cd task-spec-<버전>

# 2-1. 이미지
sha256sum -c images.tar.sha256
podman load -i images.tar                 # task-api:<버전>, task-db:1

# 2-2. DB 기동 (처음 한 번)
TASK_DB_PASSWORD='<비밀번호>' TASK_DB_SKIP_BUILD=1 docker/db/run.sh
podman healthcheck run task_db

# 2-3. DB 복원 — 운영 DB 와 pg 테스트 DB 에 같은 덤프
sha256sum -c task_db.dump.sha256
podman cp task_db_<날짜>.dump task_db:/tmp/restore.dump
podman exec task_db pg_restore -U task -d task_db --no-owner --no-privileges -j 4 /tmp/restore.dump
podman exec task_db createdb -U task task_db_test
podman exec task_db pg_restore -U task -d task_db_test --no-owner --no-privileges -j 4 /tmp/restore.dump
podman exec task_db rm /tmp/restore.dump
podman exec task_db psql -U task -d task_db -tAc "select version from public.ontology_registry"
#   → v2026-10-30e@b69d00f+dirty  (env 의 ONTOLOGY_VERSION 과 같아야 함)

# 2-4. env 파일 (레포·이미지에 넣지 않음)
sudo install -d -m 700 /etc/task-spec
sudo install -m 600 docker/api/env.example /etc/task-spec/env
sudo vi /etc/task-spec/env                # 키·DB 비밀번호·ONTOLOGY_VERSION

# 2-5. API 기동
TASK_API_IMAGE=task-api:<버전> docker/api/run.sh
#   nginx 앞단(2-6)을 쓰면 TASK_API_BIND=none — API 는 호스트에 포트를 열지 않는다
#   nginx 없이 내부망에서만: TASK_API_BIND=<서버IP>:8001 (인증 없음 — 접근 대상을 방화벽으로 제한)
```

### 2-6. nginx (TLS + Basic 인증) — 사내망 밖에서 접속할 때 필수

앱에는 아직 인증이 없다. 사내망 밖에서 접속하는 서버라면 **nginx 의 TLS 와 Basic 인증이 유일한 보호막**이다.

```bash
# 인증서 (사내 CA·공인 인증서). privkey 권한 600
sudo install -d -m 700 /etc/task-spec/certs
sudo install -m 644 fullchain.pem /etc/task-spec/certs/fullchain.pem
sudo install -m 600 privkey.pem   /etc/task-spec/certs/privkey.pem

# Basic 인증 계정 (bcrypt). 승인자마다 계정을 따로 만든다
sudo podman run --rm --entrypoint htpasswd docker.io/library/httpd:2.4-alpine -nbB <계정> '<비밀번호>' \
  | sudo tee -a /etc/task-spec/htpasswd >/dev/null
sudo chmod 600 /etc/task-spec/htpasswd

# (선택) 접속 IP 제한 — 'allow 203.0.113.0/24;' 줄들 + 마지막 줄 'deny all;'
# sudo vi /etc/task-spec/allow.conf

# API 를 포트 없이 다시 띄우고 nginx 기동 (443·80 은 특권 포트 — root podman 또는 net.ipv4.ip_unprivileged_port_start 조정)
podman rm -f task-api && TASK_API_IMAGE=task-api:<버전> TASK_API_BIND=none docker/api/run.sh
sudo TASK_NGINX_CERTS=/etc/task-spec/certs TASK_NGINX_AUTH=/etc/task-spec/htpasswd \
     [TASK_NGINX_ALLOW=/etc/task-spec/allow.conf] docker/nginx/run.sh
```

nginx 설정(`docker/nginx/nginx.conf`) 요점: HTTP→HTTPS, TLS 1.2+, HSTS·보안 헤더, 모든 경로 Basic 인증(`/healthz` 만 예외),
SSE(`/runs/{id}/events`) 버퍼링 끔·1시간, 일반 요청 600초(과제 생성), 업로드 25 MB, IP 당 10 r/s 제한,
접근 로그에 쿼리·본문 없음, Basic 인증 헤더는 앱으로 넘기지 않음.
nginx 와 API 컨테이너는 같은 podman 네트워크(`task-net`)에 있어야 한다.

## 3. 확인

```bash
curl -s http://127.0.0.1:8001/healthz     # status ok, database ok, ontology_resolved = 고정 버전
# nginx 앞단이면: curl -s https://<서버 주소>/healthz  (인증 없이 ok)
#                 curl -s -o /dev/null -w '%{http_code}' https://<서버 주소>/runs/x  → 401 (Basic 인증 동작)
podman logs --tail 50 task-api            # 원문·키가 찍히지 않아야 함
```

가상 지원서 1건으로 `POST /runs` → 상태 `awaiting_approval` → `POST /runs/{id}/approve` → `done` 까지 확인한다.

(선택) pg 통합 테스트는 레포가 있는 환경에서 `TEST_DATABASE_URL` 을 운영 서버 `task_db_test` 로 두고 `uv run pytest -m pg`.

## 4. 업그레이드·롤백

```bash
podman load -i images.tar                 # 새 버전
podman rm -f task-api
TASK_API_IMAGE=task-api:<새 버전> docker/api/run.sh
# 롤백: 이전 태그로 같은 명령 (이전 이미지는 지우지 않고 둔다)
```

DB·`task_data` 볼륨은 그대로 남는다. 새 테이블은 앱이 기동할 때 만든다.

## 5. 정해야 할 것 [미정]

- 앱 인증 (지금은 nginx Basic 인증이 임시 대책 — 승인자 식별이 요청 본문의 reviewer 값뿐)
- DB 백업 주기·보관 위치·기간 (덤프와 `task_data` 볼륨에 지원자 파생물이 있음 — 외부 반출 금지)
- `DATA_DIR`(`task_data` 볼륨) 보관 기간·삭제 정책

## 문제 해결

| 증상 | 확인 |
|---|---|
| API 가 바로 종료 | `podman logs task-api` — 온톨로지 버전 미적재(복원·ONTOLOGY_VERSION 확인), env 필수 값 누락 |
| `database: error` | 두 컨테이너가 `task-net` 에 있는지 (`podman network inspect task-net`), DATABASE_URL 호스트가 `task_db` 인지 |
| pg 테스트 skip | `task_db_test` 에 덤프가 복원됐는지, `TEST_DATABASE_URL` ≠ `DATABASE_URL` |
| `pg_restore` 에서 vector 오류 | `task-db:1` 이미지(pgvector 포함)를 쓰는지 |
| 실행이 `draft_spec` 에서 오래 멈춤 | `podman logs task-api \| grep -E "llm (retry\|give up)\|node done\|draft_task"` — 시간 초과면 `LLM_TIMEOUT_SECONDS` 확인(기본 300). 시간 초과는 1번만 재시도 |
| 앱 로그가 안 보임 | `LOG_LEVEL`(기본 INFO). 노드마다 `node done run_id=… node=… ms=…` 가 찍혀야 정상 |
| nginx 502 | `task-api` 가 `task-net` 에 있는지, `podman logs task-api` |
| 진행 상황(SSE)이 늦게·한꺼번에 옴 | nginx 설정의 events 경로 `proxy_buffering off` 가 적용됐는지 (`podman exec task-nginx nginx -T`) |
| nginx 가 443 에 못 붙음 | rootless podman 특권 포트 — root 로 실행하거나 `sysctl net.ipv4.ip_unprivileged_port_start=80` |
