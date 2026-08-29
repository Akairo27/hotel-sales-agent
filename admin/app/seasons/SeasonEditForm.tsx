"use client";

import { useState } from "react";
import type { CalendarType, Season } from "@/lib/types";
import { endOfMonthSentinel } from "@/lib/seasonCalendar";
import { monthName, MONTH_NUMBERS } from "@/lib/monthNames";
import {
  ACTION_BAR,
  BUTTON_PRIMARY,
  CHECKBOX,
  CHECKBOX_LABEL,
  FIELDSET,
  INPUT,
  LABEL,
  LEGEND,
  SELECT,
} from "@/lib/ui";
import { updateSeasonBounds } from "./actions";

interface SeasonEditFormProps {
  season: Season;
  onChange: (updated: Season) => void;
}

// One form per existing season, used both to edit it and — via controlled
// inputs reported to the parent on every keystroke — to drive the live
// calendar preview before the admin saves anything. Saving still goes
// through a real <form action> to updateSeasonBounds (migration 0015's
// admin-only RLS policy, rowcount-checked the same way PR B's hotel/room
// type renames are).
export function SeasonEditForm({ season, onChange }: SeasonEditFormProps) {
  // Whether the end_day currently on `season` already equals the
  // "through end of month" sentinel for its own end_month/calendar_type —
  // the toggle starts checked whenever the stored data already represents
  // that state, so re-opening an existing season doesn't silently show a
  // literal day number that was actually meant as "through the end".
  const [endsAtMonthEnd, setEndsAtMonthEnd] = useState(
    season.end_day >= endOfMonthSentinel(season.calendar_type, season.end_month)
  );
  const boundAction = updateSeasonBounds.bind(null, season.id);

  function update(partial: Partial<Season>): void {
    const next: Season = { ...season, ...partial };
    if (endsAtMonthEnd) {
      next.end_day = endOfMonthSentinel(next.calendar_type, next.end_month);
    }
    onChange(next);
  }

  function toggleEndsAtMonthEnd(checked: boolean): void {
    setEndsAtMonthEnd(checked);
    if (checked) {
      update({ end_day: endOfMonthSentinel(season.calendar_type, season.end_month) });
    }
  }

  return (
    <form action={boundAction} className="space-y-4 border-t border-border pt-4">
      <label className={LABEL}>
        اسم الموسم
        <input
          name="season_name"
          value={season.season_name}
          onChange={(event) => update({ season_name: event.target.value })}
          required
          className={`${INPUT} mt-1 w-full`}
        />
      </label>

      <label className={LABEL}>
        التقويم
        <select
          name="calendar_type"
          value={season.calendar_type}
          onChange={(event) => update({ calendar_type: event.target.value as CalendarType })}
          className={`${SELECT} mt-1 w-full sm:w-64`}
        >
          <option value="hijri">هجري</option>
          <option value="gregorian">ميلادي</option>
        </select>
      </label>

      <fieldset className={FIELDSET}>
        <legend className={LEGEND}>البداية</legend>
        <div className="flex flex-wrap gap-3">
          <select
            name="start_month"
            value={season.start_month}
            onChange={(event) => update({ start_month: Number(event.target.value) })}
            className={`${SELECT} w-40`}
          >
            {MONTH_NUMBERS.map((monthNumber) => (
              <option key={monthNumber} value={monthNumber}>
                {monthName(season.calendar_type, monthNumber)}
              </option>
            ))}
          </select>
          <input
            type="number"
            name="start_day"
            min={1}
            max={31}
            value={season.start_day}
            onChange={(event) => update({ start_day: Number(event.target.value) })}
            required
            className={`${INPUT} w-20`}
          />
        </div>
      </fieldset>

      <fieldset className={FIELDSET}>
        <legend className={LEGEND}>النهاية</legend>
        <div className="flex flex-wrap items-center gap-3">
          <select
            name="end_month"
            value={season.end_month}
            onChange={(event) => update({ end_month: Number(event.target.value) })}
            className={`${SELECT} w-40`}
          >
            {MONTH_NUMBERS.map((monthNumber) => (
              <option key={monthNumber} value={monthNumber}>
                {monthName(season.calendar_type, monthNumber)}
              </option>
            ))}
          </select>
          {endsAtMonthEnd ? (
            <input type="hidden" name="end_day" value={season.end_day} />
          ) : (
            <input
              type="number"
              name="end_day"
              min={1}
              max={31}
              value={season.end_day}
              onChange={(event) => update({ end_day: Number(event.target.value) })}
              required
              className={`${INPUT} w-20`}
            />
          )}
          <label className={CHECKBOX_LABEL}>
            <input
              type="checkbox"
              checked={endsAtMonthEnd}
              onChange={(event) => toggleEndsAtMonthEnd(event.target.checked)}
              className={CHECKBOX}
            />
            حتى نهاية الشهر
          </label>
        </div>
      </fieldset>

      <div className={ACTION_BAR}>
        <button type="submit" className={BUTTON_PRIMARY}>
          حفظ
        </button>
      </div>
    </form>
  );
}
