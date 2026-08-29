import Link from "next/link";
import { redirect } from "next/navigation";
import { getCurrentAppUser } from "@/lib/session";
import { navItemsForRole } from "@/lib/nav";
import { ROLE_LABELS } from "@/lib/roleLabels";
import { BADGE, BADGE_ACCENT } from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { NavIcon } from "@/app/_components/NavIcon";

export default async function DashboardPage() {
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }

  const navItems = navItemsForRole(appUser.app_role);

  return (
    <AppShell appUser={appUser}>
      <header className="mb-10 border-b border-border pb-6">
        <span aria-hidden="true" className="mb-3 block h-0.5 w-8 rounded-full bg-accent" />
        <p className="text-sm text-muted-foreground">مرحباً بعودتك</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
          {appUser.full_name}
        </h1>
        <div className="mt-4 flex flex-wrap gap-2">
          <span className={BADGE}>{ROLE_LABELS[appUser.app_role]}</span>
          <span className={appUser.can_view_cost ? BADGE_ACCENT : BADGE}>
            عرض التكلفة: {appUser.can_view_cost ? "مفعّل" : "غير مفعّل"}
          </span>
        </div>
      </header>

      <nav aria-label="شاشات اللوحة">
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {navItems.map((item) => (
            <li key={item.href}>
              <Link
                href={item.href}
                className="group flex h-full flex-col justify-between rounded-2xl border border-border bg-surface p-6 transition hover:border-accent/40 hover:bg-surface-subtle"
              >
                <div>
                  <span className="mb-4 flex h-10 w-10 items-center justify-center rounded-xl border border-accent/25 bg-accent/10 text-accent">
                    <NavIcon name={item.icon} />
                  </span>
                  <span className="block text-base font-medium text-foreground">{item.label}</span>
                  <span className="mt-1 block text-sm text-muted-foreground">
                    {item.description}
                  </span>
                </div>
                <span className="mt-6 inline-flex items-center gap-1 text-sm font-medium text-accent">
                  فتح
                  <span aria-hidden="true" className="transition group-hover:-translate-x-0.5">
                    ←
                  </span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </AppShell>
  );
}
