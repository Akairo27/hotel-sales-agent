"use client";

import { useState } from "react";
import type {
  HotelRef,
  PriceRuleForDashboard,
  PriceRuleScope,
  RoomTypeRef,
  Season,
} from "@/lib/types";
import { CARD, FIELDSET, HINT, SECTION_TITLE, SELECT } from "@/lib/ui";
import { PriceRuleEditForm } from "./PriceRuleEditForm";

interface PriceRulesWorkspaceProps {
  initialRules: PriceRuleForDashboard[];
  hotels: HotelRef[];
  roomTypes: RoomTypeRef[];
  seasons: Season[];
  canEdit: boolean;
}

interface ScopeTarget {
  scope: Exclude<PriceRuleScope, "global">;
  scopeId: number;
  label: string;
}

export function PriceRulesWorkspace({
  initialRules,
  hotels,
  roomTypes,
  seasons,
  canEdit,
}: PriceRulesWorkspaceProps) {
  const [rules, setRules] = useState(initialRules);
  const [addingTarget, setAddingTarget] = useState<ScopeTarget | null>(null);

  // Resets local state when the server delivers a genuinely new
  // initialRules snapshot after revalidatePath("/price-rules") — same
  // adjust-state-during-render idiom as admin/app/seasons/SeasonsWorkspace.tsx,
  // for the same reason: it avoids an extra commit-then-effect render pass.
  const [prevInitialRules, setPrevInitialRules] = useState(initialRules);
  if (initialRules !== prevInitialRules) {
    setPrevInitialRules(initialRules);
    setRules(initialRules);
  }

  function refresh(): void {
    // The Server Action already calls revalidatePath("/price-rules") on
    // success — this local reset just closes the "add new rule" panel so
    // the next server render (with the new row) starts from a clean
    // workspace instead of re-opening the same form.
    setAddingTarget(null);
  }

  const globalRule = rules.find((rule) => rule.scope === "global") ?? null;
  const seasonRules = rules.filter((rule) => rule.scope === "season");
  const hotelRules = rules.filter((rule) => rule.scope === "hotel");
  const roomTypeRules = rules.filter((rule) => rule.scope === "room_type");

  const hotelNames = new Map(hotels.map((h) => [h.id, h.hotel_name]));
  const roomTypeNames = new Map(roomTypes.map((rt) => [rt.id, rt.room_type_name]));
  const seasonNames = new Map(seasons.map((s) => [s.id, s.season_name]));

  const availableTargets: ScopeTarget[] = [
    ...seasons
      .filter((season) => !seasonRules.some((rule) => rule.scope_id === season.id))
      .map((season) => ({
        scope: "season" as const,
        scopeId: season.id,
        label: `موسم: ${season.season_name}`,
      })),
    ...hotels
      .filter((hotel) => !hotelRules.some((rule) => rule.scope_id === hotel.id))
      .map((hotel) => ({
        scope: "hotel" as const,
        scopeId: hotel.id,
        label: `فندق: ${hotel.hotel_name}`,
      })),
    ...roomTypes
      .filter((roomType) => !roomTypeRules.some((rule) => rule.scope_id === roomType.id))
      .map((roomType) => ({
        scope: "room_type" as const,
        scopeId: roomType.id,
        label: `نوع غرفة: ${hotelNames.get(roomType.hotel_id) ?? ""} — ${
          roomType.room_type_name
        }`,
      })),
  ];

  return (
    <div className="space-y-8">
      <section className={CARD}>
        <h2 className={SECTION_TITLE}>القاعدة العامة</h2>
        <p className={`${HINT} mt-1`}>
          أساس سلسلة التوريث — لا تُحذف ولا تُعطَّل، ويجب أن تحدد الثلاثة كاملة.
        </p>
        <div className="mt-4">
          {canEdit ? (
            <PriceRuleEditForm rule={globalRule} scope="global" scopeId={null} onSaved={refresh} />
          ) : (
            <p className={HINT}>تحتاج صلاحية عرض التكلفة لتعديل هذه القاعدة.</p>
          )}
        </div>
      </section>

      <section className={CARD}>
        <h2 className={SECTION_TITLE}>قواعد المواسم</h2>
        {seasonRules.length === 0 && <p className={`${HINT} mt-2`}>لا توجد قواعد خاصة بموسم بعينه.</p>}
        <div className="mt-4 divide-y divide-border">
          {seasonRules.map((rule) => (
            <details key={rule.id} className="py-4 first:pt-0 last:pb-0">
              <summary className="cursor-pointer text-sm font-medium text-foreground">
                {seasonNames.get(rule.scope_id ?? -1) ?? `موسم #${rule.scope_id}`}
                {!rule.is_active && <span className="text-muted-foreground"> (معطَّلة)</span>}
              </summary>
              {canEdit && (
                <div className="mt-4">
                  <PriceRuleEditForm
                    rule={rule}
                    scope="season"
                    scopeId={rule.scope_id}
                    onSaved={refresh}
                  />
                </div>
              )}
            </details>
          ))}
        </div>
      </section>

      <section className={CARD}>
        <h2 className={SECTION_TITLE}>قواعد الفنادق</h2>
        {hotelRules.length === 0 && <p className={`${HINT} mt-2`}>لا توجد قواعد خاصة بفندق بعينه.</p>}
        <div className="mt-4 divide-y divide-border">
          {hotelRules.map((rule) => (
            <details key={rule.id} className="py-4 first:pt-0 last:pb-0">
              <summary className="cursor-pointer text-sm font-medium text-foreground">
                {hotelNames.get(rule.scope_id ?? -1) ?? `فندق #${rule.scope_id}`}
                {!rule.is_active && <span className="text-muted-foreground"> (معطَّلة)</span>}
              </summary>
              {canEdit && (
                <div className="mt-4">
                  <PriceRuleEditForm
                    rule={rule}
                    scope="hotel"
                    scopeId={rule.scope_id}
                    onSaved={refresh}
                  />
                </div>
              )}
            </details>
          ))}
        </div>
      </section>

      <section className={CARD}>
        <h2 className={SECTION_TITLE}>قواعد أنواع الغرف</h2>
        {roomTypeRules.length === 0 && (
          <p className={`${HINT} mt-2`}>لا توجد قواعد خاصة بنوع غرفة بعينه.</p>
        )}
        <div className="mt-4 divide-y divide-border">
          {roomTypeRules.map((rule) => (
            <details key={rule.id} className="py-4 first:pt-0 last:pb-0">
              <summary className="cursor-pointer text-sm font-medium text-foreground">
                {roomTypeNames.get(rule.scope_id ?? -1) ?? `نوع غرفة #${rule.scope_id}`}
                {!rule.is_active && <span className="text-muted-foreground"> (معطَّلة)</span>}
              </summary>
              {canEdit && (
                <div className="mt-4">
                  <PriceRuleEditForm
                    rule={rule}
                    scope="room_type"
                    scopeId={rule.scope_id}
                    onSaved={refresh}
                  />
                </div>
              )}
            </details>
          ))}
        </div>
      </section>

      {canEdit && availableTargets.length > 0 && (
        <section className={CARD}>
          <h2 className={SECTION_TITLE}>إضافة قاعدة جديدة</h2>
          <select
            value={
              addingTarget ? `${addingTarget.scope}:${addingTarget.scopeId}` : ""
            }
            onChange={(event) => {
              const [scope, scopeId] = event.target.value.split(":");
              const target = availableTargets.find(
                (t) => t.scope === scope && t.scopeId === Number(scopeId)
              );
              setAddingTarget(target ?? null);
            }}
            className={`${SELECT} mt-4 w-full sm:w-80`}
          >
            <option value="">اختر نطاقاً…</option>
            {availableTargets.map((target) => (
              <option
                key={`${target.scope}:${target.scopeId}`}
                value={`${target.scope}:${target.scopeId}`}
              >
                {target.label}
              </option>
            ))}
          </select>
          {addingTarget && (
            <div className={`mt-4 ${FIELDSET}`}>
              <PriceRuleEditForm
                rule={null}
                scope={addingTarget.scope}
                scopeId={addingTarget.scopeId}
                onSaved={refresh}
              />
            </div>
          )}
        </section>
      )}
    </div>
  );
}
