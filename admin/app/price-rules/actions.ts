"use server";

import { revalidatePath } from "next/cache";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import { translateConstraintError } from "@/lib/postgresErrors";
import { validateLeadTimeBands, validateOccupancyBands } from "@/lib/priceRuleBands";
import type { DemandCurve, MinProfitByLeadTime, PriceRuleScope } from "@/lib/types";

const PRICE_RULES_PATH = "/price-rules";

export interface PriceRuleActionResult {
  error?: string;
}

// Called directly from the client workspace (not a <form action>): the
// band arrays are dynamic (rows added/removed), which FormData has no
// natural encoding for — same reasoning admin/app/seasons/actions.ts's
// reorderSeasons gives for its own direct-call, typed-argument shape.
//
// Defense-in-depth fast path in front of migration 0018/0020's RLS
// policies, which remain the real enforcement layer — same pattern as
// every other write in this dashboard.
async function requireAdminWithCostVisibility(): Promise<string | null> {
  const appUser = await getCurrentAppUser();
  if (!appUser || appUser.app_role !== "admin" || !appUser.can_view_cost) {
    return "هذا الإجراء يتطلب صلاحية المدير وصلاحية عرض التكلفة معاً.";
  }
  return null;
}

// Re-runs admin/lib/priceRuleBands.ts's validation server-side before ever
// calling admin_upsert_price_rule — the client workspace already checks
// this on every edit for live feedback, but that check runs in the
// browser and CLAUDE.md rule 3's principle (application-level checks are
// advisory, the server is the real gate) applies here just as much as it
// does to inventory. A caller that bypasses the browser UI still gets the
// same specific, Arabic diagnosis this whole layer exists to produce —
// not just Postgres's raw check-constraint fallback. null arguments are
// "this scope does not override this field" (see admin_upsert_price_rule's
// own comment) and have nothing to validate.
function validateBandsOrError(
  minProfitByLeadTime: MinProfitByLeadTime | null,
  demandCurve: DemandCurve | null
): string | null {
  if (minProfitByLeadTime !== null) {
    const minProfit = validateLeadTimeBands(
      minProfitByLeadTime.bands,
      "min_profit_halalas",
      "حد الربح الأدنى"
    );
    if (!minProfit.valid) {
      return minProfit.message;
    }
  }
  if (demandCurve !== null) {
    const demandLeadTime = validateLeadTimeBands(
      demandCurve.lead_time_bands,
      "multiplier_bps",
      "مضاعِف مدة الحجز"
    );
    if (!demandLeadTime.valid) {
      return demandLeadTime.message;
    }
    const occupancy = validateOccupancyBands(demandCurve.occupancy_bands);
    if (!occupancy.valid) {
      return occupancy.message;
    }
  }
  return null;
}

// targetMarginBps/minProfitByLeadTime/demandCurve are each independently
// nullable: admin_upsert_price_rule is a whole-row replace over a
// field-by-field inheritance table (ARCHITECTURE.md §5), so null means
// "this scope does not override this field, fall through to the next less
// specific one" — not "clear it to zero". The global rule's caller must
// never pass null for any of the three (price_rules_global_is_complete
// enforces this at the database level regardless).
export async function upsertPriceRule(
  scope: PriceRuleScope,
  scopeId: number | null,
  targetMarginBps: number | null,
  minProfitByLeadTime: MinProfitByLeadTime | null,
  demandCurve: DemandCurve | null
): Promise<PriceRuleActionResult> {
  const authError = await requireAdminWithCostVisibility();
  if (authError) {
    return { error: authError };
  }
  if (
    targetMarginBps !== null &&
    (!Number.isInteger(targetMarginBps) || targetMarginBps < 0)
  ) {
    return { error: "الهامش المستهدف يجب أن يكون رقماً صحيحاً غير سالب." };
  }
  const bandsError = validateBandsOrError(minProfitByLeadTime, demandCurve);
  if (bandsError) {
    return { error: bandsError };
  }

  const supabase = await createClient();
  const { error } = await supabase.rpc("admin_upsert_price_rule", {
    rule_scope: scope,
    rule_scope_id: scopeId,
    new_target_margin_bps: targetMarginBps,
    new_min_profit_by_lead_time: minProfitByLeadTime,
    new_demand_curve: demandCurve,
  });
  if (error) {
    return { error: translateConstraintError(error.message) };
  }
  revalidatePath(PRICE_RULES_PATH);
  return {};
}

export async function setPriceRuleActive(
  ruleId: number,
  isActive: boolean
): Promise<PriceRuleActionResult> {
  const authError = await requireAdminWithCostVisibility();
  if (authError) {
    return { error: authError };
  }

  const supabase = await createClient();
  const { error } = await supabase.rpc("admin_set_price_rule_active", {
    rule_id: ruleId,
    new_is_active: isActive,
  });
  if (error) {
    return { error: translateConstraintError(error.message) };
  }
  revalidatePath(PRICE_RULES_PATH);
  return {};
}
