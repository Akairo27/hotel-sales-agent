// Shared Tailwind class strings for the admin UI's visual language: a
// dark, gold-accented surface with generous spacing — see
// admin/app/globals.css for the underlying color tokens and
// admin/app/_components/AppShell.tsx for the frame they sit in.
// Centralized here so every screen's forms and tables stay visually
// identical instead of each re-deriving its own version.

export const PAGE = "mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-8 sm:py-12";

export const CARD = "rounded-2xl border border-border bg-surface p-6";

// A card meant to be read before the ones under it — the gold hairline on
// its start edge is the only place the accent is used structurally rather
// than for an interactive affordance.
export const CARD_FEATURED = "rounded-2xl border border-border border-s-2 border-s-accent bg-surface p-6";

// Deliberately carries no width utility — every call site must add its own
// (`w-full` for a field meant to fill its label/row, a fixed `w-NN` for a
// short numeric field). Tailwind v4 orders utilities by discovery, not by
// position in the className string, so `${INPUT} w-24` cannot reliably
// override a `w-full` baked in here: the two just race, and w-full used to
// win — every "why is this one-digit field full width" bug traced back to
// exactly that.
export const INPUT =
  "rounded-xl border border-border bg-background px-3 py-2 text-sm text-foreground outline-none transition placeholder:text-muted-foreground hover:border-border-strong focus:border-accent focus:ring-2 focus:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-60";

export const SELECT = INPUT;

// A lightweight field-group divider for a form section nested inside a
// CARD (an already-boxed rule, an already-boxed page section) — a second
// rounded border box at that depth reads as visually identical to the
// level above it. Also carries min-w-0: a bare <fieldset> is one of the
// few elements whose UA default is min-width: min-content, not 0 — it
// silently refuses to shrink to its container and pushes wide descendants
// (a min-w-max band table, in practice) out past every ancestor's edge.
// Pair with LEGEND; if the element isn't a <fieldset>, drop min-w-0.
export const FIELDSET = "min-w-0 border-t border-border pt-4";

export const LEGEND = "mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground";

// A save/submit action's own bar, not a button left floating at the end
// of a form — the divider gives it a fixed anchor instead of trailing
// whitespace.
export const ACTION_BAR = "mt-6 flex items-center justify-end gap-3 border-t border-border pt-4";

export const LABEL = "block text-sm font-medium text-foreground";

export const CHECKBOX_LABEL =
  "inline-flex items-center gap-2 text-sm text-foreground";

// accent-accent, not text-accent: a native checkbox takes its checked fill
// from CSS accent-color, which no text-color utility feeds. `text-accent`
// left every box rendering in the browser's default blue — invisible on a
// light theme, glaring against gold on this one.
export const CHECKBOX =
  "h-4 w-4 rounded border-border accent-accent focus:ring-2 focus:ring-accent/25";

// hover:bg-accent-hover, not hover:opacity-90: on a near-black page,
// fading the gold toward the background reads as the button going
// *disabled*, which is the opposite of the intended affordance.
export const BUTTON_PRIMARY =
  "rounded-xl bg-accent px-4 py-2 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hover focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-60";

export const BUTTON_SECONDARY =
  "rounded-xl border border-border bg-surface px-4 py-2 text-sm font-medium text-foreground transition hover:border-accent/50 hover:text-accent focus:outline-none focus:ring-2 focus:ring-accent/25 disabled:cursor-not-allowed disabled:opacity-60";

// A tertiary action that must not compete with anything on the page —
// the shell's sign-out, and nothing heavier than that.
export const BUTTON_SUBTLE =
  "rounded-lg border border-transparent px-3 py-1.5 text-sm font-medium text-muted-foreground transition hover:border-border hover:text-foreground focus:outline-none focus:ring-2 focus:ring-accent/25";

export const BUTTON_DANGER =
  "rounded-xl border border-danger/30 bg-danger-background px-3 py-1.5 text-sm font-medium text-danger transition hover:border-danger/60 disabled:cursor-not-allowed disabled:opacity-60";

export const ALERT_ERROR =
  "rounded-xl border border-danger/20 bg-danger-background px-4 py-3 text-sm text-danger";

export const ALERT_STATUS =
  "rounded-xl border border-accent/20 bg-accent/10 px-4 py-3 text-sm text-foreground";

export const TABLE_WRAPPER = "overflow-x-auto rounded-2xl border border-border bg-surface";

export const TABLE = "w-full min-w-max text-sm";

export const TH =
  "border-b border-border bg-surface-subtle px-4 py-3 text-start text-xs font-semibold uppercase tracking-wide text-muted-foreground";

export const TD = "border-b border-border px-4 py-2.5 align-middle";

// Applied to <tr> in a table body. Rows on a dark surface run together
// without it; the tint is the same one TH uses, at a fraction of its weight.
export const TABLE_ROW = "transition hover:bg-surface-subtle/50";

export const SECTION_TITLE = "text-lg font-semibold tracking-tight text-foreground";

export const HINT = "text-sm text-muted-foreground";

// A read-only status pill — a role, a permission state — never a control.
export const BADGE =
  "inline-flex items-center rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-muted-foreground";

export const BADGE_ACCENT =
  "inline-flex items-center rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-xs font-medium text-accent";
