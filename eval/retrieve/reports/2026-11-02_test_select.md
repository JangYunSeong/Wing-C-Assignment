# T1 선택 평가 — 2026-11-02 split=test 

n=20 · 검색 상한(정답이 상위 10 안) 1.0 · runs=2

| 모델 | precision | lenient_P | recall | exact | lenient_exact | none_ok | stability | uncovered_ok | multi_ok | errors | cost_usd |
|---|---|---|---|---|---|---|---|---|---|---|---|
| opencode:qwen3.8-flash | 0.905 | 0.905 | 0.95 | 0.85 | 0.85 | 0.0 | 0.9 | 0.0 | 0.0 | 0 | 0.0 |
| openai:gpt-4.1-mini | 0.76 | 0.76 | 0.95 | 0.65 | 0.65 | 0.0 | 1.0 | 0.0 | 0.0 | 0 | 0.0 |

## 틀린 것

- opencode:qwen3.8-flash g10 gold=[] pick=['comp:ai.service'] uncovered=False | 벡터 검색으로 뽑은 후보를 학습 기반 랭커로 재정렬했다.
- opencode:qwen3.8-flash g34 gold=['comp:infra.scripting', 'comp:infra.ops_automation'] pick=['comp:infra.scripting'] uncovered=False | 반복되던 서버 점검을 쉘 스크립트로 자동화했다.
- opencode:qwen3.8-flash g36 gold=['comp:emb.bsp'] pick=['comp:emb.bsp', 'comp:emb.embedded'] uncovered=False | 신규 보드의 SPI·I2C 디바이스 드라이버를 작성하고 리눅스 커널에 포
- openai:gpt-4.1-mini g02 gold=['comp:data.bigdata'] pick=['comp:data.bigdata', 'comp:data.db_tuning'] uncovered=False | Spark 집계 잡을 파티션 단위로 나눠 6시간 걸리던 집계를 40분으로
- openai:gpt-4.1-mini g06 gold=['comp:data.pipeline'] pick=['comp:data.pipeline', 'comp:infra.monitoring'] uncovered=False | 서비스 로그를 Fluent Bit 로 모아 S3 와 데이터 레이크에 적재
- openai:gpt-4.1-mini g10 gold=[] pick=['comp:ai.service'] uncovered=False | 벡터 검색으로 뽑은 후보를 학습 기반 랭커로 재정렬했다.
- openai:gpt-4.1-mini g30 gold=['comp:infra.iac'] pick=['comp:infra.iac', 'comp:infra.ops_automation'] uncovered=False | Terraform 으로 개발·운영 환경을 코드로 관리하게 바꿨다.
- openai:gpt-4.1-mini g34 gold=['comp:infra.scripting', 'comp:infra.ops_automation'] pick=['comp:infra.scripting'] uncovered=False | 반복되던 서버 점검을 쉘 스크립트로 자동화했다.
- openai:gpt-4.1-mini g36 gold=['comp:emb.bsp'] pick=['comp:emb.bsp', 'comp:emb.embedded'] uncovered=False | 신규 보드의 SPI·I2C 디바이스 드라이버를 작성하고 리눅스 커널에 포
- openai:gpt-4.1-mini g40 gold=['comp:sw.agile'] pick=['comp:si.process', 'comp:sw.agile'] uncovered=False | 매주 팀 회고를 진행하며 개발 절차를 개선했다.