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
모든 경로 Basic 인증(`/healthz`·certbot 확인 경로·Wing 응시 쪽만 예외 — 아래 Wing 문단), SSE(`/runs/{id}/events`) 버퍼링 끔·1시간, 일반 요청 600초(과제 생성),
업로드 25 MB, IP 당 10 r/s 제한(순간 20), 접근 로그에 쿼리·본문 없음, Basic 인증 헤더는 앱으로 넘기지 않음.
CORS: 다른 origin 의 프론트가 브라우저에서 직접 부를 때만. 허용 origin 은 `docker/nginx/.allow_list`(gitignore, `TASK_NGINX_CORS` 로 경로 변경)에
한 줄에 하나(`http://203.0.113.5:3000`, 끝 `/` 없이) — 파일이 없으면 CORS 헤더를 내지 않는다. preflight(`OPTIONS`)는 인증 전에 204 로 끝낸다.
목록을 바꾸면 `podman rm -f task-nginx` 후 `run.sh` 를 다시 실행한다 (run.sh 가 nginx map 조각으로 바꿔 마운트).
이미 CORS 조각을 마운트해 띄운 컨테이너라면 `.allow_list` 와 `/etc/task-spec/nginx/cors_origins.conf`(`"<origin>" $http_origin;` 줄)에 같이 넣고
`podman exec task-nginx nginx -s reload` 로도 된다 — 끊김 없음. 운영 Wing 관리 화면(admin-fe)은 443 같은 출처라 CORS 가 필요 없고,
로컬 개발 `http://localhost:5174` 만 넣는다.
CORS 로 부르는 브라우저는 443 에도 로그인돼 있어야 한다 (다른 origin 요청에는 로그인 창이 뜨지 않는다 — 먼저 443 을 열어 로그인하거나 프론트가 `Authorization` 헤더를 보낸다).
`run.sh` 가 만드는 설정 조각(upstream·빈 허용 목록·CORS origin)은 `/etc/task-spec/nginx/` 에 둔다 — `/tmp` 에 두면 재부팅 뒤 재시작이 실패한다.
`nginx.conf` 는 **파일 하나**를 바인드 마운트한다. `git pull`·`sed -i` 처럼 파일을 새로 만드는 작업 뒤에는 컨테이너가 옛 파일을 계속 보므로
reload 로는 바뀌지 않는다 — `podman exec task-nginx md5sum /etc/nginx/nginx.conf` 를 호스트 파일과 비교하고, 다르면 `podman restart task-nginx`.

Wing 코딩 테스트(`Wing-C-TEST`, pod `wing`)도 같은 nginx 가 받는다 (2026-11-20 부터) — 443 의 `/` 는 `wing-web`(`127.0.0.1:8080`)에,
task-api 는 같은 443 의 **`/task-api/`** 가 정식 주소다(앞의 `/task-api` 를 떼고 넘김, Wing 의 `TASK_API_BASE_URL`·`WING_TASK_API_URL` = `https://<공인 IP>/task-api`).
옛 루트 API 경로(`/builds`·`/runs`·`/postings`·`/pull/` 등 앱의 최상위 경로, `/openapi.json`·`/docs`)는 옛 주소를 쓰는 곳(팀원 로컬 Wing, 받는 쪽 `/pull`)이
옮길 때까지 남겨 두었다 — Wing 화면 경로(`/`·`/about`·`/exam-sessions/…`·`/admin/`·`/api/`·`/assets/`)와 겹치지 않는다. 앱에 최상위 경로를 더하면 `/task-api/` 로만 쓴다.
`/healthz` 는 컨테이너 헬스체크라 루트에 계속 둔다. 옛 Wing 주소 `https://<공인 IP>:8443` 은 443 으로 301.
Wing 은 **응시 쪽만 Basic 인증 없이** 연다 — 응시 화면(`/`)·관리 화면 정적 파일(`/admin`)·응시 API(`/api/exams`·`/api/exam-sessions/…`,
세션 API 는 Wing 앱이 `X-Exam-Session-Token` 으로 막음). 그 밖의 Wing API(`/api/admin/…`·`/api/users` 등)는 Basic 인증 —
Wing backend 에는 로그인·가드가 없어서 이 잠금이 관리 데이터(리포트·평가 로그)의 유일한 문이다.
nginx 에는 **열 경로만** 적는다: nginx location 은 대소문자를 가리고 Wing backend(Express)는 안 가려서, 막을 경로를 적으면
`/api/Admin/…` 처럼 대소문자만 바꿔 우회된다. 응시 화면이 공개라 누구나 응시 세션을 만들 수 있다 (IP 당 요청 제한뿐 — 세션 수 상한 없음).
미리보기 서브도메인(`p200NN.<공인 IP 대시>.sslip.io:8443`)은 인증 없이 `127.0.0.1:200NN` 에 넘긴다 (인증서 `/etc/task-spec/certs/wing-preview/`,
발급·갱신 훅은 Wing 레포 `.claude/docs/tooling.md`). 갱신 훅은 모든 인증서 갱신에 돌기 때문에, 이 레포의 hook 은 `task-spec` 인증서
(`TASK_CERT_NAME`)일 때만 복사·reload 한다. 미리보기 때문에 방화벽·공유기에서 8443/tcp 도 연다.
`TASK_NGINX_NETWORK=bridge` 는 예전 방식(`task-net`, 포트 전달) — 접속자 IP 를 모르게 되므로 쓰지 않는다.

