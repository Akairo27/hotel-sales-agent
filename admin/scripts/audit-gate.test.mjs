import { describe, expect, it } from "vitest";
import { advisoriesIn, evaluateAudit } from "./audit-gate-core.mjs";

const BRACES = {
  id: "GHSA-vfj7-8cjw-p6xm",
  package: "braces",
  reason: "no fixed release",
  expires: "2026-10-17",
};

function advisory(id, severity, name = "braces") {
  return { source: 1, name, title: `title of ${id}`, url: `https://github.com/advisories/${id}`, severity };
}

function report(...vias) {
  return {
    vulnerabilities: {
      braces: { name: "braces", via: vias },
      micromatch: { name: "micromatch", via: ["braces"] },
    },
  };
}

describe("advisoriesIn", () => {
  it("reads advisories, not the package names a vulnerability pulls in", () => {
    const found = advisoriesIn(report(advisory("GHSA-aaaa", "high")));
    expect(found.map((item) => item.url)).toEqual(["https://github.com/advisories/GHSA-aaaa"]);
  });
});

describe("evaluateAudit", () => {
  it("passes a clean report", () => {
    expect(evaluateAudit({ vulnerabilities: {} }, [BRACES], "2026-10-03").failures).toEqual([]);
  });

  it("passes the excepted advisory before its expiry, and says so", () => {
    const result = evaluateAudit(report(advisory(BRACES.id, "high")), [BRACES], "2026-10-17");
    expect(result.failures).toEqual([]);
    expect(result.notes.join("\n")).toContain("excepted until 2026-10-17");
  });

  it("fails the excepted advisory the day after its expiry", () => {
    const result = evaluateAudit(report(advisory(BRACES.id, "high")), [BRACES], "2026-10-18");
    expect(result.failures).toHaveLength(1);
    expect(result.failures[0]).toContain("expired on 2026-10-17");
  });

  it("still fails every other high or critical advisory, in the same report", () => {
    const result = evaluateAudit(
      report(advisory(BRACES.id, "high"), advisory("GHSA-bbbb", "high", "other"), advisory("GHSA-cccc", "critical", "next")),
      [BRACES],
      "2026-10-03",
    );
    expect(result.failures).toHaveLength(2);
    expect(result.failures.join("\n")).toContain("GHSA-bbbb");
    expect(result.failures.join("\n")).toContain("GHSA-cccc");
  });

  it("ignores moderate and low advisories, as npm audit --audit-level=high does", () => {
    const result = evaluateAudit(report(advisory("GHSA-dddd", "moderate"), advisory("GHSA-eeee", "low")), [], "2026-10-03");
    expect(result.failures).toEqual([]);
  });

  it("fails when npm audit itself could not run", () => {
    const result = evaluateAudit({ error: { summary: "offline" } }, [BRACES], "2026-10-03");
    expect(result.failures[0]).toContain("could not run");
  });

  it("tells when an exception matches nothing, so it gets removed", () => {
    const result = evaluateAudit({ vulnerabilities: {} }, [BRACES], "2026-10-03");
    expect(result.notes.join("\n")).toContain("matched nothing");
  });
});
