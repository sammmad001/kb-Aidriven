import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://43.106.12.79:8080',
        changeOrigin: true,
        headers: {
          Authorization: 'Bearer dev-token',
        },
      },
    },
  },
  base: '/ui/',
})
