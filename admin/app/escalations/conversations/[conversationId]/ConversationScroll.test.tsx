import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ConversationScroll, latestButtonLabel, unseenCount } from "./ConversationScroll";

describe("unseenCount", () => {
  it("counts the messages newer than the last one seen", () => {
    expect(unseenCount([1, 2, 3, 4], 2)).toBe(2);
    expect(unseenCount([1, 2, 3, 4], 4)).toBe(0);
    expect(unseenCount([], 0)).toBe(0);
  });

  it("stays exact when the page's capped window slides past old messages", () => {
    expect(unseenCount([103, 104, 105], 104)).toBe(1);
  });
});

describe("latestButtonLabel", () => {
  it("shows the count only when there are new messages", () => {
    expect(latestButtonLabel(0)).toBe("آخر الرسائل");
    expect(latestButtonLabel(3)).toBe("آخر الرسائل (3)");
  });
});

describe("ConversationScroll", () => {
  it("opens at the newest message, with no button until the reader scrolls up", () => {
    const html = renderToStaticMarkup(
      <ConversationScroll messageIds={[1, 2]}>
        <p>رسالة</p>
      </ConversationScroll>,
    );
    expect(html).toContain("رسالة");
    expect(html).toContain('aria-label="المحادثة"');
    expect(html).not.toContain("آخر الرسائل");
  });
});
