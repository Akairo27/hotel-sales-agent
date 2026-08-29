"use client";

import { useMemo, useState, type CSSProperties } from "react";
import type { Season } from "@/lib/types";
import {
  HIJRI_FIRST_YEAR,
  HIJRI_LAST_SELECTABLE_YEAR,
  currentHijriYearEstimate,
  hijriMonthGregorianSpan,
  hijriYearGregorianSpan,
  resolveYearCoverage,
  type DayCoverage,
} from "@/lib/seasonCalendar";
import { monthName } from "@/lib/monthNames";
import { seasonColor, GAP_COLOR } from "@/lib/seasonColor";
import { CARD, SECTION_TITLE, SELECT } from "@/lib/ui";

interface CalendarPreviewProps {
  seasons: Season[];
}

const GREGORIAN_DATE_FORMAT = new Intl.DateTimeFormat("ar-SA-u-ca-gregory", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});

function formatGregorian(date: Date): string {
  return GREGORIAN_DATE_FORMAT.format(date);
}

function dayLabel(day: DayCoverage, defaultSeasonName: string): string {
  const dateLabel = formatGregorian(day.date);
  if (day.matching.length === 0) {
    return `${dateLabel} — ${defaultSeasonName} (افتراضي)`;
  }
  const names = day.matching.map((season) => season.season_name);
  const winnerName = day.winner?.season_name ?? defaultSeasonName;
  return day.matching.length > 1
    ? `${dateLabel} — تداخل: ${names.join("، ")} — يفوز: ${winnerName}`
    : `${dateLabel} — ${winnerName}`;
}

function dayStyle(day: DayCoverage): CSSProperties {
  const baseColor = day.winner ? seasonColor(day.winner.id) : GAP_COLOR;
  if (day.matching.length > 1) {
    return {
      backgroundColor: baseColor,
      backgroundImage:
        "repeating-linear-gradient(45deg, rgba(255,255,255,0.6) 0, " +
        "rgba(255,255,255,0.6) 2px, transparent 2px, transparent 6px)",
    };
  }
  return { backgroundColor: baseColor };
}

// Slices a full Hijri year's day-by-day coverage into its 12 month blocks
// using the real per-month Gregorian boundaries from the reference table
// (hijriMonthGregorianSpan) — never assumed or counted out by hand, since
// a Hijri month is 29 or 30 days depending on the year.
function groupByHijriMonth(days: DayCoverage[], hijriYear: number): DayCoverage[][] {
  return Array.from({ length: 12 }, (_, index) => {
    const span = hijriMonthGregorianSpan(hijriYear, index + 1);
    return days.filter((day) => day.date >= span.start && day.date < span.end);
  });
}

export function CalendarPreview({ seasons }: CalendarPreviewProps) {
  const [hijriYear, setHijriYear] = useState(() => currentHijriYearEstimate(new Date()));

  const defaultSeason = seasons.find((season) => season.is_default) ?? null;
  const defaultSeasonName = defaultSeason?.season_name ?? "بلا موسم افتراضي";
  const nonDefaultSeasons = useMemo(
    () => seasons.filter((season) => !season.is_default),
    [seasons]
  );

  const coverage = useMemo(() => resolveYearCoverage(seasons, hijriYear), [seasons, hijriYear]);
  const months = useMemo(
    () => groupByHijriMonth(coverage, hijriYear),
    [coverage, hijriYear]
  );

  const yearOptions = useMemo(
    () =>
      Array.from(
        { length: HIJRI_LAST_SELECTABLE_YEAR - HIJRI_FIRST_YEAR + 1 },
        (_, index) => HIJRI_FIRST_YEAR + index
      ),
    []
  );

  return (
    <section className={CARD}>
      <h2 className={SECTION_TITLE}>معاينة التقويم</h2>
      <label className="mt-4 block text-sm font-medium text-foreground">
        السنة الهجرية
        <select
          value={hijriYear}
          onChange={(event) => setHijriYear(Number(event.target.value))}
          className={`${SELECT} mt-1 w-40`}
        >
          {yearOptions.map((year) => (
            <option key={year} value={year}>
              {year} هـ
            </option>
          ))}
        </select>
      </label>

      <ul className="mt-4 flex flex-wrap gap-x-4 gap-y-2">
        {nonDefaultSeasons.map((season) => (
          <li key={season.id} className="flex items-center gap-1.5 text-sm text-foreground">
            <span
              aria-hidden
              className="h-3 w-3 shrink-0 rounded-full"
              style={{ backgroundColor: seasonColor(season.id) }}
            />
            {season.season_name}
          </li>
        ))}
        <li className="flex items-center gap-1.5 text-sm text-foreground">
          <span aria-hidden className="h-3 w-3 shrink-0 rounded-full" style={{ backgroundColor: GAP_COLOR }} />
          {defaultSeasonName} (افتراضي — يغطي الفجوات)
        </li>
      </ul>

      <div className="mt-6 space-y-5">
        {months.map((monthDays, index) => {
          const monthNumber = index + 1;
          if (monthDays.length === 0) {
            return null;
          }
          const firstDay = monthDays[0];
          const lastDay = monthDays[monthDays.length - 1];
          return (
            <div key={monthNumber}>
              <strong className="text-sm font-medium text-foreground">
                {monthName("hijri", monthNumber)}
              </strong>{" "}
              <span className="text-sm text-muted-foreground">
                {firstDay ? formatGregorian(firstDay.date) : ""}
                {" – "}
                {lastDay ? formatGregorian(lastDay.date) : ""}
              </span>
              <div className="mt-2 overflow-x-auto">
                <div className="flex w-max gap-0.5">
                  {monthDays.map((day) => (
                    <div
                      key={day.date.toISOString()}
                      title={dayLabel(day, defaultSeasonName)}
                      className="h-3.5 w-3.5 rounded-sm"
                      style={dayStyle(day)}
                    />
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <p className="mt-6 text-sm text-muted-foreground">
        المدى المعروض: {formatGregorian(hijriYearGregorianSpan(hijriYear).start)} –{" "}
        {formatGregorian(hijriYearGregorianSpan(hijriYear).end)} (ميلادي)
      </p>
    </section>
  );
}
