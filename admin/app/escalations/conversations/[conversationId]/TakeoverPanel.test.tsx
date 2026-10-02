import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { NoticeState } from "@/lib/takeoverNotice";
import { type PanelTakeover, TakeoverPanel } from "./TakeoverPanel";

const ME = "user-me";
const NOW = "2026-10-02T05:00:00Z";

function heldBy(holderId: string, noticeState: NoticeState = "sent"): PanelTakeover {
  return {
    id: 9,
    holderId,
    holderName: "Sara",
    takenOverAt: "2026-10-02T04:55:00Z",
    noticeState,
  };
}

function render({
  takeover = null,
  openCount = 2,
  isAdmin = false,
  loadFailed = false,
}: {
  takeover?: PanelTakeover | null;
  openCount?: number;
  isAdmin?: boolean;
  loadFailed?: boolean;
}): string {
  return renderToStaticMarkup(
    <TakeoverPanel
      conversationId={5}
      openCount={openCount}
      takeover={takeover}
      currentUserId={ME}
      isAdmin={isAdmin}
      now={NOW}
      loadFailed={loadFailed}
    />,
  );
}

function buttons(html: string): string[] {
  return [...html.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map((match) => match[1].trim());
}

describe("TakeoverPanel", () => {
  it("offers take over and resolve while nobody holds the customer", () => {
    const html = render({});
    expect(html).toContain("لم يستلم أحد هذه المحادثة بعد.");
    expect(buttons(html)).toEqual(["استلام المحادثة", "إغلاق"]);
  });

  it("lets the holder resolve or hand back, and says the bot is silent", () => {
    const html = render({ takeover: heldBy(ME) });
    expect(html).toContain("استلمتها أنت منذ 5 دقائق");
    expect(html).toContain("لا يرد البوت على العميل");
    expect(html).toContain("أُرسل للعميل إشعار الاستلام.");
    expect(buttons(html)).toEqual(["إغلاق", "إعادة للبوت"]);
  });

  it("shows another sales user who holds the customer, with nothing to press", () => {
    const html = render({ takeover: heldBy("someone-else") });
    expect(html).toContain("استلمها Sara منذ 5 دقائق");
    expect(buttons(html)).toEqual([]);
  });

  it("lets an admin end someone else's takeover (owner decision D4)", () => {
    expect(buttons(render({ takeover: heldBy("someone-else"), isAdmin: true }))).toEqual([
      "إغلاق",
      "إعادة للبوت",
    ]);
  });

  it("offers the notice again only to the holder, and only when nothing was sent", () => {
    expect(buttons(render({ takeover: heldBy(ME, "not_sent") }))).toEqual([
      "إرسال إشعار الاستلام",
      "إغلاق",
      "إعادة للبوت",
    ]);
    expect(buttons(render({ takeover: heldBy(ME, "failed") }))).not.toContain(
      "إرسال إشعار الاستلام",
    );
    expect(buttons(render({ takeover: heldBy("someone-else", "not_sent") }))).toEqual([]);
  });

  it("tells staff to call the customer when the notice did not go out", () => {
    expect(render({ takeover: heldBy(ME, "failed") })).toContain("تواصل معه هاتفياً");
  });

  it("offers nothing once every escalation is closed", () => {
    const html = render({ openCount: 0 });
    expect(html).toContain("كل تصعيدات هذا العميل مغلقة.");
    expect(buttons(html)).toEqual([]);
  });

  it("offers nothing when who holds the customer could not be read", () => {
    const html = render({ loadFailed: true });
    expect(html).toContain("تعذّر تحميل حالة الاستلام");
    expect(buttons(html)).toEqual([]);
  });
});
