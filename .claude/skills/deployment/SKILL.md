---
name: deployment
description: P1 과제 명세 서비스의 컨테이너 구성·내부망 배포 — task-api/task-db 이미지 빌드, podman save/load 반입, env-file·볼륨·네트워크·헬스체크, 업그레이드·롤백·백업, 배포 후 확인. docker/ 아래 Dockerfile·run 스크립트를 만들거나 고칠 때, 운영 서버에 배포·업데이트할 때, DB 덤프를 옮길 때 사용.
---

# 배포 (podman, 내부망) [확정: 2026-11-03]

운영 담당자용 단계별 절차: `docs/deploy.md`. 이 문서는 규칙·불변식.

운영 서버는 **podman**, 외부 LLM API(OpenAI·OpenCode)로 나갈 수 있음. 인터넷 패키지 설치는 가정하지 않는다
— 빌드 서버에서 이미지를 만들어 파일로 반입한다.

| 구성 | 이미지 | 네트워크·포트 | 볼륨 |
|---|---|---|---|
| API | `task-api:<버전>` (`docker/api/Dockerfile`) | `task-net`, 기본 `127.0.0.1:8001 → 8000` | `task_data:/data` (권한 700) |
| DB | `task-db:1` (`docker/db/Dockerfile`, PG16 + pgvector) | `task-net`(이름 `task_db`), `127.0.0.1:5434 → 5432` | `task_db_data` |
| nginx (사내망 밖 접속 시 필수) | `nginx:1.27-alpine` + `docker/nginx/nginx.conf` | **호스트 네트워크**, `443`·`80` 직접 수신, API 는 `127.0.0.1:8001` | 인증서·htpasswd·ACME webroot 읽기 전용 마운트 |

## 1. 이미지 규칙 (불변식)

- 넣는 것: 설치된 패키지(`src/`), `prompts/`, `incidents/`. tesseract(kor+eng).
- **넣지 않는 것: `data/`, `.env`, `tests/`, `eval/`, 덤프** — `.containerignore`, `build_release.sh` 가 검사한다.
- 비밀값은 실행 때 `--env-file`(`docker/api/.env`, gitignore·권한 600 — `run.sh` 기본값) [확정: 2026-11-06]로만. 이미지·로그·스크립트에 쓰지 않는다.
- 지원자 데이터는 볼륨 `task_data`(`DATA_DIR=/data`)와 DB 체크포인트에만 있다 — 이미지·로그로 새지 않게.
- **uvicorn 워커 1개** — 실행 상태·SSE 를 프로세스 안에서 관리한다(`api/launcher.py`). 늘리려면 실행 추적을 바깥으로 빼는 설계가 먼저.
- 운영 `ONTOLOGY_VERSION` 은 `dev` 별칭이 아니라 고정 버전 문자열 (불변식 4). 기동 시 적재되지 않은 버전이면 실패한다.
- root 가 아닌 사용자(uid 10001)로 실행.

## 2. 빌드·반입

```bash
# 빌드 서버 (레포 루트, 커밋된 상태)
docker/build_release.sh [버전]            # → dist/task-spec-<버전>/ (images.tar + sha256, docker/, VERSION)
# DB 를 옮길 때: data/exports/ 에 pg_dump -Fc (체크포인트 = 지원자 파생물 포함 여부 확인 후)

# 운영 서버
sha256sum -c images.tar.sha256 && podman load -i images.tar
```

## 3. 실행

```bash
# DB (처음 한 번). 이미지를 load 로 받았으면 빌드 생략
TASK_DB_PASSWORD=<pw> TASK_DB_SKIP_BUILD=1 docker/db/run.sh
podman cp <덤프> task_db:/tmp/restore.dump
podman exec task_db pg_restore -U task -d task_db --no-owner --no-privileges -j 4 /tmp/restore.dump
# (pg 테스트용) createdb task_db_test 후 같은 덤프 복원 — 온톨로지 스키마·규칙이 같다

# API
install -m 600 docker/api/env.example docker/api/.env   # 값 채우기 (DATABASE_URL 호스트 = task_db:5432)
TASK_API_IMAGE=task-api:<버전> docker/api/run.sh            # 내부망 접속: TASK_API_BIND=<서버IP>:8001
```

env 항목은 `docker/api/env.example` (= `.env.example` 중 운영에 필요한 것). 새 Settings 필드를 추가하면 둘 다 고친다.

