import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 127.0.0.1 everywhere: Node 20 resolves "localhost" to ::1 while uvicorn/Chrome tend to use IPv4.
const API = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: API, changeOrigin: true } },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    proxy: { "/api": { target: API, changeOrigin: true } },
  },
});
