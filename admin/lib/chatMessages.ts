// How a message is told apart in the conversation (customer page, owner
// request 2026-10-02): who wrote it, and the colours and label that say so.
// Pure: the page reads the rows, this decides what they look like.
import { staffMessageLabel } from "@/lib/staffReply";
import type { MessageRow } from "@/lib/types";

export type MessageSender = "customer" | "bot" | "staff";

export function messageSender(message: Pick<MessageRow, "direction" | "staff_reply_id">): MessageSender {
  if (message.direction === "inbound") {
    return "customer";
  }
  return message.staff_reply_id === null ? "bot" : "staff";
}

/** The label above a message: the customer, the agent, or a staff member
 * («الموظف: name», the role alone when the name could not be read). */
export function senderLabel(
  message: Pick<MessageRow, "direction" | "staff_reply_id">,
  authorNames: Map<number, string>,
): string {
  const sender = messageSender(message);
  if (sender === "customer") {
    return "العميل";
  }
  if (sender === "bot") {
    return "الوكيل";
  }
  return staffMessageLabel(authorNames.get(message.staff_reply_id ?? -1) ?? null);
}

// The customer hugs the start edge and everyone replying the end edge, as a
// chat. Each of the three has its own tint so a glance tells them apart.
export const BUBBLE_CLASSES: Record<MessageSender, string> = {
  customer: "me-10 rounded-2xl border border-border bg-surface-subtle p-3",
  bot: "ms-10 rounded-2xl border border-sky-400/30 bg-sky-400/10 p-3",
  staff: "ms-10 rounded-2xl border border-accent/40 bg-accent/10 p-3",
};

export const SENDER_LABEL_CLASSES: Record<MessageSender, string> = {
  customer: "text-muted-foreground",
  bot: "text-sky-300",
  staff: "text-accent",
};
