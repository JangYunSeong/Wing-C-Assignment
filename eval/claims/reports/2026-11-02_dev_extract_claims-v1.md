# 주장 추출 평가 — dev (2026-11-02, 문서 20건 × 2회, prompt extract_claims@CURRENT)

| 지표 | opencode:qwen3.8-flash | openai:gpt-4.1-mini |
|---|---|---|
| sentence_recall | 0.983 | 0.744 |
| action_recall | 0.985 | 0.675 |
| split_rate | 1.0 | 0.267 |
| precision | 1.0 | 1.0 |
| non_action_rate | 0.0 | 0.0 |
| quote_ok | 1.0 | 1.0 |
| field_acc | 1.0 | 1.0 |
| role_acc | 1.0 | 1.0 |
| stability | 0.955 | 0.968 |
| claims_per_doc | 5.08 | 3.48 |
| errors | 0 | 0 |
| latency_ms_avg | 16179 | 2255 |
| reasoning_tokens_avg | 732 | 0 |

## 실패 사례 — opencode:qwen3.8-flash (1회차)

- {'doc': 'clm-dev-014', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 31% 올렸다.'}
- {'doc': 'clm-dev-018', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 21시간에서 10분으로 줄였다.'}
- {'doc': 'clm-dev-018', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 70% 올렸다.'}

## 실패 사례 — openai:gpt-4.1-mini (1회차)

- {'doc': 'clm-dev-000', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 16% 올렸다.'}
- {'doc': 'clm-dev-002', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 14% 올렸다.'}
- {'doc': 'clm-dev-002', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '쿼리 실행 계획을 분석하고 인덱스를 다시 만들었다.'}
- {'doc': 'clm-dev-002', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 69시간에서 12분으로 줄였다.'}
- {'doc': 'clm-dev-004', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '쿼리 실행 계획을 분석하고 인덱스를 다시 만들었다.'}
- {'doc': 'clm-dev-004', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 38% 올렸다.'}
- {'doc': 'clm-dev-004', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '학습 파이프라인을 컨테이너화하고 배포 스크립트를 자동화했다.'}
- {'doc': 'clm-dev-004', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 87% 올렸다.'}
- {'doc': 'clm-dev-005', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 7% 올렸다.'}
- {'doc': 'clm-dev-005', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 77% 절감했다.'}
- {'doc': 'clm-dev-006', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 35시간에서 16분으로 줄였다.'}
- {'doc': 'clm-dev-008', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 23% 절감했다.'}
- {'doc': 'clm-dev-008', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 66% 절감했다.'}
- {'doc': 'clm-dev-009', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 28% 올렸다.'}
- {'doc': 'clm-dev-010', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '쿼리 실행 계획을 분석하고 인덱스를 다시 만들었다.'}
- {'doc': 'clm-dev-010', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 7시간에서 44분으로 줄였다.'}
- {'doc': 'clm-dev-010', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': 'Kafka 토픽을 재설계하고 소비자 지연 알림을 추가했다.'}
- {'doc': 'clm-dev-010', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 62% 절감했다.'}
- {'doc': 'clm-dev-012', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '학습 파이프라인을 컨테이너화하고 배포 스크립트를 자동화했다.'}
- {'doc': 'clm-dev-012', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 27% 절감했다.'}
- {'doc': 'clm-dev-012', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': 'Kafka 토픽을 재설계하고 소비자 지연 알림을 추가했다.'}
- {'doc': 'clm-dev-012', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 83% 절감했다.'}
- {'doc': 'clm-dev-013', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '학습 파이프라인을 컨테이너화하고 배포 스크립트를 자동화했다.'}
- {'doc': 'clm-dev-013', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 22% 올렸다.'}
- {'doc': 'clm-dev-013', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '라벨링 가이드를 정리하고 검수 절차를 도입했다.'}
- {'doc': 'clm-dev-013', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 56% 절감했다.'}
- {'doc': 'clm-dev-014', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 31% 올렸다.'}
- {'doc': 'clm-dev-016', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '학습 파이프라인을 컨테이너화하고 배포 스크립트를 자동화했다.'}
- {'doc': 'clm-dev-016', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 27시간에서 11분으로 줄였다.'}
- {'doc': 'clm-dev-017', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 33시간에서 39분으로 줄였다.'}
- {'doc': 'clm-dev-017', 'kind': 'missed_action', 'expected': 2, 'got': 1, 'sentence': '라벨링 가이드를 정리하고 검수 절차를 도입했다.'}
- {'doc': 'clm-dev-017', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '월 클라우드 비용을 67% 절감했다.'}
- {'doc': 'clm-dev-018', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '집계 시간을 21시간에서 10분으로 줄였다.'}
- {'doc': 'clm-dev-018', 'kind': 'missed_action', 'expected': 1, 'got': 0, 'sentence': '추천 클릭률을 70% 올렸다.'}
