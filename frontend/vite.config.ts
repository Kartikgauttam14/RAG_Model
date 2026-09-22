import { realpathSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// On this machine ``K:\projects`` is a junction to ``K:\project``. Node resolves this config
// file, the module graph and vite's build output through the junction to the real path, so
// ``root`` has to be the real path too - with the junction path instead, rollup emitted an
// index.html chunk named "../../../project/rag-from-scratch/frontend/index.html" and the
// build failed. The *shell* keeps the junction path, and vitest asks vite for the test file
// as ``/@fs/K:/project/.../tests/Sources.test.tsx``; the fs guard rejected that, so every
// suite failed with "Cannot find module". Allowing both path spaces fixes the suite, and
// ``root`` stays in the real path space that the build needs.
const resolvedDirectory = realpathSync(new URL(".", import.meta.url));
const invokedDirectory = process.cwd();

export default defineConfig({
  root: resolvedDirectory,
  plugins: [react()],
  server: {
    port: 5173,
    fs: { allow: [resolvedDirectory, invokedDirectory] },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
