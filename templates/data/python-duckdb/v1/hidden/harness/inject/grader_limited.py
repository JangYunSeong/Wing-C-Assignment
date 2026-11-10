"""채점기 실행기: 파일 크기 상한을 걸고 대상 명령으로 바꿔 탄다 (exec).

    python -I grader_limited.py <bytes> <program> <args...>

preexec_fn 대신 쓰는 이유: 채점기가 여러 스레드에서 하위 프로세스를 띄워서 (preexec_fn 은 스레드와 함께 쓰면 안전하지 않다).
"""

import os
import resource
import sys

limit = int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execv(sys.argv[2], sys.argv[2:])
