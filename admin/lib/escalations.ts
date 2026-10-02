// The escalations screen's display rules (staff notification, step 1;
// owner decisions 2026-10-01, ARCHITECTURE.md §7): which group a reason
// belongs to and its Arabic label (the same labels the WhatsApp staff
// alert will carry), the masked phone the list shows, Riyadh times, stay
// dates as the bot writes them, the age of an escalation, and the notes an
// escalation carries, read safely.
//
// A reason this file does not know (a newer agent writing a new one) falls
// into "failure": shown, never hidden.
import { monthName } from "@/lib/monthNames";

export type EscalationGroup =
  | "booking"
  | "dates_not_open"
  | "guard"
  | "media"
  | "caps"
  | "unanswered"
  | "failure";

// Owner-approved labels (2026-10-01).
export const ESCALATION_GROUP_LABELS: Record<EscalationGroup, string> = {
  booking: "طلب حجز",
  dates_not_open: "تواريخ لم يُفتح حجزها",
  guard: "رد محجوب للمراجعة",
  media: "رسالة وسائط",
  caps: "تجاوز حد الاستخدام",
  unanswered: "رسالة لم يُرد عليها",
  failure: "تعذّر الرد تلقائياً",
};

export const ESCALATION_GROUPS = Object.keys(ESCALATION_GROUP_LABELS) as EscalationGroup[];

const GUARD_REASON_PREFIX = "output_guard_violation_";

const REASON_GROUPS: Record<string, EscalationGroup> = {
  booking_requested: "booking",
  dates_not_open_for_booking: "dates_not_open",
  unsupported_message_type: "media",
  turn_cap_exceeded: "caps",
  token_spend_cap_exceeded: "caps",
  number_daily_token_cap_exceeded: "caps",
  daily_spend_cap_exceeded: "caps",
  message_rate_cap_exceeded: "caps",
  unanswered_at_startup: "unanswered",
};

export function escalationGroup(reason: string): EscalationGroup {
  if (reason.startsWith(GUARD_REASON_PREFIX)) {
    return "guard";
  }
  return REASON_GROUPS[reason] ?? "failure";
}

export function escalationLabel(reason: string): string {
  return ESCALATION_GROUP_LABELS[escalationGroup(reason)];
}

const VISIBLE_PHONE_DIGITS = 4;

/** The list shows only the last four digits; the full number is on the
 * escalation's own page, where staff call the customer from. */
export function maskPhone(phone: string): string {
  return `***${phone.slice(-VISIBLE_PHONE_DIGITS)}`;
}

