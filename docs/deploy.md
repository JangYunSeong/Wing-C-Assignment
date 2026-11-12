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
#   → v2026-11-06f@60d5031+dirty  (env 의 ONTOLOGY_VERSION 과 같아야 함)

# 2-4. env 파일 (레포·이미지에 넣지 않음)
install -m 600 docker/api/env.example docker/api/.env   # gitignore — run.sh 가 기본으로 읽는다
vi docker/api/.env                        # 키·DB 비밀번호·ONTOLOGY_VERSION
#   DATABASE_URL 호스트는 task_db:5432 (컨테이너 기준). 127.0.0.1·localhost 면 run.sh 가 기동을 거부한다

# 2-5. API 기동
TASK_API_IMAGE=task-api:<버전> docker/api/run.sh
#   기본 127.0.0.1:8001 — nginx 앞단(2-6, 호스트 네트워크)이 이 로컬 포트로 붙는다. TASK_API_BIND=none 으로 띄우지 않는다
#   nginx 없이 내부망에서만: TASK_API_BIND=<서버IP>:8001 (인증 없음 — 접근 대상을 방화벽으로 제한)
```

### 2-6. nginx (TLS + Basic 인증) — 사내망 밖에서 접속할 때 필수

앱에는 아직 인증이 없다. 사내망 밖에서 접속하는 서버라면 **nginx 의 TLS 와 Basic 인증이 유일한 보호막**이다.

nginx 는 **rootless podman, 호스트 네트워크**로 띄운다 (`sudo` 로 띄우지 않는다 — root podman 은 사용자의 `task-net`·컨테이너를 못 본다).
rootless 포트 전달(rootlessport)은 접속자 IP 를 내부 주소(`10.89.0.x`)로 바꿔 IP 당 요청 제한·허용 목록이 동작하지 않으므로,
nginx 가 80·443 을 직접 받고 API 에는 로컬 포트 `127.0.0.1:8001` 로 붙는다.

```bash
# (한 번) 사용자 권한으로 80·443 을 열 수 있게, 로그아웃·재부팅 뒤에도 rootless 컨테이너·hook 이 돌게
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-task-spec.conf && sudo sysctl --system
sudo loginctl enable-linger <사용자>
# 방화벽·공유기: ufw 에 80/tcp·443/tcp 허용, 공유기(NAT 뒤라면)에서 80·443 → 서버 내부 IP 포워딩 + DHCP 고정 할당
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp

# Basic 인증 계정 (bcrypt cost 12). 승인자마다 계정을 따로 만든다. 권한은 run.sh 가 640(그룹 = 컨테이너 nginx)으로 맞춘다
printf '%s\n' '<비밀번호>' | podman run --rm -i docker.io/library/httpd:2.4-alpine htpasswd -niB -C 12 <계정> \
  | sed '/^$/d' >> /etc/task-spec/htpasswd

# (선택) 접속 IP 제한 — 'allow 203.0.113.0/24;' 줄들 + 마지막 줄 'deny all;'
# vi /etc/task-spec/allow.conf

