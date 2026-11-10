# {{title}}

{{scenario}}

## 요구 사항

{{requirements}}

## 공개 수용 기준

{{acceptance_public}}

## 명령·파일 계약

{{contract}}

## 제출물

{{deliverables}}

## 코드 구조

- Python 3.11 · DuckDB 1.5 · pyarrow 26 · pandas 2.3 · numpy 2.4 · pytest (의존성은 `pyproject.toml` 의 것만 — 바꾸지 않는다)
- 작업 코드는 `pipeline/` 아래, 진입점은 계약의 명령. 저장소 루트에서 실행된다.

## 실행

```bash
uv sync                      # 의존성 (이미 설치돼 있으면 생략)
python pipeline/run.py --help
python -m pytest -q          # 테스트
```

## 채점 방법

채점 때 이 계약을 기준으로, **같은 형식의 다른 입력 데이터**로 명령을 날짜 순서대로 다시 실행하는 테스트가 추가로 돈다. 샘플의 날짜·값을 코드에 박지 않는다.
명령·옵션·종료 코드·입력·출력 형식은 바꾸지 않는다.

## 진행 방식

{{process}}
