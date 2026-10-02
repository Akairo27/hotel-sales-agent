"use client";

import {
  type ReactNode,
  type RefObject,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { isWithinCustomerServiceWindow } from "@/lib/staffReply";
import { BUTTON_SECONDARY } from "@/lib/ui";

// The customer page's shared client state (owner request 2026-10-02): the
// clock the quote badges and the 24-hour indicator read, the reply draft the
// quotes' «إدراج في الرد» writes into, and whether the side panel is open.
// The panel and the reply box are far apart in the page, so they meet here.

// A minute-level clock is enough for «valid until» and the window's time
// left; ticking faster would only re-render the page.
const CLOCK_TICK_MS = 30_000;

// Tailwind's lg breakpoint: from here the panel is a side column, below it a
// sheet over the conversation.
const DESKTOP_QUERY = "(min-width: 64rem)";

interface Workspace {
  /** The time the page runs on: the server's, advancing with the browser's
   * clock, so a browser clock that is off does not move the window. */
  now: Date;
  /** Whether the signed-in user holds the takeover and so may reply. */
  canReply: boolean;
  /** Whether free text can still be sent to this customer. */
  windowOpen: boolean;
  lastInboundAt: string | null;
  draft: string;
  setDraft: (draft: string) => void;
  /** Appends text to the draft (no sending), focuses the reply box and, on
   * a narrow screen, closes the sheet that held the button pressed. */
  insertIntoDraft: (text: string) => void;
  replyInput: RefObject<HTMLTextAreaElement | null>;
  desktopPanelOpen: boolean;
  sheetOpen: boolean;
  togglePanel: () => void;
  closeSheet: () => void;
}

const WorkspaceContext = createContext<Workspace | null>(null);

export function useWorkspace(): Workspace {
  const workspace = useContext(WorkspaceContext);
  if (workspace === null) {
    throw new Error("useWorkspace must be used inside ChatWorkspaceProvider");
  }
  return workspace;
}

export function ChatWorkspaceProvider({
  serverNow,
  canReply,
  lastInboundAt,
  children,
}: {
  serverNow: string;
  canReply: boolean;
  lastInboundAt: string | null;
  children: ReactNode;
}) {
  const [now, setNow] = useState(() => new Date(serverNow));
  const [syncedServerNow, setSyncedServerNow] = useState(serverNow);
  const [draft, setDraft] = useState("");
  const [desktopPanelOpen, setDesktopPanelOpen] = useState(true);
  const [sheetOpen, setSheetOpen] = useState(false);
  const replyInput = useRef<HTMLTextAreaElement>(null);
  const clockOffsetMs = useRef(0);

  // Each render from the server (a live refresh included) re-anchors the
  // clock to the server's, then it advances by the browser's.
  if (syncedServerNow !== serverNow) {
    setSyncedServerNow(serverNow);
    setNow(new Date(serverNow));
  }
  useEffect(() => {
    clockOffsetMs.current = Date.parse(serverNow) - Date.now();
    const tick = setInterval(
      () => setNow(new Date(Date.now() + clockOffsetMs.current)),
      CLOCK_TICK_MS,
    );
    return () => clearInterval(tick);
  }, [serverNow]);

  const closeSheet = useCallback(() => setSheetOpen(false), []);

  const togglePanel = useCallback(() => {
    if (window.matchMedia(DESKTOP_QUERY).matches) {
      setDesktopPanelOpen((open) => !open);
    } else {
      setSheetOpen((open) => !open);
    }
  }, []);

  const insertIntoDraft = useCallback((text: string) => {
    setDraft((current) => (current.trim() === "" ? text : `${current}\n${text}`));
    setSheetOpen(false);
    replyInput.current?.focus();
  }, []);

  useEffect(() => {
    if (!sheetOpen) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSheetOpen(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [sheetOpen]);

  const workspace = useMemo<Workspace>(
    () => ({
      now,
      canReply,
      windowOpen: isWithinCustomerServiceWindow(lastInboundAt, now),
      lastInboundAt,
      draft,
      setDraft,
      insertIntoDraft,
      replyInput,
      desktopPanelOpen,
      sheetOpen,
      togglePanel,
      closeSheet,
    }),
    [
      now,
      canReply,
      lastInboundAt,
      draft,
      insertIntoDraft,
      desktopPanelOpen,
      sheetOpen,
      togglePanel,
      closeSheet,
    ],
  );

  return <WorkspaceContext.Provider value={workspace}>{children}</WorkspaceContext.Provider>;
}

/** The header's button for the side panel: collapses it on a wide screen,
 * opens it as a sheet on a narrow one. */
export function PanelToggleButton() {
  const { desktopPanelOpen, sheetOpen, togglePanel } = useWorkspace();
  return (
    <button
      type="button"
      onClick={togglePanel}
      aria-expanded={desktopPanelOpen || sheetOpen}
      aria-controls="chat-side-panel"
      className={BUTTON_SECONDARY}
    >
      التفاصيل
    </button>
  );
}

/** The side panel: a collapsible column beside the conversation on a wide
 * screen, a bottom sheet over it on a narrow one (one element, restyled by
 * breakpoint, so its content is rendered once). */
export function SidePanel({ children }: { children: ReactNode }) {
  const { desktopPanelOpen, sheetOpen, closeSheet } = useWorkspace();
  const mobile = sheetOpen
    ? "fixed inset-x-0 bottom-0 z-40 flex max-h-[85dvh] rounded-t-2xl border-t"
    : "hidden";
  const desktop = desktopPanelOpen
    ? "lg:static lg:z-auto lg:flex lg:max-h-none lg:w-96 lg:shrink-0 lg:rounded-none lg:border-t-0 lg:border-s"
    : "lg:hidden";
  return (
    <>
      {sheetOpen && (
        <div
          aria-hidden="true"
          onClick={closeSheet}
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
        />
      )}
      <aside
        id="chat-side-panel"
        aria-label="تفاصيل العميل"
        className={`${mobile} ${desktop} min-h-0 flex-col border-border bg-surface`}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-2 lg:hidden">
          <span className="text-sm font-semibold">التفاصيل</span>
          <button type="button" onClick={closeSheet} className="text-sm text-muted-foreground">
            إغلاق
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4">{children}</div>
      </aside>
    </>
  );
}
