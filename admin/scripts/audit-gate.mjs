// Runs `npm audit --json` and applies audit-gate-core.mjs to it. Run from
// admin/ (CI's dependency job does). No install is needed: npm audit reads
// package-lock.json against the registry's advisories. Dev dependencies are
// included on purpose -- they run in CI and on the build host.
import { spawnSync } from "node:child_process";
import { evaluateAudit } from "./audit-gate-core.mjs";

// Time-limited exceptions. Add one only with the owner's approval; never
// extend an expiry without asking again.
const EXCEPTIONS = [
  {
    id: "GHSA-vfj7-8cjw-p6xm",
    package: "braces",
    // The advisory (published 2026-09-18, high 7.5) covers every released
    // version, so there is nothing to upgrade to. braces reaches this project
    // only through eslint-config-next's glob matching: it runs on trusted
    // paths on a developer or CI machine, never on request input, and is not
    // in the dashboard's bundle.
    reason: "no fixed release exists; dev-only lint toolchain, never in the bundle",
    expires: "2026-10-17",
  },
];

const audit = spawnSync("npm", ["audit", "--json"], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
let report;
try {
  report = JSON.parse(audit.stdout);
} catch {
  console.error("audit-gate: npm audit did not return JSON");
  console.error(audit.stderr);
  process.exit(1);
}

const today = new Date().toISOString().slice(0, 10);
const { failures, notes } = evaluateAudit(report, EXCEPTIONS, today);
for (const note of notes) {
  console.log(`audit-gate: ${note}`);
}
if (failures.length > 0) {
  for (const failure of failures) {
    console.error(`audit-gate: FAIL ${failure}`);
  }
  process.exit(1);
}
console.log("audit-gate: no high or critical advisory outside the exceptions above");
