import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PriceRuleEditForm } from "./PriceRuleEditForm";
import type { PriceRuleForDashboard } from "@/lib/types";

// A hotel-scoped rule that overrides target_margin_bps and
// min_profit_by_lead_time but leaves demand_curve inherited from the
// global rule — demand_curve: null is the normal, UI-reachable state for
// any non-global row (checkbox left unchecked), not an edge case.
const hotelRuleWithInheritedDemandCurve: PriceRuleForDashboard = {
  id: 2,
  scope: "hotel",
  scope_id: 2,
  demand_curve: null,
  created_at: "2026-08-21T00:00:00Z",
  is_active: true,
  target_margin_bps: 1500,
  min_profit_by_lead_time: {
    bands: [{ min_lead_days: 0, max_lead_days: null, min_profit_halalas: 0 }],
  },
};

// Isolates the "منحنى الطلب" <fieldset> so the assertion doesn't depend on
// how many band rows the other two fieldsets happen to render (each open-
// ended band row contributes its own unrelated checkbox).
function demandCurveFieldset(html: string): string {
  const start = html.indexOf("منحنى الطلب");
  const end = html.indexOf("</fieldset>", start);
  return html.slice(start, end);
}

describe("PriceRuleEditForm", () => {
  it("renders a scoped rule with an inherited (null) demand_curve without throwing", () => {
    expect(() =>
      renderToStaticMarkup(
        <PriceRuleEditForm
          rule={hotelRuleWithInheritedDemandCurve}
          scope="hotel"
          scopeId={2}
          onSaved={() => {}}
        />
      )
    ).not.toThrow();
  });

  it("shows the demand-curve section as inherited: unchecked, band editor collapsed", () => {
    const html = renderToStaticMarkup(
      <PriceRuleEditForm
        rule={hotelRuleWithInheritedDemandCurve}
        scope="hotel"
        scopeId={2}
        onSaved={() => {}}
      />
    );
    const section = demandCurveFieldset(html);
    expect(section).not.toContain("checked=\"\"");
    // BandRowsEditor only renders while overrideDemandCurve is true — its
    // occupancy-bands heading must be absent when the field is inherited.
    expect(section).not.toContain("حسب نسبة الإشغال");
  });

  it("still shows the margin and min-profit overrides as checked (both non-null on this rule)", () => {
    const html = renderToStaticMarkup(
      <PriceRuleEditForm
        rule={hotelRuleWithInheritedDemandCurve}
        scope="hotel"
        scopeId={2}
        onSaved={() => {}}
      />
    );
    const marginSection = html.slice(
      html.indexOf("الهامش المستهدف"),
      html.indexOf("</fieldset>", html.indexOf("الهامش المستهدف"))
    );
    const minProfitSection = html.slice(
      html.indexOf("حد الربح الأدنى"),
      html.indexOf("</fieldset>", html.indexOf("حد الربح الأدنى"))
    );
    expect(marginSection).toContain("checked=\"\"");
    expect(minProfitSection).toContain("checked=\"\"");
  });
});
