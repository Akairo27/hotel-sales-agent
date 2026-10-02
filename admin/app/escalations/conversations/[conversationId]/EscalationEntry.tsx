import { isOpen } from "@/lib/escalationCustomers";
import {
  escalationLabel,
  formatAge,
  formatRiyadhDateTime,
  parseNotes,
  technicalNotes,
} from "@/lib/escalations";
import type { EscalationRow, QuoteSummaryRow } from "@/lib/types";
import { BADGE, BADGE_ACCENT, HINT } from "@/lib/ui";
import { ReasonDetails } from "../../_parts/ReasonDetails";
import type { StayNames } from "../../_parts/names";

// One escalation in the side panel: its reason and age, opening to what it
// is about (ReasonDetails) and its technical notes.
export function EscalationEntry({
  escalation,
  quotes,
  names,
  now,
}: {
  escalation: EscalationRow;
  quotes: QuoteSummaryRow[];
  names: StayNames;
  now: Date;
}) {
  const notes = parseNotes(escalation.notes);
  const technical = technicalNotes(notes);
  const open = isOpen(escalation);
  return (
    <details
      id={`escalation-${escalation.id}`}
      open={open}
      className="rounded-xl border border-border p-3 text-sm target:border-accent"
    >
      <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">رقم {escalation.id}</span>
        <span className={open ? BADGE_ACCENT : BADGE}>{escalationLabel(escalation.reason)}</span>
        <span className={HINT}>
          <span dir="ltr">{formatRiyadhDateTime(escalation.opened_at)}</span> ·{" "}
          {formatAge(escalation.opened_at, now)}
        </span>
        <span className={HINT}>{open ? "مفتوح" : "مغلق"}</span>
      </summary>
      <div className="mt-3 min-w-0 break-words">
        <ReasonDetails
          escalation={escalation}
          notes={notes}
          quote={quotes.find((quote) => quote.id === escalation.quote_id)}
          names={names}
          now={now}
        />
        {technical.length > 0 && (
          <details className="mt-3">
            <summary className={HINT}>تفاصيل تقنية</summary>
            <dl className="mt-2 grid gap-1 text-sm" dir="ltr">
              {technical.map(([key, value]) => (
                <div key={key} className="min-w-0">
                  <dt className="inline font-medium">{key}: </dt>
                  <dd className="inline break-all">{value}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </div>
    </details>
  );
}
