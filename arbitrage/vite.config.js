import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.ARBITRAGE_API_PROXY_TARGET || "http://127.0.0.1:8791";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api/arbitrage": {
        target: apiTarget,
        changeOrigin: true
      },
      "/health": {
        target: apiTarget,
        changeOrigin: true
      }
    }
  },
  preview: {
    port: 8791,
    strictPort: true,
    proxy: {}
  }
});
