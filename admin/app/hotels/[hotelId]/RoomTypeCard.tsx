import type { RoomType } from "@/lib/types";
import {
  BED_CONFIGURATIONS,
  BED_CONFIGURATION_LABELS,
  MAX_CAPACITY_ADULTS,
  MIN_CAPACITY_ADULTS,
} from "@/lib/hotelDetails";
import {
  ACTION_BAR,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  CARD,
  INPUT,
  LABEL,
  SELECT,
} from "@/lib/ui";
import { renameRoomType, updateRoomTypeDetails } from "./actions";

const NOT_RECORDED = "—";

function summarize(roomType: RoomType): string {
  const parts: string[] = [];
  if (roomType.capacity_adults !== null) {
    parts.push(`${roomType.capacity_adults} أشخاص`);
  }
  if (roomType.bed_configuration !== null) {
    parts.push(BED_CONFIGURATION_LABELS[roomType.bed_configuration]);
  }
  if (roomType.size_sqm !== null) {
    parts.push(`${roomType.size_sqm} م²`);
  }
  return parts.length === 0 ? NOT_RECORDED : parts.join(" · ");
}

export function RoomTypeCard({
  hotelId,
  roomType,
  canEdit,
}: {
  hotelId: number;
  roomType: RoomType;
  canEdit: boolean;
}) {
  const saveDetails = updateRoomTypeDetails.bind(null, hotelId, roomType.id);
  const rename = renameRoomType.bind(null, hotelId, roomType.id);

  return (
    <div className={CARD}>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <strong className="text-base font-medium text-foreground">
            {roomType.room_type_name}
          </strong>
          <span className="mt-1 block text-sm text-muted-foreground">
            {summarize(roomType)}
          </span>
        </div>
        {canEdit && (
          <form action={rename} className="flex items-end gap-2">
            <div className="w-44">
              <label
                htmlFor={`room-type-name-${roomType.id}`}
                className="mb-1 block text-xs text-muted-foreground"
              >
                الاسم الجديد
              </label>
              <input
                id={`room-type-name-${roomType.id}`}
                name="room_type_name"
                defaultValue={roomType.room_type_name}
                required
                className={`${INPUT} w-full`}
              />
            </div>
            <button type="submit" className={BUTTON_SECONDARY}>
              حفظ الاسم
            </button>
          </form>
        )}
      </div>

      {canEdit && (
        <form action={saveDetails} className="mt-6 border-t border-border pt-4">
          <div className="flex flex-wrap gap-4">
            <div className="min-w-36 flex-1">
              <label htmlFor={`capacity-${roomType.id}`} className={LABEL}>
                عدد الأشخاص
              </label>
              <input
                id={`capacity-${roomType.id}`}
                name="capacity_adults"
                type="number"
                min={MIN_CAPACITY_ADULTS}
                max={MAX_CAPACITY_ADULTS}
                step={1}
                defaultValue={roomType.capacity_adults ?? ""}
                className={`${INPUT} mt-1 w-full`}
              />
            </div>

            <div className="min-w-36 flex-1">
              <label htmlFor={`beds-${roomType.id}`} className={LABEL}>
                توزيع الأسرّة
              </label>
              <select
                id={`beds-${roomType.id}`}
                name="bed_configuration"
                defaultValue={roomType.bed_configuration ?? ""}
                className={`${SELECT} mt-1 w-full`}
              >
                <option value="">غير محدد</option>
                {BED_CONFIGURATIONS.map((configuration) => (
                  <option key={configuration} value={configuration}>
                    {BED_CONFIGURATION_LABELS[configuration]}
                  </option>
                ))}
              </select>
            </div>

            <div className="min-w-36 flex-1">
              <label htmlFor={`size-${roomType.id}`} className={LABEL}>
                المساحة (م²)
              </label>
              <input
                id={`size-${roomType.id}`}
                name="size_sqm"
                type="number"
                min={1}
                step={1}
                defaultValue={roomType.size_sqm ?? ""}
                className={`${INPUT} mt-1 w-full`}
              />
            </div>
          </div>

          <div className={ACTION_BAR}>
            <button type="submit" className={BUTTON_PRIMARY}>
              حفظ التفاصيل
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
