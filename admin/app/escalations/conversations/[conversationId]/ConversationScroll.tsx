"use client";

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { BUTTON_PRIMARY } from "@/lib/ui";

// Within this distance of the bottom the reader counts as following the
// conversation, so a new message keeps the view at the newest one.
const FOLLOW_THRESHOLD_PX = 80;

/** How many of the messages are newer than the last one the reader has
 * seen: ids only grow, and the page shows a capped window of them, so
 * counting ids is exact where counting messages would not be. */
export function unseenCount(messageIds: readonly number[], seenId: number): number {
  return messageIds.filter((id) => id > seenId).length;
}

export function latestButtonLabel(unseen: number): string {
  return unseen > 0 ? `آخر الرسائل (${unseen})` : "آخر الرسائل";
}

function newestId(messageIds: readonly number[]): number {
  return messageIds.reduce((newest, id) => Math.max(newest, id), 0);
}

// The conversation, oldest first, in its own scroll area that opens at the
// newest message (owner request 2026-10-02). A live refresh that adds a
// message keeps the newest in view, unless the reader has scrolled up --
// then a floating «آخر الرسائل» button, with the count of messages that
// arrived meanwhile, takes them back down.
export function ConversationScroll({
  messageIds,
  children,
}: {
  messageIds: readonly number[];
  children: ReactNode;
}) {
  const area = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  // Whether the reader follows the newest message; a ref as well as state so
  // the resize observer below reads it without being recreated.
  const following = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const latestId = newestId(messageIds);
  const [seenId, setSeenId] = useState(latestId);

  const scrollToEnd = () => {
    const element = area.current;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
  };

  // Opens at the newest message, before the first paint.
  useLayoutEffect(scrollToEnd, []);

  // The conversation's height can still change after that (fonts loading,
  // the reply box and the panel settling): while the reader follows, keep
  // the newest message in view.
  useEffect(() => {
    const element = content.current;
    if (!element) {
      return;
    }
    const observer = new ResizeObserver(() => {
      if (following.current) {
        scrollToEnd();
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (atBottom) {
      scrollToEnd();
    }
  }, [latestId, atBottom]);

  const onScroll = () => {
    const element = area.current;
    if (element) {
      const near =
        element.scrollHeight - element.scrollTop - element.clientHeight < FOLLOW_THRESHOLD_PX;
      following.current = near;
      setAtBottom(near);
      if (near) {
        // At the bottom everything is seen; this also runs for the scroll the
        // effect above makes when a new message arrives while following.
        setSeenId(latestId);
      }
    }
  };

  const goToLatest = () => {
    area.current?.scrollTo({ top: area.current.scrollHeight, behavior: "smooth" });
  };

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={area}
        onScroll={onScroll}
        tabIndex={0}
        aria-label="المحادثة"
        className="absolute inset-0 min-w-0 overflow-y-auto px-3 py-3 sm:px-4"
      >
        <div ref={content}>{children}</div>
      </div>
      {!atBottom && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <button
            type="button"
            onClick={goToLatest}
            className={`${BUTTON_PRIMARY} pointer-events-auto shadow-lg`}
          >
            {latestButtonLabel(unseenCount(messageIds, seenId))}
          </button>
        </div>
      )}
    </div>
  );
}