## 3-1. nginx (TLS + Basic 인증) [확정: 2026-11-03 — TLS 사용, 사내망 밖 접속]

- 앱에 인증이 없으므로 **모든 경로 Basic 인증**(`/healthz` 만 예외). 승인자마다 계정. 앱 인증이 붙으면 다시 본다.
- HTTP→HTTPS, TLS 1.2+, HSTS·nosniff·frame DENY. Basic 인증 헤더는 앱으로 넘기지 않는다.
- SSE `/runs/{id}/events`: `proxy_buffering off`, read 1h. 그 밖 read 600s (LLM_TIMEOUT_SECONDS 300 + 여유). 업로드 25 MB.
- 접근 로그 형식에 쿼리·본문을 넣지 않는다 (지원서 경로). IP 당 10 r/s.
- **rootless podman + 호스트 네트워크** [확정: 2026-11-04]. root podman 은 사용자의 `task-net`·`task-api` 를 못 보고,
  rootless 포트 전달(rootlessport)은 접속자 IP 를 `10.89.0.x` 로 바꿔 IP 당 제한·허용 목록이 무력해진다.
  nginx 는 80·443 을 직접 받고(`net.ipv4.ip_unprivileged_port_start=80`) API 로컬 포트 `127.0.0.1:8001` 로 붙는다 — API 를 `TASK_API_BIND=none` 으로 띄우지 않는다.
- 인증서: Let's Encrypt webroot(`/etc/task-spec/acme`, 80 의 `/.well-known/acme-challenge/` 만 인증·허용 목록 예외).
  IP 주소 인증서는 6일짜리(shortlived) — `renew_before_expiry = 4 days`. deploy hook(`docker/nginx/certbot-deploy-hook.sh`)은
  root 소유 사본을 `renewal-hooks/deploy/` 에 등록 (레포 파일을 root 가 실행하지 않게). 사용자 linger 를 켠다.
- 인증서 받기 전 HTTP 모드(`TASK_NGINX_MODE=http`, `nginx.http.conf`)는 임시 단계 — 평문이므로 실제 지원서 금지.
- privkey 600. htpasswd 는 640(그룹 = 컨테이너 nginx gid 101, `run.sh` 가 맞춤) — worker 가 요청마다 읽는다.
  `run.sh` 가 만드는 설정 조각은 `/etc/task-spec/nginx/` (재부팅 후 재시작에 필요 — `/tmp` 금지).
- 설정을 바꾸면 이 서버에서 `nginx -t` 로 문법만 확인 (더미 인증서·htpasswd, `upstream.conf` 에 `127.0.0.1:8001`).

## 4. 업그레이드·롤백·백업

- 업그레이드: 새 묶음 load → `podman rm -f task-api` → 새 `TASK_API_IMAGE` 로 run. 볼륨·DB 는 그대로.
- 롤백: 이전 태그 이미지를 지우지 않고 남겨 두었다가 같은 방법으로 이전 태그를 run.
- DB 스키마 변경은 앱이 기동 시 `CREATE TABLE IF NOT EXISTS` 로 만든다(예: `common_task`). 파괴적 변경은 별도 마이그레이션 [미정].
- 백업: `pg_dump -Fc` 정기 실행 [미정: 주기·보관 위치·기간]. 덤프와 `task_data` 볼륨에는 지원자 파생물이 있다 — 레포·외부로 내보내지 않는다.
- `DATA_DIR` 보관 기간·삭제 정책 [미정] (pseudonymization 스킬 "저장").

## 5. 배포 후 확인

1. `curl http://<bind>/healthz` → `status: ok`, `database: ok`, `ontology_resolved` = 고정 버전
2. (선택) `pytest -m pg` — `task_db_test` 에 덤프 복원 후, `TEST_DATABASE_URL` ≠ `DATABASE_URL`
3. 가상 지원서 1건으로 `POST /runs` → `awaiting_approval` → `POST /runs/{id}/approve` → `done`
4. `podman logs task-api` 에 원문·키가 없는지

## 주의

- **인증 없음** (승인자 이름을 요청 본문에서 받음, `api/routers/runs.py` TODO(auth)) — 사내망 밖이면 nginx(3-1) 없이 열지 않는다.
- 로컬 개발 DB 컨테이너가 `task-net` 에 없으면 `podman network connect task-net task_db`.
