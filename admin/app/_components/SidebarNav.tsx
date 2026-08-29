"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { NavItem } from "@/lib/nav";
import { NavIcon } from "./NavIcon";

// `/hotels` stays lit while the reader is on `/hotels/3` — matching on the
// exact path alone would leave the whole sidebar dark on every detail
// screen, which is precisely where knowing your place matters most.
function isCurrent(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

const VERTICAL_ITEM =
  "flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition";
const HORIZONTAL_ITEM =
  "flex shrink-0 items-center gap-2 whitespace-nowrap rounded-lg px-3 py-1.5 text-sm transition";

const ACTIVE = "bg-accent/10 font-medium text-accent";
const INACTIVE = "text-muted-foreground hover:bg-surface hover:text-foreground";

export function SidebarNav({
  items,
  orientation,
}: {
  items: NavItem[];
  orientation: "vertical" | "horizontal";
}) {
  const pathname = usePathname();
  const isVertical = orientation === "vertical";

  return (
    <nav aria-label="أقسام اللوحة">
      <ul className={isVertical ? "space-y-1" : "flex gap-1 overflow-x-auto"}>
        {items.map((item) => {
          const current = isCurrent(pathname, item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={current ? "page" : undefined}
                className={`${isVertical ? VERTICAL_ITEM : HORIZONTAL_ITEM} ${
                  current ? ACTIVE : INACTIVE
                }`}
              >
                <NavIcon name={item.icon} className={isVertical ? "h-5 w-5" : "h-4 w-4"} />
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
