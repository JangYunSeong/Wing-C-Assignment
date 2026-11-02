# T1 선택 평가 — 2026-11-02 split=dev 

n=21 · 검색 상한(정답이 상위 10 안) 1.0 · runs=2

| 모델 | precision | lenient_P | recall | exact | lenient_exact | none_ok | stability | uncovered_ok | scope_probe_ok | errors | cost_usd |
|---|---|---|---|---|---|---|---|---|---|---|---|
| opencode:qwen3.8-flash | 0.905 | 0.905 | 1.0 | 0.905 | 0.905 | 0.5 | 1.0 | 0.5 | 1.0 | 0 | 0.0 |
| openai:gpt-4.1-mini | 0.783 | 0.87 | 0.947 | 0.762 | 0.81 | 0.5 | 1.0 | 0.5 | 0.0 | 0 | 0.0 |

## 틀린 것

- opencode:qwen3.8-flash g01 gold=['comp:data.pipeline'] pick=['comp:sw.batch', 'comp:data.pipeline'] uncovered=False | Airflow DAG 를 재구성해 매일 돌던 주문 집계 배치를 단계별로 
- opencode:qwen3.8-flash g09 gold=[] pick=['comp:ai.dl'] uncovered=False | 협업 필터링 기반 상품 추천 모델을 만들어 클릭률을 12% 올렸다.
- openai:gpt-4.1-mini g09 gold=[] pick=['comp:ai.service'] uncovered=False | 협업 필터링 기반 상품 추천 모델을 만들어 클릭률을 12% 올렸다.
- openai:gpt-4.1-mini g15 gold=['comp:data.stat_ml'] pick=['comp:data.analysis'] uncovered=False | 판매 데이터로 수요 예측 모형을 만들어 재고 결정을 도왔다.
- openai:gpt-4.1-mini g19 gold=['comp:sw.rest_api'] pick=['comp:sw.rest_api', 'comp:sw.server_dev'] uncovered=False | 주문 조회·취소 REST API 를 설계하고 구현했다.
- openai:gpt-4.1-mini g35 gold=['comp:emb.rtos'] pick=['comp:emb.rtos', 'comp:def.realtime'] uncovered=False | FreeRTOS 기반 센서 펌웨어의 태스크 구조를 설계했다.