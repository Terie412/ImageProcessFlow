import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建产物直接落到后端静态目录，由 FastAPI 托管
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../server/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8770',
    },
  },
})
