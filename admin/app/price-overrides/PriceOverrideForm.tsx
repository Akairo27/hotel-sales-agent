"use client";

import { useState } from "react";
import type { HotelRef, RoomTypeRef } from "@/lib/types";
import {
  MAX_OVERRIDE_RANGE_NIGHTS,
  nightCount,
  validateOverrideRange,
} from "@/lib/priceOverrideRange";
import {
  ACTION_BAR,
  ALERT_ERROR,
  ALERT_STATUS,
  BUTTON_PRIMARY,
  CARD,
  FIELDSET,
  HINT,
  INPUT,
  LABEL,
  LEGEND,
  SELECT,
} from "@/lib/ui";
import { upsertPriceOverrides } from "./actions";

interface PriceOverrideFormProps {
  hotels: HotelRef[];
  roomTypes: RoomTypeRef[];
  onSaved: () => void;
}

// One form for both creating a new override and updating an existing one:
// admin_upsert_price_overrides (migration 0021) always resolves by
// (hotel, room type, night) — resubmitting a range that overlaps existing
// nights overwrites them, resubmitting an empty range creates them. There
// is no separate "edit" mode; picking the same hotel/room type/dates again
// with new values is the edit.
export function PriceOverrideForm({ hotels, roomTypes, onSaved }: PriceOverrideFormProps) {
  const [hotelId, setHotelId] = useState<number | "">("");
  const [roomTypeId, setRoomTypeId] = useState<number | "">("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [askPriceOverride, setAskPriceOverride] = useState(0);
  const [minAllowedOverride, setMinAllowedOverride] = useState(0);
  const [expiresAt, setExpiresAt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const availableRoomTypes = roomTypes.filter((roomType) => roomType.hotel_id === hotelId);

  // The primary guard against a fat-fingered date range: a live,
  // per-keystroke count computed entirely in the browser, no query.
  const nights = startDate && endDate ? nightCount(startDate, endDate) : null;
  const rangeValidation =
    startDate && endDate
      ? validateOverrideRange(startDate, endDate, askPriceOverride, minAllowedOverride)
      : null;

  async function handleSave(): Promise<void> {
    setError(null);
    setWarning(null);
    if (hotelId === "" || roomTypeId === "") {
      setError("اختر الفندق ونوع الغرفة.");
      return;
    }
    if (!expiresAt) {
      setError("يجب تحديد تاريخ انتهاء التجاوز.");
      return;
    }
    if (rangeValidation === null) {
      setError("يجب تحديد تاريخ البداية والنهاية.");
      return;
    }
    if (!rangeValidation.valid) {
      setError(rangeValidation.message);
      return;
    }
    setSaving(true);
    const result = await upsertPriceOverrides(
      hotelId,
      roomTypeId,
      startDate,
      endDate,
      askPriceOverride,
      minAllowedOverride,
      new Date(expiresAt).toISOString()
    );
    setSaving(false);
    if (result.error) {
      setError(result.error);
      return;
    }
    if (result.warning) {
      setWarning(result.warning);
    }
    onSaved();
  }

  return (
    <fieldset className={`${CARD} min-w-0`}>
      <legend className="px-1 text-base font-medium text-foreground">
        إضافة / تحديث تجاوز لمدى تواريخ
      </legend>

      <div className="mt-4 space-y-5">
        {error && (
          <p role="alert" className={ALERT_ERROR}>
            {error}
          </p>
        )}
        {warning && (
          <p role="status" className={ALERT_STATUS}>
            {warning}
          </p>
        )}

        <div className={FIELDSET}>
          <p className={LEGEND}>الفندق ونوع الغرفة</p>
          <div className="flex flex-wrap gap-4">
            <label className={`${LABEL} min-w-48 flex-1`}>
              الفندق
              <select
                value={hotelId}
                onChange={(event) => {
                  setHotelId(event.target.value ? Number(event.target.value) : "");
                  setRoomTypeId("");
                }}
                className={`${SELECT} mt-1 w-full`}
              >
                <option value="">اختر فندقاً…</option>
                {hotels.map((hotel) => (
                  <option key={hotel.id} value={hotel.id}>
                    {hotel.hotel_name}
                  </option>
                ))}
              </select>
            </label>

            <label className={`${LABEL} min-w-48 flex-1`}>
              نوع الغرفة
              <select
                value={roomTypeId}
                onChange={(event) =>
                  setRoomTypeId(event.target.value ? Number(event.target.value) : "")
                }
                disabled={hotelId === ""}
                className={`${SELECT} mt-1 w-full`}
              >
                <option value="">اختر نوع غرفة…</option>
                {availableRoomTypes.map((roomType) => (
                  <option key={roomType.id} value={roomType.id}>
                    {roomType.room_type_name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>

        <div className={FIELDSET}>
          <p className={LEGEND}>نطاق التواريخ</p>
          <div className="flex flex-wrap items-end gap-4">
            <label className={LABEL}>
              من تاريخ
              <input
                type="date"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
                className={`${INPUT} mt-1 w-44`}
              />
            </label>
            <label className={LABEL}>
              إلى تاريخ
              <input
                type="date"
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
                className={`${INPUT} mt-1 w-44`}
              />
            </label>
            {nights !== null && (
              <p className={HINT}>
                {nights} ليلة
                {nights > MAX_OVERRIDE_RANGE_NIGHTS &&
                  ` — يتجاوز الحد الأقصى (${MAX_OVERRIDE_RANGE_NIGHTS} ليلة)`}
              </p>
            )}
          </div>
        </div>

        <div className={FIELDSET}>
          <p className={LEGEND}>الأسعار (هللة)</p>
          <div className="flex flex-wrap gap-4">
            <label className={LABEL}>
              سعر العرض
              <input
                type="number"
                min={0}
                step={1}
                value={askPriceOverride}
                onChange={(event) => setAskPriceOverride(Number(event.target.value))}
                className={`${INPUT} mt-1 w-32`}
              />
            </label>
            <label className={LABEL}>
              الحد الأدنى المسموح
              <input
                type="number"
                min={0}
                step={1}
                value={minAllowedOverride}
                onChange={(event) => setMinAllowedOverride(Number(event.target.value))}
                className={`${INPUT} mt-1 w-32`}
              />
            </label>
          </div>
        </div>

        <div className={FIELDSET}>
          <p className={LEGEND}>انتهاء التجاوز</p>
          <label className={LABEL}>
            ينتهي في
            <input
              type="datetime-local"
              value={expiresAt}
              onChange={(event) => setExpiresAt(event.target.value)}
              className={`${INPUT} mt-1 w-56`}
            />
          </label>
        </div>

        <div className={ACTION_BAR}>
          <button type="button" onClick={handleSave} disabled={saving} className={BUTTON_PRIMARY}>
            {saving ? "جارٍ الحفظ…" : "حفظ"}
          </button>
        </div>
      </div>
    </fieldset>
  );
}
