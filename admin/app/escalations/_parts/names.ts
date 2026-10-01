import type { createClient } from "@/utils/supabase/server";

export type Names = Map<number, string>;

export interface StayNames {
  hotels: Names;
  roomTypes: Names;
}

/** Hotel and room type names for the ids an escalation page mentions,
 * read through the signed-in user's own session. */
export async function loadStayNames(
  supabase: Awaited<ReturnType<typeof createClient>>,
  hotelIds: number[],
  roomTypeIds: number[],
): Promise<StayNames> {
  const [{ data: hotels }, { data: roomTypes }] = await Promise.all([
    supabase.from("hotels").select("id, hotel_name").in("id", [...new Set(hotelIds)]),
    supabase.from("room_types").select("id, room_type_name").in("id", [...new Set(roomTypeIds)]),
  ]);
  return {
    hotels: new Map((hotels ?? []).map((hotel) => [hotel.id as number, hotel.hotel_name as string])),
    roomTypes: new Map(
      (roomTypes ?? []).map((roomType) => [roomType.id as number, roomType.room_type_name as string]),
    ),
  };
}
