import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const bridgeOrigin = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/healthz": bridgeOrigin,
      "/auth": bridgeOrigin,
      "/workspace": bridgeOrigin,
      "/ws": {
        target: bridgeOrigin,
        ws: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
});
