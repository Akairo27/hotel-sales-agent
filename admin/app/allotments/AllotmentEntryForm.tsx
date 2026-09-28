"use client";

import { useState } from "react";
import type { HotelRef, RoomTypeRef } from "@/lib/types";
import {
  MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS,
  nightCount,
  validateAllotmentEntryRange,
} from "@/lib/allotmentEntryRange";
import { hijriDateLabel } from "@/lib/hijriLookup";
import {
  ACTION_BAR,
  ALERT_ERROR,
  ALERT_STATUS,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CARD,
  FIELDSET,
  HINT,
  INPUT,
  LABEL,
  LEGEND,
  SELECT,
  TABLE,
  TABLE_WRAPPER,
  TD,
  TH,
} from "@/lib/ui";
import { previewAllotmentEntry, submitAllotmentEntry } from "./actions";
import type { AllotmentEntryPreviewNight } from "./actions";

interface AllotmentEntryFormProps {
  hotels: HotelRef[];
  roomTypes: RoomTypeRef[];
}

const ACTION_LABELS: Record<AllotmentEntryPreviewNight["action"], string> = {
  created: "جديد",
  updated: "تحديث",
  unchanged: "بلا تغيير",
};

// The screen's own write path for creating or extending allotments over a
// date range — admin_set_allotments (migration 0029) is the only function
// that can ever INSERT into allotments or room_night_inventory. Always a
// preview (dry_run) before a confirm (the real write): the RPC runs every
// real CHECK constraint during the preview too, so this can't show a plan
// the database would go on to reject.
export function AllotmentEntryForm({ hotels, roomTypes }: AllotmentEntryFormProps) {
  const [hotelId, setHotelId] = useState<number | "">("");
  const [roomTypeId, setRoomTypeId] = useState<number | "">("");
  const [firstNight, setFirstNight] = useState("");
  const [lastNight, setLastNight] = useState("");
  const [totalRooms, setTotalRooms] = useState(0);
  const [costPerNightRiyals, setCostPerNightRiyals] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [previewNights, setPreviewNights] = useState<AllotmentEntryPreviewNight[] | null>(null);
  const [loading, setLoading] = useState(false);

  const availableRoomTypes = roomTypes.filter((roomType) => roomType.hotel_id === hotelId);
  const nights = firstNight && lastNight ? nightCount(firstNight, lastNight) : null;

  // Any change to the inputs invalidates whatever preview is showing — a
  // stale preview must never be confirmable against different values.
  function resetPreview(): void {
    setPreviewNights(null);
    setConfirmed(false);
  }

  async function handlePreview(): Promise<void> {
    setError(null);
    setConfirmed(false);
    if (hotelId === "" || roomTypeId === "") {
      setError("اختر الفندق ونوع الغرفة.");
      return;
    }
    const validation = validateAllotmentEntryRange(
      firstNight,
      lastNight,
      totalRooms,
      costPerNightRiyals
    );
    if (!validation.valid) {
      setError(validation.message);
      return;
    }
    setLoading(true);
    const result = await previewAllotmentEntry(
      hotelId,
      roomTypeId,
      firstNight,
      lastNight,
      totalRooms,
      costPerNightRiyals
    );
    setLoading(false);
    if (result.error) {
      setError(result.error);
      setPreviewNights(null);
      return;
    }
    setPreviewNights(result.nights ?? []);
  }

  async function handleConfirm(): Promise<void> {
    if (hotelId === "" || roomTypeId === "") {
      return;
    }
    setError(null);
    setLoading(true);
    const result = await submitAllotmentEntry(
      hotelId,
      roomTypeId,
      firstNight,
      lastNight,
      totalRooms,
      costPerNightRiyals
    );
    setLoading(false);
    if (result.error) {
      setError(result.error);
      return;
    }
    setPreviewNights(null);
    setConfirmed(true);
  }

  return (
    <fieldset className={`${CARD} min-w-0`}>
      <legend className="px-1 text-base font-medium text-foreground">
        إدخال غرف وتكلفة لمدى تواريخ
      </legend>

      <div className="mt-4 space-y-5">
        {error && (
          <p role="alert" className={ALERT_ERROR}>
            {error}
          </p>
        )}
        {confirmed && (
          <p role="status" className={ALERT_STATUS}>
            تم الحفظ.
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
                  resetPreview();
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
                onChange={(event) => {
                  setRoomTypeId(event.target.value ? Number(event.target.value) : "");
                  resetPreview();
                }}
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
              أول ليلة
              <input
                type="date"
                value={firstNight}
                onChange={(event) => {
                  setFirstNight(event.target.value);
                  resetPreview();
                }}
                className={`${INPUT} mt-1 w-44`}
              />
            </label>
            <label className={LABEL}>
              آخر ليلة
              <input
                type="date"
                value={lastNight}
                onChange={(event) => {
                  setLastNight(event.target.value);
                  resetPreview();
                }}
                className={`${INPUT} mt-1 w-44`}
              />
            </label>
            {nights !== null && (
              <p className={HINT}>
                {nights} ليلة
                {nights > MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS &&
                  ` — يتجاوز الحد الأقصى (${MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS} ليلة)`}
              </p>
            )}
          </div>
        </div>

        <div className={FIELDSET}>
          <p className={LEGEND}>الغرف والتكلفة</p>
          <div className="flex flex-wrap gap-4">
            <label className={LABEL}>
              عدد الغرف
              <input
                type="number"
                min={0}
                step={1}
                value={totalRooms}
                onChange={(event) => {
                  setTotalRooms(Number(event.target.value));
                  resetPreview();
                }}
                className={`${INPUT} mt-1 w-32`}
              />
            </label>
            <label className={LABEL}>
              التكلفة لليلة (ريال)
              <input
                type="number"
                min={0}
                step={1}
                value={costPerNightRiyals}
                onChange={(event) => {
                  setCostPerNightRiyals(Number(event.target.value));
                  resetPreview();
                }}
                className={`${INPUT} mt-1 w-32`}
              />
            </label>
          </div>
        </div>

        {previewNights && (
          <div className={FIELDSET}>
            <p className={LEGEND}>معاينة</p>
            {previewNights.length === 0 ? (
              <p className={HINT}>لا توجد ليالٍ في هذا المدى.</p>
            ) : (
              <div className={TABLE_WRAPPER}>
                <table className={TABLE}>
                  <thead>
                    <tr>
                      <th className={TH}>الليلة</th>
                      <th className={TH}>التاريخ الهجري</th>
                      <th className={TH}>الإجراء</th>
                      <th className={TH}>عدد الغرف</th>
                      <th className={TH}>التكلفة لليلة (ريال)</th>
                      <th className={TH}>محجوز</th>
                      <th className={TH}>حجز مؤقت</th>
                    </tr>
                  </thead>
                  <tbody>
                    {previewNights.map((night) => (
                      <tr key={night.stayDate}>
                        <td className={TD}>{night.stayDate}</td>
                        <td className={TD}>
                          {hijriDateLabel(new Date(`${night.stayDate}T00:00:00Z`))}
                        </td>
                        <td className={TD}>{ACTION_LABELS[night.action]}</td>
                        <td className={TD}>{night.totalRooms}</td>
                        <td className={TD}>{night.costPerNightRiyals}</td>
                        <td className={TD}>{night.reserved}</td>
                        <td className={TD}>{night.held}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        <div className={ACTION_BAR}>
          {previewNights ? (
            <button
              type="button"
              onClick={handleConfirm}
              disabled={loading}
              className={BUTTON_PRIMARY}
            >
              {loading ? "جارٍ الحفظ…" : "تأكيد الحفظ"}
            </button>
          ) : (
            <button
              type="button"
              onClick={handlePreview}
              disabled={loading}
              className={BUTTON_SECONDARY}
            >
              {loading ? "جارٍ المعاينة…" : "معاينة"}
            </button>
          )}
        </div>
      </div>
    </fieldset>
  );
}
