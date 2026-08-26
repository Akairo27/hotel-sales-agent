import Link from "next/link";
import { redirect } from "next/navigation";
import { getCurrentAppUser } from "@/lib/session";
import { ROLE_LABELS } from "@/lib/roleLabels";
import { PAGE } from "@/lib/ui";

type NavItem = {
  href: string;
  label: string;
  description: string;
};

const NAV_ITEMS: NavItem[] = [
  { href: "/hotels", label: "الفنادق وأنواع الغرف", description: "إدارة الفنادق وأنواع الغرف المتاحة" },
  { href: "/seasons", label: "المواسم", description: "تقويم المواسم وحدود الأسعار" },
  { href: "/allotments", label: "التكلفة", description: "تكلفة المخزون اليومية لكل نوع غرفة" },
  { href: "/price-rules", label: "قواعد التسعير", description: "قواعد الأسعار حسب الموسم والطلب" },
  { href: "/price-overrides", label: "تجاوزات الأسعار", description: "استثناءات سعرية على تواريخ محددة" },
];

const ADMIN_ONLY_NAV_ITEM: NavItem = {
  href: "/users",
  label: "الصلاحيات",
  description: "إدارة حسابات المستخدمين وأدوارهم",
};

export default async function DashboardPage() {
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }

  const navItems =
    appUser.app_role === "admin" ? [...NAV_ITEMS, ADMIN_ONLY_NAV_ITEM] : NAV_ITEMS;

  return (
    <main className={PAGE}>
      <header className="mb-10">
        <p className="text-sm text-muted-foreground">مرحباً بعودتك</p>
        <h1 className="mt-1 text-2xl font-semibold text-foreground sm:text-3xl">
          {appUser.full_name}
        </h1>
        <div className="mt-4 flex flex-wrap gap-2">
          <span className="rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-muted-foreground">
            {ROLE_LABELS[appUser.app_role]}
          </span>
          <span className="rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-muted-foreground">
            عرض التكلفة: {appUser.can_view_cost ? "مفعّل" : "غير مفعّل"}
          </span>
        </div>
      </header>

      <nav>
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {navItems.map((item) => (
            <li key={item.href}>
              <Link
                href={item.href}
                className="group flex h-full flex-col justify-between rounded-2xl border border-border bg-surface p-6 transition hover:border-accent/40 hover:shadow-sm"
              >
                <div>
                  <span className="block text-base font-medium text-foreground">
                    {item.label}
                  </span>
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
    </main>
  );
}
