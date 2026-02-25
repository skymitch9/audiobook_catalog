import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../site/build',
    assetsDir: 'static',
    emptyOutDir: true,
  },
  server: {
    host: '0.0.0.0', // Allow connections from outside container
    port: 3001,
    strictPort: true,
    watch: {
      usePolling: true, // Enable polling for Docker volume mounts
    },
    proxy: {
      '/api': {
        target: process.env.DOCKER_ENV === 'true' ? 'http://backend:5001' : 'http://localhost:5000',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
