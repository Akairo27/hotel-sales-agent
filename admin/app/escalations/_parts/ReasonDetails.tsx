import {
  escalationGroup,
  formatStayDate,
  halalasList,
  notOpenStays,
  stringList,
} from "@/lib/escalations";
import { formatHalalasAsRiyal } from "@/lib/money";
import type { EscalationRow, QuoteSummaryRow } from "@/lib/types";
import { HINT } from "@/lib/ui";
import type { StayNames } from "./names";

// What an escalation is about, in words, per reason group: the booking
// request's stay, the nights not yet open, the reply the guard blocked, or
// the media type -- otherwise that the customer got the fallback. Stay
// dates read as the bot writes them («20 أكتوبر»), not ISO.
export function ReasonDetails({
  escalation,
  notes,
  quote,
  names,
  now,
}: {
  escalation: EscalationRow;
  notes: Record<string, unknown>;
  quote: QuoteSummaryRow | undefined;
  names: StayNames;
  now: Date;
}) {
  const group = escalationGroup(escalation.reason);
  if (group === "booking" && quote) {
    return (
      <p>
        طلب العميل حجز {names.hotels.get(quote.hotel_id) ?? `الفندق ${quote.hotel_id}`}،{" "}
        {names.roomTypes.get(quote.room_type_id) ?? `نوع الغرفة ${quote.room_type_id}`}، من{" "}
        {formatStayDate(quote.check_in, now)} إلى {formatStayDate(quote.check_out, now)}، عدد
        الغرف {quote.rooms}، الإجمالي {formatHalalasAsRiyal(quote.ask_price_total)} (العرض رقم{" "}
        {quote.id}).
      </p>
    );
  }
  if (group === "dates_not_open") {
    return (
      <ul className="list-disc ps-5">
        {notOpenStays(notes).map((stay) => (
          <li key={`${stay.hotelId}-${stay.roomTypeId}`}>
            {names.hotels.get(stay.hotelId) ?? `الفندق ${stay.hotelId}`}،{" "}
            {names.roomTypes.get(stay.roomTypeId) ?? `نوع الغرفة ${stay.roomTypeId}`}: ليالٍ لم
            يُفتح حجزها {stay.nights.map((night) => formatStayDate(night, now)).join("، ")}
          </li>
        ))}
      </ul>
    );
  }
  if (group === "guard") {
    const claims = stringList(notes.booking_claims);
    const amounts = halalasList(notes.blocked_amounts_halalas);
    return (
      <div className="space-y-2">
        <p>حجب حارس الرسائل هذا الرد ولم يصل العميل، ووصلته الرسالة الاحتياطية بدلاً منه:</p>
        {typeof notes.blocked_reply_text === "string" && (
          <blockquote className="whitespace-pre-wrap rounded-xl border border-border bg-surface-subtle p-3">
            {notes.blocked_reply_text}
          </blockquote>
        )}
        {claims.length > 0 && <p>عبارات ادعاء حجز: {claims.join("، ")}</p>}
        {amounts.length > 0 && (
          <p>مبالغ محجوبة: {amounts.map((amount) => formatHalalasAsRiyal(amount)).join("، ")}</p>
        )}
      </div>
    );
  }
  if (group === "media" && typeof notes.message_type === "string") {
    return <p>نوع الرسالة: {notes.message_type}</p>;
  }
  return <p className={HINT}>وصلت العميلَ الرسالة الاحتياطية بأن زميلاً سيتواصل معه.</p>;
}