const RIYADH_TIME = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Riyadh",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** "2026-10-01 08:50", Riyadh time, Western digits. */
export function formatRiyadhDateTime(instant: string): string {
  const parts = Object.fromEntries(
    RIYADH_TIME.formatToParts(new Date(instant)).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/** The time of day in Riyadh («14:05») -- what a quote's «valid until»
 * reads as. */
export function formatRiyadhClock(instant: string | number): string {
  const parts = Object.fromEntries(
    RIYADH_TIME.formatToParts(new Date(instant)).map((part) => [part.type, part.value]),
  );
  return `${parts.hour}:${parts.minute}`;
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A stay date ("2026-10-20") as the bot writes it to customers: «20
 * أكتوبر», Western digits (owner request 2026-10-02), with the year added
 * only when it is not the current year in Riyadh («5 يناير 2027»). Anything
 * that is not such a date is returned as it is, never hidden. */
export function formatStayDate(isoDate: string, now: Date): string {
  const match = ISO_DATE.exec(isoDate);
  if (!match) {
    return isoDate;
  }
  const [, year, month, day] = match;
  const dayAndMonth = `${Number(day)} ${monthName("gregorian", Number(month))}`;
  return year === riyadhYear(now) ? dayAndMonth : `${dayAndMonth} ${year}`;
}

function riyadhYear(now: Date): string {
  return RIYADH_TIME.formatToParts(now).find((part) => part.type === "year")?.value ?? "";
}

interface ArabicCountForms {
  one: string;
  two: string;
  few: string;
  many: string;
}

// Arabic counts in three forms: the dual for two, a plural for three to
// ten, and the singular again from eleven -- the same rule as the agent's
// night and room counts (services/agent/llm/quote_display.py).
const ARABIC_DUAL_COUNT = 2;
const ARABIC_PLURAL_MAX_COUNT = 10;

function arabicCount(count: number, forms: ArabicCountForms): string {
  if (count === 1) {
    return forms.one;
  }
  if (count === ARABIC_DUAL_COUNT) {
    return forms.two;
  }
  if (count <= ARABIC_PLURAL_MAX_COUNT) {
    return `${count} ${forms.few}`;
  }
  return `${count} ${forms.many}`;
}

const MINUTES: ArabicCountForms = { one: "دقيقة", two: "دقيقتين", few: "دقائق", many: "دقيقة" };
const HOURS: ArabicCountForms = { one: "ساعة", two: "ساعتين", few: "ساعات", many: "ساعة" };
const DAYS: ArabicCountForms = { one: "يوم", two: "يومين", few: "أيام", many: "يوماً" };
const MS_PER_MINUTE = 60_000;
const MINUTES_PER_HOUR = 60;
const HOURS_PER_DAY = 24;

function twoUnits(
  whole: number,
  wholeForms: ArabicCountForms,
  part: number,
  partForms: ArabicCountForms,
): string {
  const first = arabicCount(whole, wholeForms);
  return part === 0 ? first : `${first} و${arabicCount(part, partForms)}`;
}

/** How much of a period is left, as «باقي 5 ساعات و12 دقيقة» (the same
 * parts as formatAge, never rounded up): «أقل من دقيقة» under a minute,
 * nothing at or below zero. */
export function formatTimeLeft(remainingMs: number): string {
  const minutes = Math.floor(remainingMs / MS_PER_MINUTE);
  if (remainingMs <= 0) {
    return "";
  }
  if (minutes < 1) {
    return "أقل من دقيقة";
  }
  if (minutes < MINUTES_PER_HOUR) {
    return `باقي ${arabicCount(minutes, MINUTES)}`;
  }
  const hours = Math.floor(minutes / MINUTES_PER_HOUR);
  if (hours < HOURS_PER_DAY) {
    return `باقي ${twoUnits(hours, HOURS, minutes % MINUTES_PER_HOUR, MINUTES)}`;
  }
  return `باقي ${twoUnits(Math.floor(hours / HOURS_PER_DAY), DAYS, hours % HOURS_PER_DAY, HOURS)}`;
}

/** How long ago something happened, never rounded away (owner request
 * 2026-10-01: 1 hour 49 minutes used to read «منذ ساعة»): minutes under an
 * hour, hours and minutes under a day, days and hours beyond -- «الآن»,
 * «منذ 49 دقيقة», «منذ ساعة و49 دقيقة», «منذ ساعتين», «منذ يوم و4 ساعات».
 * A part that is zero is left out. */
export function formatAge(openedAt: string, now: Date): string {
  const minutes = Math.floor((now.getTime() - new Date(openedAt).getTime()) / MS_PER_MINUTE);
  if (minutes < 1) {
    return "الآن";
  }
  if (minutes < MINUTES_PER_HOUR) {
    return `منذ ${arabicCount(minutes, MINUTES)}`;
  }
  const hours = Math.floor(minutes / MINUTES_PER_HOUR);
  if (hours < HOURS_PER_DAY) {
    return `منذ ${twoUnits(hours, HOURS, minutes % MINUTES_PER_HOUR, MINUTES)}`;
  }
  return `منذ ${twoUnits(Math.floor(hours / HOURS_PER_DAY), DAYS, hours % HOURS_PER_DAY, HOURS)}`;
}

/** An escalation's notes column, which the agent writes as JSON text: the
 * parsed object, or an empty one for anything else (null, malformed, or
 * not an object). */
export function parseNotes(notes: string | null): Record<string, unknown> {
  if (!notes) {
    return {};
  }
  try {
    const parsed: unknown = JSON.parse(notes);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

export interface NotOpenStay {
  hotelId: number;
  roomTypeId: number;
  nights: string[];
}

/** The stays a dates_not_open_for_booking escalation lists
 * (services/agent/staff_follow_up.py), skipping any malformed entry. */
export function notOpenStays(notes: Record<string, unknown>): NotOpenStay[] {
  const stays = notes.stays;
  if (!Array.isArray(stays)) {
    return [];
  }
  return stays.flatMap((stay: unknown) => {
    if (stay === null || typeof stay !== "object") {
      return [];
    }
    const { hotel_id: hotelId, room_type_id: roomTypeId, nights } = stay as Record<string, unknown>;
    if (typeof hotelId !== "number" || typeof roomTypeId !== "number" || !Array.isArray(nights)) {
      return [];
    }
    return [{ hotelId, roomTypeId, nights: nights.filter((night) => typeof night === "string") }];
  });
}

/** The fields shown on their own in the reason section, so the technical
 * details list leaves them out; "retention" is an internal reminder. */
const SHOWN_NOTE_KEYS = new Set([
  "stays",
  "blocked_reply_text",
  "booking_claims",
  "blocked_amounts_halalas",
  "message_type",
  "retention",
]);

/** Every other note as key and text, for the collapsed technical details. */
export function technicalNotes(notes: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(notes)
    .filter(([key]) => !SHOWN_NOTE_KEYS.has(key))
    .map(([key, value]) => [key, typeof value === "string" ? value : JSON.stringify(value)]);
}

/** A list of strings from a note, or none. */
export function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

/** A list of non-negative whole numbers from a note, or none. */
export function halalasList(value: unknown): number[] {
  return Array.isArray(value)
    ? value.filter((item): item is number => Number.isInteger(item) && item >= 0)
    : [];
}
