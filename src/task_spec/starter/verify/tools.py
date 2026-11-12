"""검증기가 쓰는 도구·캐시 경로 — 환경 변수로 바꾼다 (builder 컨테이너 #129 는 이미지 안 경로를 준다).

| 환경 변수 | 뜻 | 기본 |
|---|---|---|
| `BUILD_TOOLS_DIR` | 아래 기본 경로들의 루트 (git 밖) | `data` (작업 디렉터리 기준) |
| `GRADLE_BIN` | Gradle 실행 파일 | `<BUILD_TOOLS_DIR>/feature_starters/.tools/dist/gradle-8.10.2/bin/gradle` |
| `GRADLE_USER_HOME` | Gradle 의존성 캐시 | `<BUILD_TOOLS_DIR>/.gradle-home` |
| `NODE_MODULES_DIR` | 쓸 node_modules 하나 (주면 설치 안 함) | `<BUILD_TOOLS_DIR>/.node-modules/<템플릿>@v<n>-<lock 해시>/` |
| `PY_ENV_DIR` | 쓸 가상 환경 하나 (주면 설치 안 함) | `<BUILD_TOOLS_DIR>/.py-envs/<템플릿>@v<n>-<lock 해시>/` |

지원자 데이터 루트(`DATA_DIR`)와 일부러 나눴다 — builder 는 지원자 데이터를 받지 않는다 (#115 보안 경계).
값은 부를 때마다 환경에서 읽고 절대 경로로 바꾼다 (Gradle 은 조립 폴더를 cwd 로 돌기 때문).
"""

from __future__ import annotations

import os
from pathlib import Path

GRADLE_DIST = Path("feature_starters") / ".tools" / "dist" / "gradle-8.10.2" / "bin" / "gradle"


def tools_dir() -> Path:
    return Path(os.environ.get("BUILD_TOOLS_DIR") or "data").resolve()


def gradle_bin() -> Path:
    env = os.environ.get("GRADLE_BIN")
    return Path(env).resolve() if env else tools_dir() / GRADLE_DIST


def gradle_user_home() -> Path:
    env = os.environ.get("GRADLE_USER_HOME")
    return Path(env).resolve() if env else tools_dir() / ".gradle-home"


def node_cache() -> Path:
    return tools_dir() / ".node-modules"


def py_cache() -> Path:
    return tools_dir() / ".py-envs"
