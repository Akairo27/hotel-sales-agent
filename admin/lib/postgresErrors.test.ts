import { describe, expect, it } from "vitest";
import { translateConstraintError } from "./postgresErrors";

describe("translateConstraintError", () => {
  it("maps a known price_rules CHECK violation to its Arabic message", () => {
    const raw =
      'new row for relation "price_rules" violates check constraint ' +
      '"price_rules_min_profit_bands_valid"';
    expect(translateConstraintError(raw)).toBe(
      "فترات حد الربح الأدنى غير مكتملة أو متداخلة أو فيها فجوة — راجع الفترات وحاول مرة أخرى."
    );
  });

  it("maps a known price_rules UNIQUE violation to its Arabic message", () => {
    const raw =
      'duplicate key value violates unique constraint "price_rules_single_global"';
    expect(translateConstraintError(raw)).toBe(
      "توجد قاعدة عامة واحدة بالفعل — لا يمكن إنشاء أخرى."
    );
  });

  it("maps a known price_overrides CHECK violation to its Arabic message", () => {
    const raw =
      'new row for relation "price_overrides" violates check constraint ' +
      '"price_overrides_min_allowed_not_above_ask"';
    expect(translateConstraintError(raw)).toBe(
      "الحد الأدنى المسموح لا يمكن أن يتجاوز سعر العرض."
    );
  });

  it("maps price_overrides' two non-negative CHECK violations to their Arabic messages", () => {
    expect(
      translateConstraintError(
        'violates check constraint "price_overrides_ask_price_non_negative"'
      )
    ).toBe("سعر العرض لا يمكن أن يكون سالباً.");
    expect(
      translateConstraintError(
        'violates check constraint "price_overrides_min_allowed_non_negative"'
      )
    ).toBe("الحد الأدنى المسموح لا يمكن أن يكون سالباً.");
  });

  it("falls back to the generic message for an unrecognized constraint name", () => {
    const raw = 'violates check constraint "some_future_constraint_nobody_mapped_yet"';
    expect(translateConstraintError(raw)).toBe("تعذر الحفظ — تحقق من صحة القيم المدخلة.");
  });

  it("falls back to the generic message for a non-constraint error", () => {
    // Covers both screens' wrapper-function permission errors (e.g.
    // admin_upsert_price_rule's, admin_upsert_price_overrides') and the
    // range RPC's own RAISE EXCEPTION text — none of these are CHECK/UNIQUE
    // violations, so none have a specific Arabic entry; the pre-flight
    // checks in each actions.ts are what should catch these cases first.
    expect(translateConstraintError("not permitted to update this price rule")).toBe(
      "تعذر الحفظ — تحقق من صحة القيم المدخلة."
    );
    expect(translateConstraintError("date range must not exceed 180 nights")).toBe(
      "تعذر الحفظ — تحقق من صحة القيم المدخلة."
    );
  });

  it("never echoes the raw English message back for a matched constraint", () => {
    const raw = 'violates check constraint "price_rules_global_always_active"';
    const translated = translateConstraintError(raw);
    expect(translated).not.toContain("violates");
    expect(translated).not.toContain("constraint");
  });
});
