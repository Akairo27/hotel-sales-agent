import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ChatWorkspaceProvider, PanelToggleButton, SidePanel } from "./ChatWorkspace";

const NOW = "2026-10-02T12:00:00Z";

function render(): string {
  return renderToStaticMarkup(
    <ChatWorkspaceProvider serverNow={NOW} canReply={false} lastInboundAt={null}>
      <PanelToggleButton />
      <SidePanel>
        <p>محتوى اللوحة</p>
      </SidePanel>
    </ChatWorkspaceProvider>,
  );
}

describe("SidePanel", () => {
  it("is a side column on a wide screen and hidden on a narrow one until opened", () => {
    const html = render();
    expect(html).toContain('id="chat-side-panel"');
    expect(html).toMatch(/class="hidden lg:static[^"]*lg:w-96/);
  });

  it("renders its content once, not once per breakpoint", () => {
    expect(render().split("محتوى اللوحة").length - 1).toBe(1);
  });

  it("has its toggle in the header, pointing at it", () => {
    const html = render();
    expect(html).toContain('aria-controls="chat-side-panel"');
    expect(html).toContain("التفاصيل");
  });
});

describe("useWorkspace", () => {
  it("refuses to be used outside the provider", async () => {
    const { useWorkspace } = await import("./ChatWorkspace");
    const Probe = () => {
      useWorkspace();
      return null;
    };
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderToStaticMarkup(<Probe />)).toThrow("ChatWorkspaceProvider");
    quiet.mockRestore();
  });
});
