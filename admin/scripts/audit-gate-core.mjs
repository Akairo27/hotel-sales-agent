// The dashboard's dependency gate (CLAUDE.md §3: zero high or critical),
// with one narrow, time-limited exception list. Pure: the report and the
// date come in, the verdict comes out.
//
// `npm audit --audit-level=high` cannot except a single advisory, and one
// new advisory with no fixed release (braces, 2026-09-18) would then block
// every merge until upstream patches. Each exception here names one
// advisory, why it is safe, and the day it stops applying; once that day
// passes the gate fails again, so an exception cannot be forgotten. Every
// other high or critical advisory still fails.

const BLOCKING_SEVERITIES = new Set(["high", "critical"]);

/** The advisories in an `npm audit --json` report: only the entries of a
 * vulnerability's `via` that are objects (an advisory); string entries just
 * name another vulnerable package the first one pulls in. */
export function advisoriesIn(report) {
  const found = new Map();
  for (const vulnerability of Object.values(report.vulnerabilities ?? {})) {
    for (const via of vulnerability.via ?? []) {
      if (typeof via === "object" && via !== null && via.url) {
        found.set(via.url, { url: via.url, severity: via.severity, name: via.name, title: via.title });
      }
    }
  }
  return [...found.values()];
}

/** Judges a report against the exceptions on `today` (a YYYY-MM-DD string).
 * Returns { failures, notes }: any failure fails the gate. */
export function evaluateAudit(report, exceptions, today) {
  const failures = [];
  const notes = [];
  if (report.error) {
    return { failures: [`npm audit could not run: ${report.error.summary ?? report.error.code}`], notes };
  }
  const advisories = advisoriesIn(report);
  const seen = new Set();
  for (const advisory of advisories) {
    if (!BLOCKING_SEVERITIES.has(advisory.severity)) {
      continue;
    }
    const exception = exceptions.find((candidate) => advisory.url.endsWith(candidate.id));
    if (!exception) {
      failures.push(`${advisory.severity}: ${advisory.name} - ${advisory.title} (${advisory.url})`);
    } else if (today > exception.expires) {
      failures.push(
        `exception for ${exception.id} expired on ${exception.expires}: ${advisory.name} - ${advisory.title}`,
      );
    } else {
      seen.add(exception.id);
      notes.push(`excepted until ${exception.expires}: ${exception.id} (${exception.package}) - ${exception.reason}`);
    }
  }
  for (const exception of exceptions) {
    if (!seen.has(exception.id)) {
      notes.push(`exception ${exception.id} matched nothing: remove it from scripts/audit-gate.mjs`);
    }
  }
  return { failures, notes };
}
