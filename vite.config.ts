import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 构建配置
// - base "./": 产物用相对路径,便于子路径(如 /nas1/)部署
// - build.outDir = static/dist: 输出给 Python 后端服务
// - server.proxy: 开发时把 /api 转发到本地 8765 后端
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'static/dist',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
})
