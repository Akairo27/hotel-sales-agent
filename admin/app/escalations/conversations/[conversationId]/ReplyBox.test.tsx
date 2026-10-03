import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ChatWorkspaceProvider } from "./ChatWorkspace";
import { type BoxReply, ReplyBox } from "./ReplyBox";

const ME = "user-me";
const NOW = "2026-10-02T12:00:00Z";
const INSIDE_WINDOW = "2026-10-02T09:00:00Z";
const OUTSIDE_WINDOW = "2026-10-01T11:00:00Z";
const TAKEOVER_ID = 9;

function reply(overrides: Partial<BoxReply>): BoxReply {
  return {
    id: 1,
    takeover_id: TAKEOVER_ID,
    conversation_id: 5,
    sent_by: ME,
    body: "حياك الله",
    created_at: "2026-10-02T11:00:00Z",
    claimed_at: null,
    sent_at: null,
    failed_at: null,
    failure_reason: null,
    kind: "text",
    template_hotel_id: null,
    authorName: "Sara",
    ...overrides,
  };
}

function render({
  takeoverId = TAKEOVER_ID,
  holderId = ME,
  lastInboundAt = INSIDE_WINDOW,
  replies = [],
  loadFailed = false,
  templateEnabled = true,
}: {
  takeoverId?: number | null;
  holderId?: string | null;
  lastInboundAt?: string | null;
  replies?: BoxReply[];
  loadFailed?: boolean;
  templateEnabled?: boolean;
}): string {
  return renderToStaticMarkup(
    <ChatWorkspaceProvider serverNow={NOW} canReply={holderId === ME} lastInboundAt={lastInboundAt}>
      <ReplyBox
        conversationId={5}
        takeoverId={takeoverId}
        holderId={holderId}
        currentUserId={ME}
        replies={replies}
        loadFailed={loadFailed}
        templateEnabled={templateEnabled}
      />
    </ChatWorkspaceProvider>,
  );
}

function buttons(html: string): { label: string; disabled: boolean }[] {
  return [...html.matchAll(/<button([^>]*)>([^<]*)<\/button>/g)].map((match) => ({
    label: match[2].trim(),
    disabled: /\bdisabled(=|\s|$)/.test(match[1]),
  }));
}

function textareaDisabled(html: string): boolean {
  const textarea = /<textarea([^>]*)>/.exec(html);
  return textarea !== null && /\bdisabled(=|\s|$)/.test(textarea[1]);
}

describe("ReplyBox visibility", () => {
  it("shows to the holder of the takeover", () => {
    expect(render({})).toContain('aria-label="نص الرد"');
  });

  it("shows nothing when nobody holds the customer", () => {
    expect(render({ takeoverId: null, holderId: null })).toBe("");
  });

  it("shows nothing to another staff member, an admin included (owner decision 1)", () => {
    expect(render({ holderId: "someone-else" })).toBe("");
  });
});

describe("ReplyBox inside the 24-hour window", () => {
  it("offers free text and send, and no template button", () => {
    const html = render({});
    expect(textareaDisabled(html)).toBe(false);
    expect(buttons(html)).toEqual([{ label: "إرسال الرد", disabled: false }]);
    expect(html).not.toContain("مرّ أكثر من 24 ساعة");
  });

  it("counts the window down, in hours and minutes, with Ctrl/⌘+Enter named", () => {
    // Last message 09:00, now 12:00: 21 hours left.
    const html = render({});
    expect(html).toContain("نافذة الرد الحر مفتوحة");
    expect(html).toContain("باقي 21 ساعة");
    expect(html).toContain("Ctrl/⌘ + Enter للإرسال");
  });

  it("turns the countdown into a warning in the last hour", () => {
    const html = render({ lastInboundAt: "2026-10-01T12:30:00Z" });
    expect(html).toContain("باقي 30 دقيقة");
    expect(html).toContain("text-danger");
    expect(render({})).not.toContain("text-danger");
  });
});

