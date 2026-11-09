import { setupServer } from 'msw/node';
import { handlers } from './handlers';

/** 테스트용 MSW 서버 (src/test/setup.ts 에서 켠다). 테스트마다 `server.use(...)` 로 응답을 바꿀 수 있다. */
export const server = setupServer(...handlers);
