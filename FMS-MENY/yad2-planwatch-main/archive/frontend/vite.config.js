import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    proxy: {
      '/api': mode === 'integration' ? 'http://127.0.0.1:4100' : 'http://127.0.0.1:4000',
    },
  },
}));
