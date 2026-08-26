"use client";

import { useState } from "react";
import type { CalendarType, Season } from "@/lib/types";
import { endOfMonthSentinel } from "@/lib/seasonCalendar";
import { monthName, MONTH_NUMBERS } from "@/lib/monthNames";
import { seasonColor } from "@/lib/seasonColor";
import {
  ACTION_BAR,
  ALERT_ERROR,
  BUTTON_PRIMARY,
  CARD,
  CHECKBOX,
  CHECKBOX_LABEL,
  FIELDSET,
  HINT,
  INPUT,
  LABEL,
  LEGEND,
  SECTION_TITLE,
  SELECT,
} from "@/lib/ui";
import { SeasonEditForm } from "./SeasonEditForm";
import { createDefaultSeason, createSeason, renameDefaultSeason } from "./actions";

interface SeasonListProps {
  seasons: Season[];
  isAdmin: boolean;
  onSeasonChange: (updated: Season) => void;
  onReorder: (orderedSeasonIds: number[]) => void;
  reorderError: string | null;
}

export function SeasonList({
  seasons,
  isAdmin,
  onSeasonChange,
  onReorder,
  reorderError,
}: SeasonListProps) {
  const defaultSeason = seasons.find((season) => season.is_default) ?? null;
  // Highest priority first — matches resolve_season_id's own tie-break
  // (higher priority wins), so the list's visual top-to-bottom order is
  // exactly "who wins first" without the admin needing to read numbers.
  const orderedSeasons = seasons
    .filter((season) => !season.is_default)
    .sort((a, b) => b.priority - a.priority || a.id - b.id);

  const [draggedId, setDraggedId] = useState<number | null>(null);

  function handleDrop(targetId: number): void {
    if (draggedId === null || draggedId === targetId) {
      setDraggedId(null);
      return;
    }
    const ids = orderedSeasons.map((season) => season.id);
    const fromIndex = ids.indexOf(draggedId);
    const toIndex = ids.indexOf(targetId);
    if (fromIndex === -1 || toIndex === -1) {
      setDraggedId(null);
      return;
    }
    ids.splice(fromIndex, 1);
    ids.splice(toIndex, 0, draggedId);
    setDraggedId(null);
    onReorder(ids);
  }

  return (
    <section>
      <h2 className={SECTION_TITLE}>المواسم</h2>
      {isAdmin && (
        <p className={`${HINT} mt-1`}>
          الأعلى في القائمة يفوز عند تداخل المواسم — اسحب لإعادة الترتيب.
        </p>
      )}
      {reorderError && (
        <p role="alert" className={`${ALERT_ERROR} mt-3`}>
          {reorderError}
        </p>
      )}

      <ul className="mt-4 space-y-2">
        {orderedSeasons.map((season) => (
          <li
            key={season.id}
            draggable={isAdmin}
            onDragStart={isAdmin ? () => setDraggedId(season.id) : undefined}
            onDragOver={isAdmin ? (event) => event.preventDefault() : undefined}
            onDrop={isAdmin ? () => handleDrop(season.id) : undefined}
            className={`${CARD} p-4 ${isAdmin ? "cursor-move" : ""}`}
          >
            <div className="flex items-center gap-2.5">
              <span
                aria-hidden
                className="h-3.5 w-3.5 shrink-0 rounded-full"
                style={{ backgroundColor: seasonColor(season.id) }}
              />
              {isAdmin ? (
                <details className="flex-1">
                  <summary className="cursor-pointer text-sm font-medium text-foreground">
                    {season.season_name}
                  </summary>
                  <div className="mt-4">
                    <SeasonEditForm season={season} onChange={onSeasonChange} />
                  </div>
                </details>
              ) : (
                <span className="text-sm font-medium text-foreground">{season.season_name}</span>
              )}
            </div>
          </li>
        ))}
      </ul>

      <div className="mt-6">
        <DefaultSeasonBlock defaultSeason={defaultSeason} isAdmin={isAdmin} />
      </div>

      {isAdmin && (
        <div className="mt-6">
          <AddSeasonForm />
        </div>
      )}
    </section>
  );
}

