import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AGENT_BASE_URL,
  AGENT_REQUEST_TIMEOUT_MS,
  STAFF_REPLY_REQUEST_TIMEOUT_MS,
  requestReengagementTemplateEnabled,
  requestStaffReplySend,
  requestTakeoverNotice,
} from "./agentInternal";

// A placeholder, never a real token.
const TOKEN = "test-agent-internal-token-placeholder";

function answer(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("requestTakeoverNotice", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("AGENT_INTERNAL_TOKEN", TOKEN);
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("posts to the agent's loopback endpoint with the Bearer token and a timeout", async () => {
    fetchMock.mockResolvedValue(answer(200, { status: "sent" }));

    expect(await requestTakeoverNotice(42)).toBe("sent");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${AGENT_BASE_URL}/internal/takeovers/42/acknowledge`);
    expect(AGENT_BASE_URL).toBe("http://127.0.0.1:8000");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ Authorization: `Bearer ${TOKEN}` });
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("returns the agent's status for each code it answers with", async () => {
    fetchMock.mockResolvedValueOnce(answer(200, { status: "outside_window" }));
    fetchMock.mockResolvedValueOnce(answer(404, { status: "not_found" }));
    fetchMock.mockResolvedValueOnce(answer(502, { status: "failed" }));
    fetchMock.mockResolvedValueOnce(answer(503, { status: "unavailable" }));

    expect(await requestTakeoverNotice(1)).toBe("outside_window");
    expect(await requestTakeoverNotice(1)).toBe("not_found");
    expect(await requestTakeoverNotice(1)).toBe("failed");
    expect(await requestTakeoverNotice(1)).toBe("unavailable");
  });

  it("calls nothing and reports unreachable when no token is configured", async () => {
    vi.stubEnv("AGENT_INTERNAL_TOKEN", "");

    expect(await requestTakeoverNotice(1)).toBe("unreachable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports a refused token, a network error or a timeout as unreachable", async () => {
    fetchMock.mockResolvedValueOnce(answer(401, { detail: "unauthorized" }));
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    fetchMock.mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"));

    expect(await requestTakeoverNotice(1)).toBe("unreachable");
    expect(await requestTakeoverNotice(1)).toBe("unreachable");
    expect(await requestTakeoverNotice(1)).toBe("unreachable");
  });

  it("never logs the token", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    fetchMock.mockResolvedValueOnce(answer(401, {}));
    fetchMock.mockRejectedValueOnce(new TypeError(`failed with ${TOKEN}`));

    await requestTakeoverNotice(1);
    await requestTakeoverNotice(1);

    expect(logged).toHaveBeenCalled();
    expect(JSON.stringify(logged.mock.calls)).not.toContain(TOKEN);
  });
});

describe("requestStaffReplySend", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("AGENT_INTERNAL_TOKEN", TOKEN);
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("posts only the reply id to the agent's loopback endpoint, with the Bearer token", async () => {
    fetchMock.mockResolvedValue(answer(200, { status: "sent" }));

    expect(await requestStaffReplySend(7)).toBe("sent");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${AGENT_BASE_URL}/internal/staff-replies/7/send`);
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ Authorization: `Bearer ${TOKEN}` });
    expect(init?.body).toBeUndefined();
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("waits longer than the notice does, for the WhatsApp send itself", () => {
    expect(STAFF_REPLY_REQUEST_TIMEOUT_MS).toBeGreaterThan(AGENT_REQUEST_TIMEOUT_MS);
  });

  it("returns the agent's status for each code it answers with", async () => {
    fetchMock.mockResolvedValueOnce(answer(200, { status: "outside_window" }));
    fetchMock.mockResolvedValueOnce(answer(200, { status: "already_claimed" }));
    fetchMock.mockResolvedValueOnce(answer(404, { status: "not_found" }));
    fetchMock.mockResolvedValueOnce(answer(502, { status: "failed" }));
    fetchMock.mockResolvedValueOnce(answer(503, { status: "unavailable" }));

    expect(await requestStaffReplySend(1)).toBe("outside_window");
    expect(await requestStaffReplySend(1)).toBe("already_claimed");
    expect(await requestStaffReplySend(1)).toBe("not_found");
    expect(await requestStaffReplySend(1)).toBe("failed");
    expect(await requestStaffReplySend(1)).toBe("unavailable");
  });

  it("calls nothing and reports unreachable when no token is configured", async () => {
    vi.stubEnv("AGENT_INTERNAL_TOKEN", "");

    expect(await requestStaffReplySend(1)).toBe("unreachable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports a refused token, a network error, a timeout or a non-JSON answer as unreachable", async () => {
    fetchMock.mockResolvedValueOnce(answer(401, { detail: "unauthorized" }));
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    fetchMock.mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"));
    fetchMock.mockResolvedValueOnce(new Response("<html>", { status: 200 }));

    for (let attempt = 0; attempt < 4; attempt += 1) {
      expect(await requestStaffReplySend(1)).toBe("unreachable");
    }
  });

  it("logs the reply id under its own key, and never the token", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    fetchMock.mockResolvedValueOnce(answer(401, {}));
    fetchMock.mockRejectedValueOnce(new TypeError(`failed with ${TOKEN}`));

    await requestStaffReplySend(5);
    await requestStaffReplySend(5);

    expect(logged).toHaveBeenCalledTimes(2);
    const first = JSON.parse(String(logged.mock.calls[0][0])) as Record<string, unknown>;
    expect(first).toMatchObject({ event: "agent_internal_token_refused", staff_reply_id: 5 });
    expect(first).not.toHaveProperty("takeover_id");
    expect(JSON.stringify(logged.mock.calls)).not.toContain(TOKEN);
  });
});

describe("requestReengagementTemplateEnabled", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("AGENT_INTERNAL_TOKEN", TOKEN);
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("reads the agent's loopback status endpoint with a GET and the Bearer token", async () => {
    fetchMock.mockResolvedValue(answer(200, { enabled: true }));

    expect(await requestReengagementTemplateEnabled()).toBe(true);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${AGENT_BASE_URL}/internal/reengagement-template`);
    expect(init?.method).toBe("GET");
    expect(init?.headers).toEqual({ Authorization: `Bearer ${TOKEN}` });
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("is true only for an explicit enabled: true", async () => {
    fetchMock.mockResolvedValueOnce(answer(200, { enabled: false }));
    fetchMock.mockResolvedValueOnce(answer(200, { enabled: "yes" }));
    fetchMock.mockResolvedValueOnce(answer(200, {}));

    expect(await requestReengagementTemplateEnabled()).toBe(false);
    expect(await requestReengagementTemplateEnabled()).toBe(false);
    expect(await requestReengagementTemplateEnabled()).toBe(false);
  });

  it("is false, calling nothing, with no token", async () => {
    vi.stubEnv("AGENT_INTERNAL_TOKEN", "");

    expect(await requestReengagementTemplateEnabled()).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is false for a refused token, a network error, a timeout or a non-JSON answer, and logs no token", async () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    fetchMock.mockResolvedValueOnce(answer(401, {}));
    fetchMock.mockRejectedValueOnce(new TypeError(`failed with ${TOKEN}`));
    fetchMock.mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"));
    fetchMock.mockResolvedValueOnce(new Response("<html>", { status: 200 }));

    for (let attempt = 0; attempt < 4; attempt += 1) {
      expect(await requestReengagementTemplateEnabled()).toBe(false);
    }
    expect(JSON.stringify(logged.mock.calls)).not.toContain(TOKEN);
  });
});
