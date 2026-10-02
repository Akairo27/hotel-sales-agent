import { beforeEach, describe, expect, it, vi } from "vitest";

const requestStaffReplySend = vi.fn();
const rpc = vi.fn();
const maybeSingle = vi.fn();
const getCurrentAppUser = vi.fn();
const revalidatePath = vi.fn();

vi.mock("next/cache", () => ({ revalidatePath }));
vi.mock("@/lib/agentInternal", () => ({ requestStaffReplySend }));
vi.mock("@/lib/session", () => ({ getCurrentAppUser }));
vi.mock("@/utils/supabase/server", () => ({
  createClient: async () => ({
    rpc,
    from: () => ({ select: () => ({ eq: () => ({ maybeSingle }) }) }),
  }),
}));

const { retryStaffReply, sendStaffReply } = await import("./replyActions");

const ME = { id: "user-me", is_active: true, app_role: "sales" };

beforeEach(() => {
  vi.resetAllMocks();
  getCurrentAppUser.mockResolvedValue(ME);
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
