"use client";

import { BUTTON_DANGER, BUTTON_SECONDARY, CHECKBOX, CHECKBOX_LABEL, INPUT, TABLE, TABLE_WRAPPER, TD, TH } from "@/lib/ui";

// A normalized {min, max, value} shape shared by all three band lists
// this screen edits (min_profit_by_lead_time.bands, demand_curve's
// lead_time_bands and occupancy_bands) — the parent form maps each real
// band type to and from this shape, since the three differ only in field
// names (min_lead_days/max_lead_days/min_profit_halalas vs. min/max/
// multiplier_bps) and whether max may be open-ended (null).
export interface GenericBand {
  min: number;
  max: number | null;
  value: number;
}

interface BandRowsEditorProps {
  bands: GenericBand[];
  onChange: (bands: GenericBand[]) => void;
  valueLabel: string;
  // Lead-time chains end with one open-ended band (max = null); occupancy
  // is closed at 1 and never has a null max — see
  // admin/lib/priceRuleBands.ts's own validateChain for the same split.
  allowOpenEnded: boolean;
  step?: number;
}

export function BandRowsEditor({
  bands,
  onChange,
  valueLabel,
  allowOpenEnded,
  step = 1,
}: BandRowsEditorProps) {
  function updateRow(index: number, partial: Partial<GenericBand>): void {
    onChange(bands.map((band, i) => (i === index ? { ...band, ...partial } : band)));
  }

  function addRow(): void {
    const lastMax = bands.length > 0 ? bands[bands.length - 1].max : null;
    onChange([
      ...bands,
      { min: lastMax ?? 0, max: allowOpenEnded ? null : lastMax ?? 0, value: 0 },
    ]);
  }

  function removeRow(index: number): void {
    onChange(bands.filter((_, i) => i !== index));
  }

  return (
    <div className={TABLE_WRAPPER}>
      <table className={TABLE}>
        <thead>
          <tr>
            <th className={TH}>من</th>
            <th className={TH}>إلى</th>
            <th className={TH}>{valueLabel}</th>
            <th className={TH}></th>
          </tr>
        </thead>
        <tbody>
          {bands.map((band, index) => (
            // Rows have no stable id of their own; index is fine here since
            // the list is only ever appended to or filtered, never reordered.
            <tr key={index}>
              <td className={TD}>
                <input
                  type="number"
                  step={step}
                  value={band.min}
                  onChange={(event) => updateRow(index, { min: Number(event.target.value) })}
                  className={`${INPUT} w-24`}
                />
              </td>
              <td className={TD}>
                <div className="flex items-center gap-2">
                  {allowOpenEnded && (
                    <label className={CHECKBOX_LABEL}>
                      <input
                        type="checkbox"
                        checked={band.max === null}
                        onChange={(event) =>
                          updateRow(index, { max: event.target.checked ? null : band.min })
                        }
                        className={CHECKBOX}
                      />
                      مفتوحة
                    </label>
                  )}
                  {band.max !== null && (
                    <input
                      type="number"
                      step={step}
                      value={band.max}
                      onChange={(event) =>
                        updateRow(index, { max: Number(event.target.value) })
                      }
                      className={`${INPUT} w-24`}
                    />
                  )}
                </div>
              </td>
              <td className={TD}>
                <input
                  type="number"
                  step={step}
                  value={band.value}
                  onChange={(event) =>
                    updateRow(index, { value: Number(event.target.value) })
                  }
                  className={`${INPUT} w-28`}
                />
              </td>
              <td className={TD}>
                <button type="button" onClick={() => removeRow(index)} className={BUTTON_DANGER}>
                  حذف
                </button>
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={4} className={TD}>
              <button type="button" onClick={addRow} className={BUTTON_SECONDARY}>
                إضافة فترة
              </button>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
