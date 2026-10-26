/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    // Relative asset paths, so the same build works at any URL prefix.
    base: "./",
    plugins: [react()],
    server: {
      // Local development against a deployed stack: proxy API calls to it so
      // the browser sees one origin, exactly as it does behind CloudFront.
      proxy: env.API_PROXY_TARGET
        ? { "/reviews": { target: env.API_PROXY_TARGET, changeOrigin: true } }
        : undefined,
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
