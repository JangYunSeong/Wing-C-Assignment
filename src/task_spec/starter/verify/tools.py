"""검증기가 쓰는 도구·캐시 경로 — 환경 변수로 바꾼다 (builder 컨테이너 #129 는 이미지 안 경로를 준다).

Gradle 실행 파일·가상 환경은 `VERIFY_ISOLATION=host` 일 때만 쓴다 — 기본(컨테이너)은 검증용 템플릿 층 안의 것을 쓴다 (#127).
`BUILD_TOOLS_DIR` 는 컨테이너 모드에서도 검증용 층의 빌드 맥락·로그와 Node 의존성 캐시 자리다.

| 환경 변수 | 뜻 | 기본 |
|---|---|---|
| `BUILD_TOOLS_DIR` | 아래 기본 경로들의 루트 (git 밖) | 레포 루트의 `data/` (옮기기 전과 같음 — 어느 디렉터리에서 실행해도) |
| `GRADLE_BIN` | Gradle 실행 파일 (경로 없이 이름만 주면 PATH 에서 찾는다) | `<BUILD_TOOLS_DIR>/feature_starters/.tools/dist/gradle-8.10.2/bin/gradle` |
| `GRADLE_USER_HOME` | Gradle 의존성 캐시 | `<BUILD_TOOLS_DIR>/.gradle-home` |
| `NODE_MODULES_DIR` | 쓸 node_modules 하나 (주면 설치 안 함) | `<BUILD_TOOLS_DIR>/.node-modules/<템플릿>@v<n>-<lock 해시>/` |
| `PY_ENV_DIR` | 쓸 가상 환경 하나 (주면 설치 안 함) | `<BUILD_TOOLS_DIR>/.py-envs/<템플릿>@v<n>-<lock 해시>/` |

지원자 데이터 루트(`DATA_DIR`)와 일부러 나눴다 — builder 는 지원자 데이터를 받지 않는다 (#115 보안 경계).
값은 부를 때마다 환경에서 읽고 절대 경로로 바꾼다 (Gradle 은 조립 폴더를 cwd 로 돌기 때문). builder 이미지처럼 레포 밖에 설치되면
`BUILD_TOOLS_DIR` 를 준다 (#129).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]  # src/task_spec/starter/verify/tools.py → 레포
GRADLE_DIST = Path("feature_starters") / ".tools" / "dist" / "gradle-8.10.2" / "bin" / "gradle"


def tools_dir() -> Path:
    env = os.environ.get("BUILD_TOOLS_DIR")
    return Path(env).resolve() if env else REPO_ROOT / "data"


def gradle_bin() -> Path:
    env = os.environ.get("GRADLE_BIN")
    if not env:
        return tools_dir() / GRADLE_DIST
    return (
        Path(env).resolve() if os.sep in env else Path(env)
    )  # 이름만이면 PATH 에서 (subprocess 가 찾음)


def gradle_user_home() -> Path:
    env = os.environ.get("GRADLE_USER_HOME")
    return Path(env).resolve() if env else tools_dir() / ".gradle-home"


def node_cache() -> Path:
    return tools_dir() / ".node-modules"


def py_cache() -> Path:
    return tools_dir() / ".py-envs"
