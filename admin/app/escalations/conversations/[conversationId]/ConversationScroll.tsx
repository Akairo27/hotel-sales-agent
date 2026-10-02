"use client";

import { type ReactNode, useEffect, useRef } from "react";

// Within this distance of the bottom the reader counts as following the
// conversation, so a new message keeps the view at the newest one.
const FOLLOW_THRESHOLD_PX = 80;

// The conversation, oldest first, in its own scroll area that opens at the
// newest message (owner request 2026-10-02). A live refresh that adds a
// message keeps the newest in view, unless the reader has scrolled up.
export function ConversationScroll({
  messageCount,
  children,
}: {
  messageCount: number;
  children: ReactNode;
}) {
  const area = useRef<HTMLDivElement>(null);
  const following = useRef(true);

  useEffect(() => {
    const element = area.current;
    if (element && following.current) {
      element.scrollTop = element.scrollHeight;
    }
  }, [messageCount]);

  const onScroll = () => {
    const element = area.current;
    if (element) {
      following.current =
        element.scrollHeight - element.scrollTop - element.clientHeight < FOLLOW_THRESHOLD_PX;
    }
  };

  return (
    <div
      ref={area}
      onScroll={onScroll}
      tabIndex={0}
      aria-label="المحادثة"
      className="mt-3 max-h-[70vh] min-w-0 overflow-y-auto pe-1"
    >
      {children}
    </div>
  );
}
