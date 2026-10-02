import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Mirrors tsconfig.json's "@/*" -> "./*" path mapping. Next.js resolves
// that alias itself via its own bundler; plain `vitest run` has no such
// resolution without this, so any test that imports a file under app/ or
// lib/ using the `@/` convention (the norm in this codebase) fails to
// even load, not just to assert.
//
// "server-only" is resolved by Next.js itself, not installed (Next's own
// docs call installing it optional): its bundler maps the import to an
// empty module on the server and to a throwing one in client code. Vitest
// runs the server side of such a module, so it gets the same empty module
// Next uses there.
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
      "server-only": fileURLToPath(
        new URL("./node_modules/next/dist/compiled/server-only/empty.js", import.meta.url),
      ),
    },
  },
});