### 2-7. 과제 전달 묶음 폴더 (`IMAGES_DIR`) — #115 2-a

과제 묶음(응시용·채점용 이미지 tar, 설명 파일)은 **호스트 사용자**(적재 스크립트, 나중에 builder #129)가 쓰고, API(#116 전달)가 읽는다.
그래서 `task_data` 볼륨이 아니라 호스트 디렉터리에 두고, API 에는 **같은 절대 경로로 읽기 전용** 마운트한다 (`docker/api/run.sh`).

- 묶음 파일은 소유자 전용(파일 0600·폴더 0700)으로 쓴다 — 채점용에는 숨은 테스트가 들어 있다.
- rootless podman 에서 API 컨테이너의 app(uid 10001)은 기본 매핑이면 호스트 하위 uid(예: 110000)가 되어 이 파일을 못 읽는다.
  `run.sh` 는 rootless 면 `--userns=keep-id:uid=10001,gid=10001` 로 app 을 호스트 사용자로 맞춘다 (런타임 runc 라 keep-groups 는 못 쓰고,
  SELinux 가 꺼져 있어 라벨 옵션은 필요 없다 — 2026-11-12 운영 서버 확인).
- 과제 생성 검증·채점 컨테이너의 마운트 정책(`starter/mounts.py`, #174)은 SELinux 가 켜진 서버 기준이다 — 격리를 켠 채 실행 사본은 전용 라벨 `:Z`,
  함께 쓰는 캐시(Node 검증 node_modules)는 공유 라벨 `:z`. 운영 서버는 SELinux 가 꺼져 있어 동작 변화가 없다.
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
기본 ACL(`u:<배포 사용자>:rX`)을 건다. 0600 으로 chmod 하면 ACL 마스크가 비어 ACL 이 꺼지므로 둘을 같이 바꾼다 (2-8).

### 2-8. 과제 생성 builder (`task-builder`) — #129

명세 승인과 과제 생성은 따로다 — 승인자가 화면에서 "과제 생성"을 누르면 큐(`common_task_build`)에 들어가고, builder 가 하나씩 가져가
계약 → 시작 코드·테스트 → 응시용·채점용 이미지 → 묶음(승인 전 `approval=pending`)까지 만든다. API 는 빌드하지 않는다.

경계 (#115):
- **builder 전용 호스트 사용자**(예: `task-builder`)의 rootless podman — 그 사용자로는 task-api·task-db 컨테이너와 `task_data` 볼륨이 보이지 않는다.
- **builder 전용 DB 역할**(`task_builder`, `docker/builder/db-role.sql`) — 공통 과제 읽기, 빌드 표 쓰기만. 체크포인트(지원자 파생물)에는 권한이 없다.
  builder 는 표를 만들거나 바꾸지 않는다 (표·칸은 API 가 기동할 때) → **API 를 새 버전으로 먼저** 띄운다.
  builder 는 기동할 때 자기가 읽는 빌드 표 칸 전체(`store.BUILD_COLUMNS`)가 있는지 보고, 하나라도 없으면 기동에서 멈춘다 (#175 —
  예전에는 일부 칸만 봐서 기동은 통과하고 첫 생성 요청에서 죽었다. 자동 업그레이드는 기동 로그만 보고 성공으로 처리했다).
  빌드 출처 칸 `origin`(#175)은 API 기동 때 생기고, 있던 빌드는 요청 시각이 있으면 `request`, 없으면 `eval` 로 채워진다.
  매니페스트 해시 칸 `manifest_sha256`(#176 — 같은 vN 에서 고칠 수 있는 검사·수리 칸의 판)도 API 기동 때 생긴다 (있던 빌드는 NULL). 이 칸이 생긴 뒤의 builder 는 칸이 없으면 기동에서 멈추므로 **API 를 먼저** 올린다.
  생성기 provenance 칸 `provenance`(#233 — 계약·빌드 단계 LLM 호출마다 프롬프트 판·sha·모델·설정·비용, 실패한 빌드도)도 마찬가지로 API 기동 때 생기고(있던 빌드는 빈 목록) **API 를 먼저** 올린다.
  builder 역할 권한은 표 단위(`GRANT … ON public.common_task_build`)라 새 칸 때문에 `db-role.sql` 을 다시 돌리지 않아도 된다.
  **새 표는 다르다** — 회사 결정적 사건 표 `company_incident`(2026-11-19, 빌더가 회사 사례 재료를 읽음)는 API 를 먼저 올려 표를 만든 뒤
  `db-role.sql` 을 **다시 돌려** 읽기 권한을 준다. 권한이 없으면 회사 사례(`co:…`)로 만든 과제의 빌드만 계약 단계에서 사유와 함께 실패한다.
- LLM 이 만든 코드는 builder 사용자의 podman 에서 `--network none` 자식 컨테이너로만 돈다 (#127). 이미지에는 JDK·Gradle·Node 가 없다.
- 묶음은 builder 가 쓰고(권한 0640/0750) API(배포 사용자, keep-id)가 기본 ACL 로 읽는다. 0600 이면 ACL 마스크가 비어 못 읽는다.

처음 한 번 (sudo 가 필요한 줄은 `sudo`):

```bash
# 1) 전용 사용자 — 로그아웃 뒤에도 소켓·컨테이너가 돌게 linger. 하위 uid 범위가 있어야 rootless podman 이 돈다
sudo useradd -m -s /bin/bash task-builder
sudo loginctl enable-linger task-builder
grep task-builder /etc/subuid /etc/subgid          # 없으면: sudo usermod --add-subuids 200000-265535 --add-subgids 200000-265535 task-builder

# 2) 폴더 — 작업·도구는 builder 소유, 묶음 폴더(2-7)는 배포 사용자 소유 그대로 두고 ACL 로 builder 쓰기 + 배포 사용자 읽기 상속
sudo install -d -o task-builder -g task-builder -m 700 /srv/task-spec/builder /srv/task-spec/builder/work /srv/task-spec/builder/tools
sudo setfacl -m u:task-builder:rx /srv/task-spec    # 지나가기 + 목록 — 2-10 의 systemd path 유닛이 위쪽 폴더까지 감시하려면 읽기가 있어야 한다
sudo setfacl -m u:task-builder:rwx /srv/task-spec/images
sudo setfacl -d -m u:task-builder:rwx,u:<배포 사용자>:rx /srv/task-spec/images    # 새 묶음: builder 가 쓰고 배포 사용자(=API)가 읽음
getfacl /srv/task-spec/images

# 3) DB 역할 (표 주인으로, API 를 새 버전으로 한 번 띄운 뒤) — 비밀번호는 셸 기록에 남지 않게 \password 로
podman exec -i task_db psql -U task -d task_db < docker/builder/db-role.sql
podman exec -it task_db psql -U task -d task_db -c '\password task_builder'

# 4) builder 사용자로 — 소켓·이미지·설정 파일·기동 (반입 묶음 폴더를 그 사용자가 읽을 수 있는 곳에 둔다)
sudo -iu task-builder
# sudo -iu 셸에는 사용자 세션 변수가 없어 systemctl --user 가 "Failed to connect to bus" — linger 로 떠 있는 세션을 가리킨다 (한 번 .bashrc 에)
echo 'export XDG_RUNTIME_DIR=/run/user/$(id -u); export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus' >> ~/.bashrc && . ~/.bashrc
systemctl --user enable --now podman.socket
podman load -i <반입 묶음>/images.tar                     # task-builder:<버전> (이 사용자 저장소에)
install -m 600 <반입 묶음>/docker/builder/env.example ~/builder-settings
vi ~/builder-settings                                     # task_builder 비밀번호·LLM 키·폴더 (따옴표 없이)
TASK_BUILDER_ENV=~/builder-settings TASK_BUILDER_IMAGE=task-builder:<버전> <반입 묶음>/docker/builder/run.sh
```

확인:

```bash
podman logs --tail 20 task-builder                        # "builder 시작 (worker …, 작업당 $0.50, 하루 $5.00)"
podman ps --format '{{.Names}}'                           # (builder 사용자) task-api·task_db 가 보이지 않아야 함
# 화면에서 승인된 기능형 공통 과제의 "과제 생성" → 빌드 화면에서 단계(contract·build·images·package) → review
getfacl /srv/task-spec/images/<build_id>/candidate         # user:<배포 사용자>:r-x, mask::r-x
podman exec task-api ls /srv/task-spec/images/<build_id>/candidate   # (배포 사용자) API 가 읽힘
```

- 첫 빌드는 검증용 템플릿 층(Java 약 0.7GB·Python 약 0.5GB)과 Node 의존성 캐시를 builder 사용자 저장소·`/srv/task-spec/builder/tools` 에 만든다 — 몇 분, 그때만 인터넷 필요.
- 실패한 빌드의 작업 폴더(`/srv/task-spec/builder/work/<build_id>`, 생성 코드 평문 — git·외부 반출 금지)는 원인 확인용으로 남는다.
  `verify/*.log` = 검증 실행마다의 결과, `overlays/<키>/` = 수리 루프가 실패했을 때의 마지막 생성물(시작 코드·숨은·공개 테스트·흔한/허용 구현,
  알파 실행 폴더의 overlays/ 와 같은 모양), `error.txt` = 예상 못 한 예외. builder 사용자 폴더라 볼 때는
  `sudo cp -r /srv/task-spec/builder/work/<build_id> <내 폴더> && sudo chown -R <나>: <내 폴더>/<build_id>`. 일주일 지난 것 정리 [제안]:
  `find /srv/task-spec/builder/work -mindepth 1 -maxdepth 1 -mtime +7 -exec rm -rf {} +`
- 업그레이드: `podman stop task-builder` (지금 작업 뒤 멈춤, 60초 넘으면 강제 — lease 가 지나 다음 기동 때 다시 대기열로. 묶음을 등록하던
  중이었으면 다시 하지 않고 실패 `lease_expired` — 다시 요청) → `podman rm task-builder` → 새 이미지 `podman load` → `run.sh`.
- 비용 상한(작업당·하루)과 모델은 builder 설정 파일 — 모델을 바꾸면 상한도 같이 올린다.
- 승인된 빌드의 묶음 확정(descriptor approved·평가 키트 `eval_kit/`)도 builder 가 한다 (#130) — 새 작업을 가져가기 전마다, 몇 초.
  빌드 화면에 "묶음 확정 대기"가 오래 남으면 builder 로그와 빌드의 사유(`finalize: …`)를 본다. 전달은 확정된 빌드만.

### 2-9. 재부팅 때 다시 띄우기 — 배포 사용자·`task-builder` 각각

컨테이너는 `--restart unless-stopped` 라 죽으면 podman 이 다시 띄우지만, **서버 재부팅 뒤에는** rootless podman 이 사용자 서비스
`podman-restart.service` 로만 다시 띄운다. 그 유닛은 `restart-policy=always` 만 띄우므로(podman 4.9) `unless-stopped` 줄을 더한다.
(`podman update --restart` 는 podman 5 부터 — 4.9 에서는 컨테이너를 다시 만들지 않고 이 방법으로.) 사용자마다 한 번, sudo 없이:

```bash
mkdir -p ~/.config/systemd/user/podman-restart.service.d
cat > ~/.config/systemd/user/podman-restart.service.d/unless-stopped.conf <<'CONF'
[Service]
ExecStart=/usr/bin/podman $LOGGING start --all --filter restart-policy=unless-stopped
CONF
systemctl --user daemon-reload
systemctl --user enable podman-restart.service
systemctl --user start podman-restart.service        # 시험 — 떠 있는 컨테이너는 그대로, 멈춘 것은 다시 뜬다
```

- linger 가 켜져 있어야 로그인 없이 부팅 때 사용자 서비스가 돈다 (`loginctl show-user <사용자> -p Linger` → yes).
- API 가 DB 보다 먼저 떠서 실패해도 restart 정책으로 다시 붙는다. `podman stop` 으로 일부러 멈춘 컨테이너도 재부팅 때는 다시 뜬다.

### 2-10. builder 자동 업그레이드 — 배포 사용자가 넣으면 task-builder 가 스스로

배포할 때마다 `sudo -iu task-builder` 로 들어가지 않게, **받는 폴더**(`/srv/task-spec/builder-inbox`)에 배포 사용자가 이미지·`run.sh`·체크섬을
넣으면 task-builder 의 systemd path 유닛이 업그레이드한다 (`docker/builder/upgrade.sh`): 체크섬 → `podman load` → 지금 builder 정지
(지금 작업이 끝날 때까지, 최대 60분 — `TASK_BUILDER_DRAIN_SECONDS`) → 새 `run.sh` 로 기동 → "builder 시작" 로그 확인 → **실패면 이전 이미지·run.sh 로 되돌림**. 결과는 받는 폴더의
`last-upgrade.json`. 설정 파일(`~/builder-settings`, 비밀값)은 task-builder 홈에만 있고 건드리지 않는다. 배포 사용자는 받는 폴더에 쓰기만 —
builder 작업 폴더·설정은 여전히 못 본다.

처음 한 번 (sudo 없음 — `/srv/task-spec` 는 배포 사용자 소유):

```bash
# 배포 사용자 — task-builder 가 /srv/task-spec 를 읽을 수 있어야 한다 (x 만이면 path 유닛이 감시를 못 걸고 아무 기록 없이 반응하지 않는다)
setfacl -m u:task-builder:rx /srv/task-spec
# 받는 폴더 (배포 사용자·task-builder 둘 다 읽고 쓰고, 새 파일도 그대로 상속)
mkdir -m 770 /srv/task-spec/builder-inbox
setfacl -m u:task-builder:rwx /srv/task-spec/builder-inbox
setfacl -d -m u:task-builder:rwx,u:$(id -un):rwx /srv/task-spec/builder-inbox
docker/builder/deliver.sh --setup            # 설치 파일을 받는 폴더 setup/ 에

# task-builder 사용자 — 유닛 설치·켜기 (지금 떠 있는 builder 의 run.sh 를 되돌리기용으로 둔다)
sudo -iu task-builder
bash /srv/task-spec/builder-inbox/setup/install-upgrader.sh   # active 가 나오면 됨 (sh 로 돌리면 'Illegal option -o pipefail')
```

그다음부터 배포 (배포 사용자, 레포 루트 — 커밋된 상태로):

```bash
docker/builder/deliver.sh    # 이미지 빌드·금지 파일 검사 → 받는 폴더 → last-upgrade.json 이 ok 면 끝 (지금 작업이 있으면 끝난 뒤, 최대 75분 기다림)
```

- 정지는 builder 가 **지금 작업을 끝낼 때까지** 기다린다 (SIGTERM → 새 작업을 잡지 않고 지금 것만 끝냄, 최대 60분). 넘으면 강제 정지하고,
  끊긴 빌드는 임대가 지나면 다시 대기열로 간다 (시도가 남았을 때 — `build_max_attempts`. 묶음을 등록하던 중에 끊겼으면 다시 하지 않고
  실패 `lease_expired` — 다시 요청, #244). 기다리는 동안 새 빌드 요청은 대기로 쌓였다가 새 builder 가 처리한다.
  컨테이너의 `--stop-timeout 60`(`run.sh`)은 재부팅 같은 평소 정지용이라 그대로 둔다 (2026-11-13: 60초만 기다려 시작 코드 단계 빌드가 끊겼다).

- 실패하면 `last-upgrade.json` 의 `message` (체크섬·불러오기·기동 실패·되돌림). 자세한 건 task-builder 로 `journalctl --user -u task-builder-upgrade`·`podman logs task-builder`.
- `upgrade.sh`·유닛을 바꾸면 `deliver.sh --setup` 뒤 task-builder 로 `install-upgrader.sh` 를 다시 (자기 자신은 자동으로 바꾸지 않는다).
  빠뜨리면 서버에 옛 판이 남는다 — 2026-11-13: 설치된 유닛이 `KillMode=process` 이전 판이라, 업그레이드가 끝나며 새 builder 에도 SIGTERM 이 가
  잡은 작업 하나만 하고 멈췄다 (컨테이너는 떠 보이는데 일을 안 받음). 설치된 판은 `systemctl --user cat task-builder-upgrade.service` 로 확인.
- builder 설정 값(`~/builder-settings`)을 바꿀 때는 지금처럼 task-builder 로 고치고 다시 만든다 (`podman restart` 는 env 파일을 다시 읽지 않는다).

### 2-11. SFTP 전달 대상 — #116

승인된 빌드 묶음은 **API 가** SFTP 로 보낸다 (builder 는 만들기만 — 생성 코드·podman 소켓 옆에 전달 키를 두지 않는다).
받는 곳 접속 정보는 DB 가 아니라 API env 파일에 둔다. DB `delivery_target` 에는 이름·역할만 있다.

- 원격 경로: `<REMOTE_DIR>/<common_id>-r<판>/<role>/` — `*.tar.zst`·`<role>.json`·`SHA256SUMS` 를 `.part` 로 올려 이름을 바꾸고, 마지막에 빈 `.done`.
  받는 쪽은 `.done` 이 있는 폴더만 가져가고 `sha256sum -c SHA256SUMS` 로 확인한다.
- **다시 보내기는 막는다** — 원격에 `.done` 이 있으면 보내지 않는다 (`already_delivered`). 다시 보내려면 받는 쪽에서 폴더를 지운다.
- 키 인증만 (비밀번호 없음), 호스트 키는 `known_hosts` 로 고정 — 처음 보는 키는 거부(`host_key`).
- 실패 사유는 코드만 남는다: `local_mismatch`·`already_delivered`·`host_key`·`auth`·`timeout`·`io`. `io`·`timeout` 만 한 번 더 시도한다.

처음 한 번 (운영 서버, 배포 사용자):

```bash
# 1) 키 폴더 — 배포 사용자만 (run.sh 가 700 이 아니면 거부)
install -d -m 700 /etc/task-spec/sftp            # /etc/task-spec 가 root 소유면 sudo install -d -o "$(id -un)" …
ssh-keygen -t ed25519 -N '' -f /etc/task-spec/sftp/grader_test_ed25519   # 공개키(.pub)를 받는 곳 계정에 등록
ssh-keyscan -p 22 <받는 호스트> > /etc/task-spec/sftp/known_hosts         # 지문은 받는 쪽 담당자와 따로 맞춰 본다
chmod 600 /etc/task-spec/sftp/*

# 2) API env 파일 (docker/api/.env) — docker/api/env.example 의 SFTP 절. <이름> 은 소문자로 읽힌다
#   SFTP_DIR=/etc/task-spec/sftp
#   SFTP_TARGETS__GRADER_TEST__HOST=… / PORT / USER / KEY_PATH / KNOWN_HOSTS / REMOTE_DIR

# 3) DB 에 대상 등록 (이름·역할) — POST /delivery-targets {"name": "grader_test", "role": "grader"}

# 4) API 다시 만들기 (env 파일은 restart 로 다시 읽지 않는다)
podman rm -f task-api && TASK_API_IMAGE=task-api:<버전> docker/api/run.sh
```

- 운영망에서 API 컨테이너가 받는 곳의 SFTP 포트로 나갈 수 있어야 한다 [미정: 방화벽 확인].

### 2-12. 받는 쪽이 가져가는 전달 (/pull)

SFTP(2-11) 대신 받는 서버가 **HTTPS 와 토큰**으로 묶음을 내려받는다 — 받는 곳에 SSH 계정·공개키 등록·방화벽이 필요 없다.
받는 곳마다 둘 중 하나를 고른다. 토큰 하나가 그 대상의 역할(채점·평가) 하나만 연다.

처음 한 번 (운영 서버):

```bash
# 1) 토큰 — 받는 곳 담당자에게 따로 전달 (메일 본문·채팅에 남기지 않는 편이 낫다)
openssl rand -hex 32
# 2) API env 파일 (docker/api/.env) — <이름> 은 소문자로 읽힌다
#   PULL_TOKENS__GRADER_TEST=<위 값>
# 3) DB 에 대상 등록 (이름·역할) — POST /delivery-targets {"name": "grader_test", "role": "grader"}
# 4) 받는 서버 IP 를 nginx 허용 목록(.allow_list)에 — /pull 도 허용 목록은 거친다 (Basic 인증만 빠짐)
# 5) API·nginx 다시 만들기 (env 는 restart 로 다시 읽지 않는다)
```

받는 쪽 (예):

```bash
H="Authorization: Bearer $TOKEN"; S=https://<서버 주소>
curl -sfH "$H" $S/pull/builds                                  # 가져갈 빌드 — delivered=false 인 것
mkdir -p <build_id> && cd <build_id>
for f in SHA256SUMS <files[].name …>; do curl -sfH "$H" -o "$f" $S/pull/builds/<build_id>/files/$f; done
sha256sum -c SHA256SUMS                                          # SHA256SUMS 는 DB 기록으로 만든다
curl -sfH "$H" -H 'content-type: application/json' \
  -d "{\"sha256sums_sha256\": \"$(sha256sum SHA256SUMS | cut -d' ' -f1)\"}" $S/pull/builds/<build_id>/done
```

- `done` 이 전달 기록(sent)을 남기고 첫 전달이면 빌드가 published 가 된다. 다시 내려받기는 막지 않는다 (기록은 한 번).
- 새 빌드 알림은 없다 — 받는 쪽이 `/pull/builds` 를 주기적으로 본다 [제안: 알림은 받는 쪽이 원하면].
- 토큰을 바꾸려면 env 값을 바꾸고 API 를 다시 만든다. 토큰 유출 시 그 줄을 지우면 그 대상은 바로 막힌다.

### 2-13. 한 번에 배포 — API + builder (`docker/deploy_all.sh`)

배포 사용자로 레포 루트에서 (main 을 받은 뒤). 비밀번호·sudo 가 필요 없다 — API 는 배포 사용자의 컨테이너, builder 는 받는 폴더에 넣으면
task-builder 가 스스로 업그레이드한다 (2-10).

```bash
git pull
docker/deploy_all.sh                  # API 이미지 빌드·교체(헬스체크 실패면 이전 이미지로 되돌림) → builder 이미지 보내기·결과 기다림
docker/deploy_all.sh --api-only       # API 만 · --builder-only 는 builder 만 (= deliver.sh)
```

작업 폴더가 따로면 `TASK_API_ENV` 로 메인 폴더의 API env 파일을 지정한다.

- DB 에 열을 더하는 변경이면 **builder 도 같이** 배포한다 — API 가 기동 때 DDL 을 적용하면 예전 builder 는 연결에 캐시한 준비문(`SELECT *`)으로
  `FeatureNotSupported` 가 난다 (2026-11-15).
- builder 자동 업그레이드 유닛은 **task-builder 사용자에게만** 둔다. 배포 사용자에게도 설치돼 있으면 두 유닛이 같은 배달을 다퉈, 늦은 쪽이
  "체크섬이 맞지 않거나 파일이 빠짐"을 쓰거나 배포 사용자 쪽이 기동에 실패한다 (#199). 확인: 배포 사용자로
  `systemctl --user is-enabled task-builder-upgrade.path` → `disabled` 또는 없음.

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
2-7 이전 버전에서 올라올 때는 2-7 의 1)~4) 를 먼저 한다 (`run.sh` 가 볼륨 주인이 맞지 않으면 기동을 거부한다). 2-7 이후 버전에서 그 이전으로 내릴 때는 2-7 의 되돌리기.

## 5. 정해야 할 것 [미정]

- 앱 인증 (지금은 nginx Basic 인증이 임시 대책 — 승인자 식별이 요청 본문의 reviewer 값뿐)
- DB 백업 주기·보관 위치·기간 (덤프와 `task_data` 볼륨에 지원자 파생물이 있음 — 외부 반출 금지)
- `DATA_DIR`(`task_data` 볼륨) 보관 기간·삭제 정책
  - 채용담당자 사례 인터뷰(#262)만 정해졌다 [확정: 2026-11-19 사용자]: 이름 대응표 `data/interviews/<id>/mapping.json` 은 승인·폐기 때,
    방치 30일(`INTERVIEW_MAPPING_TTL_DAYS`, 업무 저장 기준) 뒤 지우고, 화면 임시 저장 원문(DB `recruiter_interview.ui_drafts`)은 처음 쓴 때부터 하루(`INTERVIEW_DRAFT_TTL_HOURS`) 뒤 지운다.
    API 안 주기 작업(`INTERVIEW_SWEEP_MINUTES`)이 정리한다 — API 를 오래 내려 두면 그동안 정리되지 않는다.

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
