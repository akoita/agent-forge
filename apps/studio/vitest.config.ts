import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      {
        find: /^monaco-editor$/,
        replacement: "/src/testMonacoStub.ts",
      },
      {
        find: /^@xterm\/xterm$/,
        replacement: "/src/testXtermStub.ts",
      },
      {
        find: /^@xterm\/addon-fit$/,
        replacement: "/src/testXtermFitStub.ts",
      },
    ],
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/testSetup.ts"],
    include: ["src/__tests__/**/*.test.{ts,tsx}"],
  },
});
