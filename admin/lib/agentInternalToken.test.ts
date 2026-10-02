import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// AGENT_INTERNAL_TOKEN must stay on the server (owner requirement
// 2026-10-02). This checks the source; CI's build step checks the browser
// output itself (scripts/check-client-bundle.mjs).

const ADMIN_ROOT = fileURLToPath(new URL("..", import.meta.url));
const VARIABLE = "AGENT_INTERNAL_TOKEN";
const READER = "lib/agentInternal.ts";
const SOURCE_EXTENSIONS = [".ts", ".tsx", ".mts", ".js", ".mjs"];
const SKIPPED_DIRECTORIES = new Set(["node_modules", ".next"]);

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return SKIPPED_DIRECTORIES.has(entry.name) ? [] : sourceFiles(path);
    }
    return SOURCE_EXTENSIONS.some((extension) => entry.name.endsWith(extension)) ? [path] : [];
  });
}

function isTestOrCheck(path: string): boolean {
  return /\.test\.tsx?$/.test(path) || path.startsWith("scripts/");
}

const files = sourceFiles(ADMIN_ROOT).map((path) => ({
  path: relative(ADMIN_ROOT, path),
  text: readFileSync(path, "utf8"),
}));

describe("AGENT_INTERNAL_TOKEN", () => {
  it("is read by lib/agentInternal.ts alone among the dashboard's code", () => {
    const readers = files
      .filter((file) => !isTestOrCheck(file.path) && file.text.includes(VARIABLE))
      .map((file) => file.path);
    expect(readers).toEqual([READER]);
  });

  it("lives in a module that imports server-only first", () => {
    const reader = files.find((file) => file.path === READER);
    const firstImport = reader?.text.split("\n").find((line) => line.startsWith("import "));
    expect(firstImport).toBe('import "server-only";');
  });

  it("is imported by no Client Component", () => {
    const clientImporters = files
      .filter((file) => /^["']use client["'];?/m.test(file.text))
      .filter((file) => /from ["'](@\/lib|\.{1,2}(\/\.\.)*\/lib|\.)\/agentInternal["']/.test(file.text))
      .map((file) => file.path);
    expect(clientImporters).toEqual([]);
  });

  it("is never given a NEXT_PUBLIC_ name or put in next.config's env", () => {
    expect(files.filter((file) => file.text.includes(`NEXT_PUBLIC_${VARIABLE}`))).toEqual([]);
    const nextConfig = files.find((file) => file.path === "next.config.ts");
    expect(nextConfig?.text).not.toContain(VARIABLE);
  });
});
