import type { AppRole } from "@/lib/types";

export type NavIconName =
  | "home"
  | "hotels"
  | "seasons"
  | "cost"
  | "rules"
  | "overrides"
  | "users";

export interface NavItem {
  href: string;
  label: string;
  description: string;
  icon: NavIconName;
  adminOnly?: boolean;
}

// The dashboard renders these as its landing cards and the shell renders
// them as its sidebar, so a screen added in one place can never go missing
// from the other.
export const NAV_ITEMS: NavItem[] = [
  {
    href: "/hotels",
    label: "الفنادق وأنواع الغرف",
    description: "إدارة الفنادق وأنواع الغرف المتاحة",
    icon: "hotels",
  },
  {
    href: "/seasons",
    label: "المواسم",
    description: "تقويم المواسم وحدود الأسعار",
    icon: "seasons",
  },
  {
    href: "/allotments",
    label: "التكلفة",
    description: "تكلفة المخزون اليومية لكل نوع غرفة",
    icon: "cost",
  },
  {
    href: "/price-rules",
    label: "قواعد التسعير",
    description: "قواعد الأسعار حسب الموسم والطلب",
    icon: "rules",
  },
  {
    href: "/price-overrides",
    label: "تجاوزات الأسعار",
    description: "استثناءات سعرية على تواريخ محددة",
    icon: "overrides",
  },
  {
    href: "/users",
    label: "الصلاحيات",
    description: "إدارة حسابات المستخدمين وأدوارهم",
    icon: "users",
    adminOnly: true,
  },
];

// The sidebar's own first entry. Kept out of NAV_ITEMS so the dashboard
// never renders a card pointing back at the dashboard.
export const HOME_NAV_ITEM: NavItem = {
  href: "/dashboard",
  label: "الرئيسية",
  description: "نظرة عامة على الشاشات",
  icon: "home",
};

export function navItemsForRole(role: AppRole): NavItem[] {
  return NAV_ITEMS.filter((item) => !item.adminOnly || role === "admin");
}