describe("ReplyBox outside the 24-hour window", () => {
  it("disables free text and offers the re-engagement template once the agent has it", () => {
    const html = render({ lastInboundAt: OUTSIDE_WINDOW });
    expect(textareaDisabled(html)).toBe(true);
    expect(buttons(html)).toEqual([
      { label: "إرسال الرد", disabled: true },
      { label: "إرسال قالب إعادة التواصل", disabled: false },
    ]);
    expect(html).toContain("مرّ أكثر من 24 ساعة");
    expect(html).not.toContain("غير مفعّل بعد");
  });

  it("keeps the template button off, saying to call the customer, until the agent has template names", () => {
    const html = render({ lastInboundAt: OUTSIDE_WINDOW, templateEnabled: false });
    expect(buttons(html)).toEqual([
      { label: "إرسال الرد", disabled: true },
      { label: "إرسال قالب إعادة التواصل", disabled: true },
    ]);
    expect(html).toContain("قالب إعادة التواصل غير مفعّل بعد. تواصل مع العميل هاتفياً.");
  });

  it("treats a customer who never wrote as outside the window", () => {
    const html = render({ lastInboundAt: null });
    expect(textareaDisabled(html)).toBe(true);
    expect(buttons(html).map((button) => button.label)).toContain("إرسال قالب إعادة التواصل");
  });

  it("offers no template inside the window, whatever the agent has", () => {
    expect(buttons(render({ templateEnabled: true })).map((button) => button.label)).toEqual([
      "إرسال الرد",
    ]);
  });
});

describe("ReplyBox replies", () => {
  it("leaves a sent reply to the conversation, where it shows as a staff message", () => {
    const html = render({
      replies: [reply({ claimed_at: NOW, sent_at: NOW })],
    });
    expect(html).not.toContain("حياك الله");
    expect(buttons(html)).toEqual([{ label: "إرسال الرد", disabled: false }]);
  });

  it("shows a reply the agent never claimed with a retry for its author", () => {
    const html = render({ replies: [reply({})] });
    expect(html).toContain("لم يُرسل بعد");
    expect(buttons(html).map((button) => button.label)).toEqual(["إعادة المحاولة", "إرسال الرد"]);
  });

  it("lists only the replies of this takeover", () => {
    const html = render({ replies: [reply({ takeover_id: 3 })] });
    expect(buttons(html).map((button) => button.label)).toEqual(["إرسال الرد"]);
    expect(html).not.toContain("حياك الله");
  });

  it("offers no retry for a reply someone else wrote", () => {
    const html = render({ replies: [reply({ sent_by: "someone-else" })] });
    expect(buttons(html).map((button) => button.label)).toEqual(["إرسال الرد"]);
  });

  it("shows a failed send with the call-the-customer hint and a rewrite, never a retry", () => {
    const html = render({
      replies: [
        reply({ claimed_at: NOW, failed_at: NOW, failure_reason: "send_failed" }),
      ],
    });
    expect(html).toContain("لم يُرسل");
    expect(html).toContain("تواصل معه هاتفياً");
    expect(buttons(html).map((button) => button.label)).toEqual(["إعادة كتابته", "إرسال الرد"]);
  });

  it("shows an outside-window failure with the hint, and no rewrite while the window is shut", () => {
    const html = render({
      lastInboundAt: OUTSIDE_WINDOW,
      replies: [
        reply({ claimed_at: NOW, failed_at: NOW, failure_reason: "outside_window" }),
      ],
    });
    expect(html).toContain("لا يقبل واتساب رداً حراً");
    expect(buttons(html).map((button) => button.label)).not.toContain("إعادة كتابته");
  });

  it("never offers to rewrite a failed template as text", () => {
    const html = render({
      replies: [
        reply({
          kind: "template",
          body: "قالب إعادة التواصل",
          claimed_at: NOW,
          failed_at: NOW,
          failure_reason: "send_failed",
        }),
      ],
    });
    expect(buttons(html).map((button) => button.label)).toEqual(["إرسال الرد"]);
  });

  it("tells staff a template was refused because the window is open", () => {
    const html = render({
      replies: [
        reply({
          kind: "template",
          claimed_at: NOW,
          failed_at: NOW,
          failure_reason: "window_open",
        }),
      ],
    });
    expect(html).toContain("نافذة الـ24 ساعة مفتوحة");
  });

  it("shows a reply being sent with neither retry nor rewrite", () => {
    const html = render({ replies: [reply({ claimed_at: "2026-10-02T11:59:50Z" })] });
    expect(html).toContain("قيد الإرسال");
    expect(buttons(html).map((button) => button.label)).toEqual(["إرسال الرد"]);
  });

  it("says when the earlier replies could not be loaded", () => {
    expect(render({ loadFailed: true })).toContain("تعذّر تحميل ردودك السابقة");
  });

  it("renders a reply's text as text, never as HTML", () => {
    const html = render({ replies: [reply({ body: "<img src=x onerror=alert(1)>" })] });
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});
