import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Build to ../src/dashboard/dist so the SAM/S3 deploy path stays close to the
// existing static dashboard location. Relative asset base keeps it portable
// under any CloudFront/S3 prefix.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Split heavy vendors so the initial payload is lean and cacheable.
        manualChunks: {
          charts: ["recharts"],
          query: ["@tanstack/react-query"],
          motion: ["motion"],
        },
      },
    },
  },
  server: {
    port: 5173,
  },
});
