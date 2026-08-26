import type { ReactNode } from "react";
import type { AppUser } from "@/lib/types";
import { BRAND_NAME, BRAND_TAGLINE } from "@/lib/brand";
import { HOME_NAV_ITEM, navItemsForRole } from "@/lib/nav";
import { ROLE_LABELS } from "@/lib/roleLabels";
import { BUTTON_SUBTLE, PAGE } from "@/lib/ui";
import { BrandMark } from "./BrandMark";
import { SidebarNav } from "./SidebarNav";
import { logout } from "./sessionActions";

// The persistent frame every signed-in screen renders inside: a fixed
// sidebar on wide viewports, a scrollable strip of the same links below the
// bar on narrow ones. Deliberately not a route-group layout — the sidebar
// needs the caller's AppUser, and every page already loads it to decide
// what it may show, so passing it in avoids a second round trip per render.
export function AppShell({ appUser, children }: { appUser: AppUser; children: ReactNode }) {
  const navItems = [HOME_NAV_ITEM, ...navItemsForRole(appUser.app_role)];

  return (
    <div className="flex min-h-full flex-1">
      <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col overflow-y-auto border-e border-border bg-shell lg:flex">
        <div className="flex items-center gap-3 px-5 py-6">
          <BrandMark />
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold text-foreground">
              {BRAND_NAME}
            </span>
            <span className="block truncate text-xs tracking-wide text-accent">
              {BRAND_TAGLINE}
            </span>
          </span>
        </div>

        <div className="flex-1 px-3 pb-4">
          <SidebarNav items={navItems} orientation="vertical" />
        </div>

        <p className="border-t border-border px-5 py-4 text-xs text-muted-foreground">
          عرض التكلفة: {appUser.can_view_cost ? "مفعّل" : "غير مفعّل"}
        </p>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-border bg-shell/95 backdrop-blur">
          <div className="flex items-center gap-4 px-4 py-3 sm:px-6">
            <div className="flex items-center gap-3 lg:hidden">
              <BrandMark className="h-8 w-8" />
              <span className="text-sm font-semibold text-foreground">{BRAND_NAME}</span>
            </div>

            <div className="ms-auto flex items-center gap-3">
              <span className="hidden text-end sm:block">
                <span className="block text-sm font-medium leading-tight text-foreground">
                  {appUser.full_name}
                </span>
                <span className="block text-xs leading-tight text-muted-foreground">
                  {ROLE_LABELS[appUser.app_role]}
                </span>
              </span>
              <span
                aria-hidden="true"
                className="flex h-9 w-9 items-center justify-center rounded-full border border-accent/30 bg-accent/10 text-sm font-semibold text-accent"
              >
                {appUser.full_name.trim().charAt(0)}
              </span>
              <form action={logout}>
                <button type="submit" className={BUTTON_SUBTLE}>
                  خروج
                </button>
              </form>
            </div>
          </div>

          <div className="border-t border-border px-4 py-2 sm:px-6 lg:hidden">
            <SidebarNav items={navItems} orientation="horizontal" />
          </div>
        </header>

        <main className={PAGE}>{children}</main>
      </div>
    </div>
  );
}