# certbot 확인 경로 (80 의 /.well-known/acme-challenge/ 로 낸다)
install -d -m 755 /etc/task-spec/acme
```

#### 인증서 — Let's Encrypt (도메인 또는 IP 주소)

1. 인증서 없이 **HTTP 모드**로 먼저 띄운다. 평문이므로 이 단계에서는 실제 지원서를 올리지 않는다.
   ```bash
   TASK_NGINX_MODE=http TASK_NGINX_AUTH=/etc/task-spec/htpasswd docker/nginx/run.sh
   ```
2. 바깥에서 80 이 열렸는지 확인한다 — 접근 로그에 바깥 요청이 찍혀야 한다 (`podman logs task-nginx`). 같은 공유기 안에서의 접속은 확인이 아니다.
3. deploy hook 을 root 소유로 설치하고 발급한다. hook 은 인증서를 `/etc/task-spec/certs` 로 복사(privkey 600)하고 nginx 를 reload 한다.
   ```bash
   sudo install -o root -g root -m 755 docker/nginx/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/task-spec.sh
   # IP 주소 인증서: certbot 5.4 이상(snap), 6일짜리 shortlived 프로필만 발급됨
   sudo certbot certonly --preferred-profile shortlived --webroot -w /etc/task-spec/acme \
        --ip-address <공인 IP> --cert-name task-spec -m <알림 메일> --agree-tos -n
   # 도메인 인증서: --ip-address 대신 -d <도메인>, --preferred-profile 생략 가능
   ```
   레포의 hook 파일을 그대로 등록하지 않는다 — 사용자가 고칠 수 있는 파일을 root 가 실행하게 된다.
4. IP 인증서(6일)는 갱신 시점을 정한다. 기본값(만료 30일 전)이면 갱신 타이머가 돌 때마다 새로 발급해 주 5회 제한에 걸린다.
   ```bash
   sudo sed -i '/^\[renewalparams\]/i renew_before_expiry = 4 days' /etc/letsencrypt/renewal/task-spec.conf   # 약 3일마다 갱신
   ```
5. TLS 모드로 다시 띄운다.
   ```bash
   podman rm -f task-nginx
   TASK_NGINX_CERTS=/etc/task-spec/certs TASK_NGINX_AUTH=/etc/task-spec/htpasswd \
     [TASK_NGINX_ALLOW=/etc/task-spec/allow.conf] docker/nginx/run.sh
   ```

사내 CA·구매한 공인 인증서라면 3~4 대신 `/etc/task-spec/certs/{fullchain,privkey}.pem` 에 직접 둔다 (디렉토리 700, privkey 600).

nginx 설정(`docker/nginx/nginx.conf`, HTTP 단계는 `nginx.http.conf`) 요점: HTTP→HTTPS(certbot 확인 경로만 예외), TLS 1.2+, HSTS·보안 헤더,
모든 경로 Basic 인증(`/healthz`·certbot 확인 경로만 예외), SSE(`/runs/{id}/events`) 버퍼링 끔·1시간, 일반 요청 600초(과제 생성),
업로드 25 MB, IP 당 10 r/s 제한(순간 20), 접근 로그에 쿼리·본문 없음, Basic 인증 헤더는 앱으로 넘기지 않음.
`run.sh` 가 만드는 설정 조각(upstream·빈 허용 목록)은 `/etc/task-spec/nginx/` 에 둔다 — `/tmp` 에 두면 재부팅 뒤 재시작이 실패한다.
`TASK_NGINX_NETWORK=bridge` 는 예전 방식(`task-net`, 포트 전달) — 접속자 IP 를 모르게 되므로 쓰지 않는다.

### 2-7. 과제 전달 묶음 폴더 (`IMAGES_DIR`) — #115 2-a

과제 묶음(응시용·채점용 이미지 tar, 설명 파일)은 **호스트 사용자**(적재 스크립트, 나중에 builder #129)가 쓰고, API(#116 전달)가 읽는다.
그래서 `task_data` 볼륨이 아니라 호스트 디렉터리에 두고, API 에는 **같은 절대 경로로 읽기 전용** 마운트한다 (`docker/api/run.sh`).

- 묶음 파일은 소유자 전용(파일 0600·폴더 0700)으로 쓴다 — 채점용에는 숨은 테스트가 들어 있다.
- rootless podman 에서 API 컨테이너의 app(uid 10001)은 기본 매핑이면 호스트 하위 uid(예: 110000)가 되어 이 파일을 못 읽는다.
  `run.sh` 는 rootless 면 `--userns=keep-id:uid=10001,gid=10001` 로 app 을 호스트 사용자로 맞춘다 (런타임 runc 라 keep-groups 는 못 쓰고,
  SELinux 가 꺼져 있어 라벨 옵션은 필요 없다 — 2026-11-12 운영 서버 확인).
- keep-id 로 바꾸면 `task_data` 볼륨(지원자 데이터)의 기존 파일 주인(호스트 하위 uid)이 컨테이너 안에서 app 이 아니게 된다 →
  **한 번** 볼륨 주인을 호스트 사용자로 옮긴다. 주인이 맞지 않으면 `run.sh` 가 기동을 거부한다.

처음 한 번 (운영 서버, 배포 사용자로 — 디렉터리 만들기만 sudo):

```bash
# 0) 지금 상태 기록 (되돌릴 때 비교용)
podman exec task-api ls -ld /data /data/* | head
vol=$(podman volume inspect task_data --format '{{.Mountpoint}}'); stat -c '%u %a %n' "$vol"

# 1) 묶음 디렉터리 — 배포 사용자 소유, 다른 사용자 접근 없음
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 700 /srv/task-spec
install -d -m 700 /srv/task-spec/images

# 2) API 를 멈추고 기존 묶음을 옮긴다 (레포 data/images/<build_id>/ — 빌드 저장소에는 경로가 아니라 파일 이름·해시만 있어 옮겨도 된다)
podman rm -f task-api
mv <레포>/data/images/* /srv/task-spec/images/          # 다른 파일시스템이면 cp -a 후 sha256sum -c 로 확인하고 지운다
for d in /srv/task-spec/images/*/*/; do (cd "$d" && sha256sum -c --quiet SHA256SUMS) || echo "불일치: $d"; done

