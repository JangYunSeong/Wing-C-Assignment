// 채점용 테스트 설정 — 지원자 setupFiles·가짜 API 는 쓰지 않는다 (하네스가 응답 시점을 정한다).
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterAll, afterEach } from 'vitest';
import { api } from './grader-api';

// 앱 모듈이 import 되기 전에 가로채기를 켠다 (모듈 최상위에서 fetch 를 잡아 두는 구현도 채점되도록).
api.listen();

afterEach(async () => {
  cleanup();
  await api.reset();
});

afterAll(() => api.close());
