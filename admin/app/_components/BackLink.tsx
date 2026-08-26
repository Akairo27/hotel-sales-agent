import Link from "next/link";
import type { ReactNode } from "react";

// A right-pointing arrow, not next/font/google's left-pointing default —
// in this app's RTL layout (admin/app/layout.tsx's dir="rtl"), "back to a
// less-nested page" reads toward the line's start, which is the right.
export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="mb-6 inline-flex items-center gap-1 text-sm text-muted-foreground transition hover:text-accent"
    >
      <span aria-hidden="true">→</span>
      {children}
    </Link>
  );
}
