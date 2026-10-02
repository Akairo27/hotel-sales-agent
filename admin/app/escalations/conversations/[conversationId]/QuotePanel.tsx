"use client";

import { formatRiyadhClock } from "@/lib/escalations";
import { quoteValidity, quoteValidUntil } from "@/lib/quoteStatus";
import { BADGE, BADGE_ACCENT, BUTTON_SECONDARY, HINT } from "@/lib/ui";
import { useWorkspace } from "./ChatWorkspace";

/** A quote as the panel shows it: names resolved and the texts written on
 * the server, so this component only decides the badges and the button. */
export interface PanelQuote {
  id: number;
  title: string;
  details: string;
  createdAt: string;
  bookingRequested: boolean;
  /** The short text «إدراج في الرد» puts in the reply box. */
  replyText: string;
}

const WINDOW_CLOSED_TITLE = "الرد الحر غير متاح خارج نافذة 24 ساعة.";

function QuoteEntry({ quote }: { quote: PanelQuote }) {
  const { now, canReply, windowOpen, insertIntoDraft } = useWorkspace();
  const validity = quoteValidity(quote.createdAt, now);
  return (
    <li className="rounded-xl border border-border p-3">
      <p className="font-medium">{quote.title}</p>
      <p className={HINT}>{quote.details}</p>
      <p className="mt-2 flex flex-wrap items-center gap-2">
        {quote.bookingRequested && <span className={BADGE_ACCENT}>طُلب حجزه</span>}
        {validity === "valid" ? (
          <span className={BADGE_ACCENT}>
            صالح حتى <span dir="ltr">{formatRiyadhClock(quoteValidUntil(quote.createdAt))}</span>
          </span>
        ) : (
          <span className={BADGE}>منتهي</span>
        )}
        {validity === "valid" && canReply && (
          <button
            type="button"
            onClick={() => insertIntoDraft(quote.replyText)}
            disabled={!windowOpen}
            title={windowOpen ? undefined : WINDOW_CLOSED_TITLE}
            className={`${BUTTON_SECONDARY} ms-auto px-3 py-1`}
          >
            إدراج في الرد
          </button>
        )}
      </p>
    </li>
  );
}

// The conversation's quotes in the side panel, newest first, each with its
// status (owner request 2026-10-02): valid until a time, expired, and/or
// booking requested. A valid one can be put into the reply box -- never
// sent from here.
export function QuotePanel({ quotes }: { quotes: PanelQuote[] }) {
  return (
    <ul className="grid gap-2">
      {quotes.map((quote) => (
        <QuoteEntry key={quote.id} quote={quote} />
      ))}
    </ul>
  );
}
