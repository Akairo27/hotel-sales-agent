import { beforeEach, describe, expect, it, vi } from "vitest";

const requestStaffReplySend = vi.fn();
const requestReengagementTemplateEnabled = vi.fn();
const rpc = vi.fn();
const maybeSingle = vi.fn();
const getCurrentAppUser = vi.fn();
const revalidatePath = vi.fn();

vi.mock("next/cache", () => ({ revalidatePath }));
vi.mock("@/lib/agentInternal", () => ({
  requestStaffReplySend,
  requestReengagementTemplateEnabled,
}));
vi.mock("@/lib/session", () => ({ getCurrentAppUser }));

interface Result {
  data: unknown;
  error: unknown;
}

// A Supabase query builder that answers with one canned result however it is
// chained, awaited as a list or ended with maybeSingle().
interface Chain extends PromiseLike<Result> {
  select: () => Chain;
  eq: () => Chain;
  order: () => Chain;
  limit: () => Chain;
  overrideTypes: () => Chain;
  maybeSingle: () => Promise<Result>;
}

function chain(result: Result): Chain {
  const query: Chain = {
    select: () => query,
    eq: () => query,
    order: () => query,
    limit: () => query,
    overrideTypes: () => query,
    maybeSingle: () => Promise.resolve(result),
    then: (onFulfilled, onRejected) => Promise.resolve(result).then(onFulfilled, onRejected),
  };
  return query;
}

// What each table the template action reads answers with.
const tables: Record<string, Result> = {};

vi.mock("@/utils/supabase/server", () => ({
  createClient: async () => ({
    rpc,
    from: (table: string) =>
      table === "staff_replies"
        ? { select: () => ({ eq: () => ({ maybeSingle }) }) }
        : chain(tables[table] ?? { data: null, error: null }),
  }),
}));

const { retryStaffReply, sendReengagementTemplate, sendStaffReply } = await import(
  "./replyActions"
);

const ME = { id: "user-me", is_active: true, app_role: "sales" };

beforeEach(() => {
  vi.resetAllMocks();
  getCurrentAppUser.mockResolvedValue(ME);
  for (const table of Object.keys(tables)) {
    delete tables[table];
  }
});

describe("sendStaffReply", () => {
  it("stores the reply under the user's session, then asks the agent to send that id", async () => {
    rpc.mockResolvedValue({ data: 42, error: null });
    requestStaffReplySend.mockResolvedValue("sent");

    expect(await sendStaffReply(5, "حياك الله")).toEqual({ replyId: 42, status: "sent" });

    expect(rpc).toHaveBeenCalledWith("staff_queue_reply", {
      target_conversation_id: 5,
      reply_body: "حياك الله",
    });
    expect(requestStaffReplySend).toHaveBeenCalledWith(42);
    expect(revalidatePath).toHaveBeenCalledWith("/escalations/conversations/5");
  });

  it("passes the agent's refusal through, the reply staying stored", async () => {
    rpc.mockResolvedValue({ data: 42, error: null });
    requestStaffReplySend.mockResolvedValue("outside_window");

    expect(await sendStaffReply(5, "hello")).toEqual({ replyId: 42, status: "outside_window" });
  });

  it("sends nothing for a blank or oversized draft, or a bad conversation id", async () => {
    expect(await sendStaffReply(5, "  \n ")).toHaveProperty("error");
    expect(await sendStaffReply(5, "a".repeat(4097))).toHaveProperty("error");
    expect(await sendStaffReply(0, "hello")).toHaveProperty("error");
    expect(await sendStaffReply(1.5, "hello")).toHaveProperty("error");
    expect(rpc).not.toHaveBeenCalled();
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("sends nothing for a signed-out or deactivated user", async () => {
    getCurrentAppUser.mockResolvedValueOnce(null);
    getCurrentAppUser.mockResolvedValueOnce({ ...ME, is_active: false });

    expect(await sendStaffReply(5, "hello")).toHaveProperty("error");
    expect(await sendStaffReply(5, "hello")).toHaveProperty("error");
    expect(rpc).not.toHaveBeenCalled();
  });

  it("names a refusal because someone else holds the conversation, and asks the agent nothing", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "42501", message: "x" } });

    const result = await sendStaffReply(5, "hello");

    expect(result).toEqual({ error: expect.stringContaining("ليست مستلمة باسمك") });
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("names a refusal because nobody holds the conversation", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "P0002", message: "x" } });

    expect(await sendStaffReply(5, "hello")).toEqual({
      error: expect.stringContaining("غير مستلمة"),
    });
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("reports any other database error without asking the agent", async () => {
    rpc.mockResolvedValue({ data: null, error: { code: "08006", message: "x" } });

    expect(await sendStaffReply(5, "hello")).toEqual({
      error: expect.stringContaining("تعذّر حفظ الرد"),
    });
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("does not ask the agent when no reply id came back", async () => {
    rpc.mockResolvedValue({ data: null, error: null });

    expect(await sendStaffReply(5, "hello")).toHaveProperty("error");
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });
});

