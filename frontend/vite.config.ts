import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // WSL 访问 Windows 挂载目录时可能收不到文件变更事件，开发环境改用轮询保证热更新。
    watch: {
      usePolling: true,
      interval: 300,
    },
    proxy: {
      "/api": {
        target: process.env.STORYWEAVER_API_URL ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
