import { render } from '@testing-library/react';
import { expect, it } from 'vitest';
import App from './App';

it('앱 화면이 그려진다', () => {
  const { container } = render(<App />);
  expect(container.querySelector('.page')).not.toBeNull();
});
