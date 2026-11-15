import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

// 개발 서버에서는 MSW 가 서버 API 를 흉내 낸다 (src/mocks/handlers.ts). 운영 빌드에는 들어가지 않는다.
async function enableMocking(): Promise<void> {
  if (!import.meta.env.DEV) return;
  const { worker } = await import('./mocks/browser');
  await worker.start({ onUnhandledRequest: 'bypass' });
}

enableMocking().then(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
