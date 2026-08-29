import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PriceOverrideForm } from "./PriceOverrideForm";
import type { HotelRef, RoomTypeRef } from "@/lib/types";

const hotels: HotelRef[] = [
  { id: 1, hotel_name: "فندق الاختبار", created_at: "2026-08-01T00:00:00Z" },
  { id: 2, hotel_name: "فندق آخر", created_at: "2026-08-01T00:00:00Z" },
];

const roomTypes: RoomTypeRef[] = [
  { id: 10, hotel_id: 1, room_type_name: "غرفة قياسية", created_at: "2026-08-01T00:00:00Z" },
  { id: 11, hotel_id: 2, room_type_name: "جناح ملكي", created_at: "2026-08-01T00:00:00Z" },
];

function fieldsetHtml(): string {
  return renderToStaticMarkup(
    <PriceOverrideForm hotels={hotels} roomTypes={roomTypes} onSaved={() => {}} />
  );
}

describe("PriceOverrideForm", () => {
  it("renders without throwing", () => {
    expect(() => fieldsetHtml()).not.toThrow();
  });

  it("lists every hotel as a room-type-selector-independent option", () => {
    const html = fieldsetHtml();
    expect(html).toContain("فندق الاختبار");
    expect(html).toContain("فندق آخر");
  });

  it("disables the room-type select before a hotel is chosen", () => {
    const html = fieldsetHtml();
    // Anchored on the label-select junction itself, not just the label
    // text "نوع الغرفة" — the form's own "الفندق ونوع الغرفة" section
    // heading contains that same substring earlier in the markup.
    const selectStart = html.indexOf("نوع الغرفة<select");
    const selectEnd = html.indexOf("</select>", selectStart);
    expect(html.slice(selectStart, selectEnd)).toContain("disabled=\"\"");
  });

  it("shows no night count before both dates are filled in", () => {
    const html = fieldsetHtml();
    expect(html).not.toContain("ليلة");
  });
});