function DefaultSeasonBlock({
  defaultSeason,
  isAdmin,
}: {
  defaultSeason: Season | null;
  isAdmin: boolean;
}) {
  if (defaultSeason === null && !isAdmin) {
    return <p className={HINT}>لا يوجد موسم افتراضي بعد.</p>;
  }
  if (defaultSeason === null) {
    return (
      <form action={createDefaultSeason} className={CARD}>
        <p className="text-sm text-foreground">
          لا يوجد موسم افتراضي بعد — أي تاريخ لا يقع ضمن موسم محدد يحتاج مرجعاً. أنشئ الموسم
          الافتراضي المطلوب (ARCHITECTURE.md §4).
        </p>
        <div className={ACTION_BAR}>
          <button type="submit" className={BUTTON_PRIMARY}>
            إنشاء الموسم الافتراضي
          </button>
        </div>
      </form>
    );
  }

  if (!isAdmin) {
    return (
      <p className="text-sm text-foreground">
        الموسم الافتراضي (يغطي كل الفجوات): <strong>{defaultSeason.season_name}</strong>
      </p>
    );
  }

  return (
    <form action={renameDefaultSeason} className={CARD}>
      <input type="hidden" name="season_id" value={defaultSeason.id} />
      <label className={LABEL}>
        اسم الموسم الافتراضي (يغطي كل الفجوات، حدوده غير قابلة للتعديل)
        <input
          name="season_name"
          defaultValue={defaultSeason.season_name}
          required
          className={`${INPUT} mt-1 w-full`}
        />
      </label>
      <div className={ACTION_BAR}>
        <button type="submit" className={BUTTON_PRIMARY}>
          حفظ
        </button>
      </div>
    </form>
  );
}

function AddSeasonForm() {
  const [calendarType, setCalendarType] = useState<CalendarType>("hijri");
  const [endMonth, setEndMonth] = useState(1);
  const [endDay, setEndDay] = useState(1);
  const [endsAtMonthEnd, setEndsAtMonthEnd] = useState(false);

  function toggleEndsAtMonthEnd(checked: boolean): void {
    setEndsAtMonthEnd(checked);
    if (checked) {
      setEndDay(endOfMonthSentinel(calendarType, endMonth));
    }
  }

  function handleCalendarTypeChange(next: CalendarType): void {
    setCalendarType(next);
    if (endsAtMonthEnd) {
      setEndDay(endOfMonthSentinel(next, endMonth));
    }
  }

  function handleEndMonthChange(next: number): void {
    setEndMonth(next);
    if (endsAtMonthEnd) {
      setEndDay(endOfMonthSentinel(calendarType, next));
    }
  }

  return (
    <form action={createSeason} className={CARD}>
      <h3 className={SECTION_TITLE}>إضافة موسم</h3>

      <label className={`${LABEL} mt-4`}>
        اسم الموسم
        <input name="season_name" required className={`${INPUT} mt-1 w-full`} />
      </label>

      <label className={`${LABEL} mt-4`}>
        التقويم
        <select
          name="calendar_type"
          value={calendarType}
          onChange={(event) => handleCalendarTypeChange(event.target.value as CalendarType)}
          className={`${SELECT} mt-1 w-full sm:w-64`}
        >
          <option value="hijri">هجري</option>
          <option value="gregorian">ميلادي</option>
        </select>
      </label>

      <fieldset className={`${FIELDSET} mt-4`}>
        <legend className={LEGEND}>البداية</legend>
        <div className="flex flex-wrap gap-3">
          <select name="start_month" defaultValue={1} className={`${SELECT} w-40`}>
            {MONTH_NUMBERS.map((monthNumber) => (
              <option key={monthNumber} value={monthNumber}>
                {monthName(calendarType, monthNumber)}
              </option>
            ))}
          </select>
          <input
            type="number"
            name="start_day"
            min={1}
            max={31}
            defaultValue={1}
            required
            className={`${INPUT} w-20`}
          />
        </div>
      </fieldset>

      <fieldset className={`${FIELDSET} mt-4`}>
        <legend className={LEGEND}>النهاية</legend>
        <div className="flex flex-wrap items-center gap-3">
          <select
            name="end_month"
            value={endMonth}
            onChange={(event) => handleEndMonthChange(Number(event.target.value))}
            className={`${SELECT} w-40`}
          >
            {MONTH_NUMBERS.map((monthNumber) => (
              <option key={monthNumber} value={monthNumber}>
                {monthName(calendarType, monthNumber)}
              </option>
            ))}
          </select>
          {endsAtMonthEnd ? (
            <input type="hidden" name="end_day" value={endDay} />
          ) : (
            <input
              type="number"
              name="end_day"
              min={1}
              max={31}
              value={endDay}
              onChange={(event) => setEndDay(Number(event.target.value))}
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
          إضافة
        </button>
      </div>
    </form>
  );
}
