// 채점용 테스트 설정 — 지원자 setupFiles·가짜 API 는 쓰지 않는다 (하네스가 응답 시점을 정한다).
// v3: jsdom 에 없는 IndexedDB 를 fake-indexeddb 로 채운다 (테스트마다 빈 저장소). 2026-11-10 프론트 알파 0108be12 — 모든 구현이 `indexedDB is not defined`.
import 'fake-indexeddb/auto';
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { IDBFactory } from 'fake-indexeddb';
import { afterAll, afterEach, beforeEach } from 'vitest';
import { api } from './grader-api';
import { resetHead } from './grader-head';

// 앱 모듈이 import 되기 전에 가로채기를 켠다 (모듈 최상위에서 fetch 를 잡아 두는 구현도 채점되도록).
api.listen();

// 테스트마다 빈 브라우저 저장소 (IndexedDB·localStorage·sessionStorage). 한 테스트 안에서 renderApp 을 다시 부르는 '새로고침'은 저장소를 그대로 둔다.
// 문서 머리는 테스트마다 index.html 그대로 (v15 — 새로고침에서도 renderApp 이 되돌린다)
beforeEach(() => {
  globalThis.indexedDB = new IDBFactory() as unknown as typeof globalThis.indexedDB;
  window.localStorage.clear();
  window.sessionStorage.clear();
  resetHead();
});

afterEach(async () => {
  cleanup();
  await api.reset();
});

afterAll(() => api.close());