describe("retryStaffReply", () => {
  it("asks the agent again for the author's own reply", async () => {
    maybeSingle.mockResolvedValue({ data: { id: 42, conversation_id: 5, sent_by: ME.id } });
    requestStaffReplySend.mockResolvedValue("sent");

    expect(await retryStaffReply(42)).toEqual({ replyId: 42, status: "sent" });
    expect(requestStaffReplySend).toHaveBeenCalledWith(42);
    expect(revalidatePath).toHaveBeenCalledWith("/escalations/conversations/5");
  });

  it("refuses another user's reply, an admin's included", async () => {
    getCurrentAppUser.mockResolvedValue({ ...ME, app_role: "admin" });
    maybeSingle.mockResolvedValue({ data: { id: 42, conversation_id: 5, sent_by: "someone-else" } });

    expect(await retryStaffReply(42)).toEqual({
      error: expect.stringContaining("صلاحية"),
    });
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("asks the agent nothing for a reply the session cannot read", async () => {
    maybeSingle.mockResolvedValue({ data: null });

    expect(await retryStaffReply(42)).toHaveProperty("error");
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("refuses a bad id or a deactivated user", async () => {
    expect(await retryStaffReply(-1)).toHaveProperty("error");
    getCurrentAppUser.mockResolvedValueOnce({ ...ME, is_active: false });
    expect(await retryStaffReply(42)).toHaveProperty("error");
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });
});

describe("sendReengagementTemplate", () => {
  const LONG_AGO = new Date(Date.now() - 30 * 60 * 60 * 1000).toISOString();
  const JUST_NOW = new Date(Date.now() - 60 * 1000).toISOString();

  function windowClosed(): void {
    tables.messages = { data: { created_at: LONG_AGO }, error: null };
    requestReengagementTemplateEnabled.mockResolvedValue(true);
  }

  it("stores the template naming the latest quote's hotel, then asks the agent to send that id", async () => {
    windowClosed();
    tables.quotes = {
      data: [
        { id: 1, hotel_id: 10, created_at: "2026-10-02T10:00:00Z" },
        { id: 2, hotel_id: 20, created_at: "2026-10-02T11:00:00Z" },
      ],
      error: null,
    };
    tables.escalations = { data: [], error: null };
    rpc.mockResolvedValue({ data: 42, error: null });
    requestStaffReplySend.mockResolvedValue("sent");

    expect(await sendReengagementTemplate(5)).toEqual({ replyId: 42, status: "sent" });

    expect(rpc).toHaveBeenCalledWith("staff_queue_template_reply", {
      target_conversation_id: 5,
      hotel_for_template: 20,
    });
    expect(requestStaffReplySend).toHaveBeenCalledWith(42);
    expect(revalidatePath).toHaveBeenCalledWith("/escalations/conversations/5");
  });

  it("names no hotel when nothing in the conversation does", async () => {
    windowClosed();
    tables.quotes = { data: [], error: null };
    tables.escalations = { data: [], error: null };
    rpc.mockResolvedValue({ data: 42, error: null });
    requestStaffReplySend.mockResolvedValue("sent");

    await sendReengagementTemplate(5);

    expect(rpc).toHaveBeenCalledWith("staff_queue_template_reply", {
      target_conversation_id: 5,
      hotel_for_template: null,
    });
  });

  it("passes the agent's refusal through, the template staying stored", async () => {
    windowClosed();
    rpc.mockResolvedValue({ data: 42, error: null });
    requestStaffReplySend.mockResolvedValue("not_configured");

    expect(await sendReengagementTemplate(5)).toEqual({ replyId: 42, status: "not_configured" });
  });

  it("stores and sends nothing while the 24-hour window is open", async () => {
    tables.messages = { data: { created_at: JUST_NOW }, error: null };
    requestReengagementTemplateEnabled.mockResolvedValue(true);

    expect(await sendReengagementTemplate(5)).toEqual({
      error: expect.stringContaining("نافذة الـ24 ساعة مفتوحة"),
    });
    expect(rpc).not.toHaveBeenCalled();
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("treats an unreadable last message as an open window, and stores nothing", async () => {
    tables.messages = { data: null, error: { code: "08006" } };
    requestReengagementTemplateEnabled.mockResolvedValue(true);

    expect(await sendReengagementTemplate(5)).toHaveProperty("error");
    expect(rpc).not.toHaveBeenCalled();
  });

  it("stores and sends nothing while the agent has no template names, saying to call the customer", async () => {
    tables.messages = { data: { created_at: LONG_AGO }, error: null };
    requestReengagementTemplateEnabled.mockResolvedValue(false);

    expect(await sendReengagementTemplate(5)).toEqual({
      error: expect.stringContaining("تواصل مع العميل هاتفياً"),
    });
    expect(rpc).not.toHaveBeenCalled();
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("treats a customer who never wrote as outside the window", async () => {
    tables.messages = { data: null, error: null };
    requestReengagementTemplateEnabled.mockResolvedValue(true);
    rpc.mockResolvedValue({ data: 42, error: null });
    requestStaffReplySend.mockResolvedValue("sent");

    expect(await sendReengagementTemplate(5)).toEqual({ replyId: 42, status: "sent" });
  });

  it("names a refusal because someone else holds the conversation, or nobody does", async () => {
    windowClosed();
    rpc.mockResolvedValueOnce({ data: null, error: { code: "42501" } });
    rpc.mockResolvedValueOnce({ data: null, error: { code: "P0002" } });

    expect(await sendReengagementTemplate(5)).toEqual({
      error: expect.stringContaining("ليست مستلمة باسمك"),
    });
    expect(await sendReengagementTemplate(5)).toEqual({
      error: expect.stringContaining("غير مستلمة"),
    });
    expect(requestStaffReplySend).not.toHaveBeenCalled();
  });

  it("refuses a bad conversation id, a signed-out or a deactivated user", async () => {
    expect(await sendReengagementTemplate(0)).toHaveProperty("error");
    getCurrentAppUser.mockResolvedValueOnce(null);
    expect(await sendReengagementTemplate(5)).toHaveProperty("error");
    getCurrentAppUser.mockResolvedValueOnce({ ...ME, is_active: false });
    expect(await sendReengagementTemplate(5)).toHaveProperty("error");
    expect(rpc).not.toHaveBeenCalled();
  });
});
