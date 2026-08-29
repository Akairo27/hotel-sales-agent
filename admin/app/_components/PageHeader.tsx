import Link from "next/link";

interface Breadcrumb {
  href: string;
  label: string;
}

// Every screen opens the same way: an optional trail back up, a gold rule,
// the title, and an optional line saying what the screen is for. Written
// once here so the eight screens cannot drift apart in type size or spacing.
export function PageHeader({
  breadcrumb,
  title,
  description,
}: {
  breadcrumb?: Breadcrumb;
  title: string;
  description?: string;
}) {
  return (
    <header className="mb-8 border-b border-border pb-6">
      {breadcrumb && (
        <Link
          href={breadcrumb.href}
          className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted-foreground transition hover:text-accent"
        >
          {/* A right-pointing arrow: in this app's RTL layout, "back to a
              less-nested screen" reads toward the line's start. */}
          <span aria-hidden="true">→</span>
          {breadcrumb.label}
        </Link>
      )}
      <span aria-hidden="true" className="mb-3 block h-0.5 w-8 rounded-full bg-accent" />
      <h1 className="text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">{title}</h1>
      {description && <p className="mt-2 text-sm text-muted-foreground">{description}</p>}
    </header>
  );
}
