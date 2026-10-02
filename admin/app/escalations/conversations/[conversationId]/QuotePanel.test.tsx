import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ChatWorkspaceProvider } from "./ChatWorkspace";
import { type PanelQuote, QuotePanel } from "./QuotePanel";

const NOW = "2026-10-02T11:10:00Z";
const INSIDE_WINDOW = "2026-10-02T09:00:00Z";
const OUTSIDE_WINDOW = "2026-10-01T09:00:00Z";

function quote(overrides: Partial<PanelQuote>): PanelQuote {
  return {
    id: 11,
    title: "فندق الاختبار2، جناح ملكي",
    details: "العرض رقم 11 · من 20 أكتوبر إلى 22 أكتوبر",
    createdAt: "2026-10-02T11:00:00Z",
    bookingRequested: false,
    replyText: "نص العرض",
    ...overrides,
  };
}

function render(
  quotes: PanelQuote[],
  { canReply = true, lastInboundAt = INSIDE_WINDOW }: { canReply?: boolean; lastInboundAt?: string | null } = {},
): string {
  return renderToStaticMarkup(
    <ChatWorkspaceProvider serverNow={NOW} canReply={canReply} lastInboundAt={lastInboundAt}>
      <QuotePanel quotes={quotes} />
    </ChatWorkspaceProvider>,
  );
}

describe("QuotePanel", () => {
  it("shows a valid quote with the Riyadh time it is valid until, and the insert button", () => {
    // Created 11:00Z, valid 30 minutes: until 11:30Z = 14:30 in Riyadh.
    const html = render([quote({})]);
    expect(html).toContain("صالح حتى");
    expect(html).toContain("14:30");
    expect(html).toContain("إدراج في الرد");
    expect(html).not.toContain("منتهي");
  });

  it("shows an expired quote with no insert button", () => {
    const html = render([quote({ createdAt: "2026-10-02T10:00:00Z" })]);
    expect(html).toContain("منتهي");
    expect(html).not.toContain("صالح حتى");
    expect(html).not.toContain("إدراج في الرد");
  });

  it("marks a booking request, with the validity badge beside it", () => {
    const valid = render([quote({ bookingRequested: true })]);
    expect(valid).toContain("طُلب حجزه");
    expect(valid).toContain("صالح حتى");
    const expired = render([quote({ bookingRequested: true, createdAt: "2026-10-02T10:00:00Z" })]);
    expect(expired).toContain("طُلب حجزه");
    expect(expired).toContain("منتهي");
  });

  it("offers insert only to the one who may reply", () => {
    expect(render([quote({})], { canReply: false })).not.toContain("إدراج في الرد");
  });

  it("disables insert outside the 24-hour window, with the reason", () => {
    const html = render([quote({})], { lastInboundAt: OUTSIDE_WINDOW });
    expect(html).toMatch(/<button[^>]*disabled[^>]*>\s*إدراج في الرد/);
    expect(html).toContain("غير متاح خارج نافذة 24 ساعة");
  });

  it("renders quote text as text, never as HTML", () => {
    const html = render([quote({ title: "<b>x</b>" })]);
    expect(html).not.toContain("<b>x");
    expect(html).toContain("&lt;b&gt;");
  });
});
