import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_')
  return {
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/v1': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000', '/healthz': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000', '/readyz': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000', '/docs': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000', '/openapi.json': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000' },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          mui: ['@mui/material', '@mui/icons-material', '@emotion/react', '@emotion/styled'],
          query: ['@tanstack/react-query', 'axios', 'react-hook-form'],
        },
      },
    },
  },
  }
})
