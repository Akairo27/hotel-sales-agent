// Proves AGENT_INTERNAL_TOKEN never reaches the browser (owner requirement
// 2026-10-02). Run right after `next build` with AGENT_INTERNAL_TOKEN set to
// a throwaway canary (CI's admin job does): fails if the canary or the
// variable's name appears in anything the browser downloads -- the static
// bundles, and prerendered HTML and RSC payloads -- or if the canary was
// baked into any build output at all (the token is meant to be read at
// request time only). A positive control keeps the check from passing
// vacuously: the server build must still contain the code that reads the
// variable.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { extname, join, relative } from "node:path";

const VARIABLE = "AGENT_INTERNAL_TOKEN";
const MIN_CANARY_LENGTH = 32;
const NEXT_DIR = join(process.cwd(), ".next");
// Prerendered pages the browser receives as they are.
const PRERENDERED_EXTENSIONS = new Set([".html", ".rsc", ".body"]);

function fail(message) {
  console.error(`check-client-bundle: ${message}`);
  process.exit(1);
}

function filesUnder(directory) {
  if (!existsSync(directory)) {
    return [];
  }
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? filesUnder(path) : [path];
  });
}

function containing(paths, needle) {
  return paths.filter((path) => readFileSync(path).includes(needle)).map((path) => relative(NEXT_DIR, path));
}

const canary = process.env[VARIABLE];
if (!canary || canary.length < MIN_CANARY_LENGTH) {
  fail(`set ${VARIABLE} to a canary of at least ${MIN_CANARY_LENGTH} characters before building`);
}

const browserFiles = [
  ...filesUnder(join(NEXT_DIR, "static")),
  ...filesUnder(join(NEXT_DIR, "server", "app")).filter((path) =>
    PRERENDERED_EXTENSIONS.has(extname(path)),
  ),
];
if (!browserFiles.some((path) => path.endsWith(".js"))) {
  fail("no browser JavaScript under .next/static -- run next build first");
}

const buildOutput = filesUnder(NEXT_DIR).filter((path) => !relative(NEXT_DIR, path).startsWith("cache"));
const leaks = [
  ...containing(browserFiles, VARIABLE).map((path) => `${path} names ${VARIABLE}`),
  ...containing(buildOutput, canary).map((path) => `${path} contains the token's value`),
];
if (leaks.length > 0) {
  fail(`the agent token reaches build output:\n  ${leaks.join("\n  ")}`);
}

const serverCode = filesUnder(join(NEXT_DIR, "server")).filter((path) => path.endsWith(".js"));
if (containing(serverCode, VARIABLE).length === 0) {
  fail(`positive control: no server output reads ${VARIABLE}, so this check would prove nothing`);
}
console.log(
  `check-client-bundle: ${VARIABLE} is in none of ${browserFiles.length} browser files, ` +
    `and its value in no build output`,
);