# 3) task_data 볼륨 주인을 호스트 사용자로 (unshare 안의 0 = 호스트 사용자) — 한 번만
podman unshare chown -R 0:0 "$vol"

# 4) 설정 — API env 와 호스트 .env(적재 스크립트가 읽음)에 같은 경로, 따옴표 없이
echo 'IMAGES_DIR=/srv/task-spec/images' >> docker/api/.env
#   레포 .env 의 IMAGES_DIR 도 같은 값으로 (주석 해제)

# 5) 기동
TASK_API_IMAGE=task-api:<버전> docker/api/run.sh
```

확인:

```bash
podman exec task-api sh -c 'id; ls -ld /data; ls /srv/task-spec/images | head'          # uid=10001, /data 읽힘, 묶음 목록
podman exec task-api python -c "import json,glob; f=sorted(glob.glob('/srv/task-spec/images/*/candidate/candidate.json'))[0]; print(json.load(open(f))['build_id'])"
podman exec task-api sh -c 'touch /srv/task-spec/images/x' && echo "쓰기 되면 안 됨" || echo "읽기 전용 ok"
curl -s http://127.0.0.1:8001/healthz     # database ok
# 실행 하나 (가상 지원서) — /data 에 업로드가 써지는지: §3 의 POST /runs
```

되돌리기 (keep-id 이전으로):

```bash
podman rm -f task-api
podman unshare chown -R 10001:10001 "$vol"     # 볼륨 주인을 기본 매핑의 app 으로
# 이전 버전의 run.sh(keep-id 없음)로 기동. 묶음은 /srv 에 그대로 두되 API 는 못 읽는다 (#116 전)
```

builder(#129)가 다른 호스트 사용자로 쓰게 되면 소유자 전용 권한으로는 API 가 못 읽는다 — 그때(2-b) 권한 모드를 0640/0750 으로 바꾸고
기본 ACL(`u:<배포 사용자>:rX`)을 건다. 0600 으로 chmod 하면 ACL 마스크가 비어 ACL 이 꺼지므로 둘을 같이 바꾼다.

## 3. 확인

```bash
curl -s http://127.0.0.1:8001/healthz     # status ok, database ok, ontology_resolved = 고정 버전
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/ui/   # 200 — 임시 화면 (UI_ENABLED=false 면 404)
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
2-7 이전 버전에서 올라올 때는 2-7 의 1)~4) 를 먼저 한다 (`run.sh` 가 볼륨 주인이 맞지 않으면 기동을 거부한다). 2-7 이후 버전에서 그 이전으로 내릴 때는 2-7 의 되돌리기.

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
| nginx 502 | `curl http://127.0.0.1:8001/healthz` — `task-api` 가 로컬 포트로 떠 있는지 (`TASK_API_BIND=none` 이면 안 됨), `podman logs task-api` |
| 인증하면 500 | `podman logs task-nginx` 에 htpasswd `Permission denied` — `run.sh` 로 다시 띄우면 권한(640, 그룹 = 컨테이너 nginx)을 맞춘다 |
| 바깥에서 접속 안 됨 | 접근 로그에 요청이 찍히는지. 안 찍히면 ufw(`sudo ufw status`)·공유기 포트 포워딩. 서버 안에서 공인 IP 로의 접속은 공유기에 따라 안 될 수 있음 |
| certbot `Error getting validation data` | 위와 같음 — 바깥에서 80 의 `/.well-known/acme-challenge/` 가 열리는지. 실패는 시간당 5회 제한이 있으니 확인 뒤 재시도 |
| 접근 로그 IP 가 전부 `10.89.0.x` | nginx 가 bridge 방식으로 떴다 — `TASK_NGINX_NETWORK` 기본값(host)으로 다시 띄움 |
| 진행 상황(SSE)이 늦게·한꺼번에 옴 | nginx 설정의 events 경로 `proxy_buffering off` 가 적용됐는지 (`podman exec task-nginx nginx -T`) |
| nginx 가 443 에 못 붙음 | rootless podman 특권 포트 — `sysctl net.ipv4.ip_unprivileged_port_start=80` (root 로 띄우지 않는다) |
