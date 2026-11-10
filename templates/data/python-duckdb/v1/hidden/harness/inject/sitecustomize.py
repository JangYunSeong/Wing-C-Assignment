"""채점기 주입 (크래시 시나리오에서만 PYTHONPATH 로 들어간다): 파이썬이 무시하도록 바꿔 둔 SIGXFSZ 를 기본 동작으로 되돌린다.

그러면 파일 크기 상한(RLIMIT_FSIZE)을 넘는 첫 쓰기에서 프로세스가 그 자리에서 죽는다 — 정리 코드도 돌지 않는다.
대상 코드는 바꾸지 않는다.
"""

import signal

signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
