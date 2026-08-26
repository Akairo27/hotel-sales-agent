// Two squares at 45° to each other — the eight-point khatam star of
// Islamic geometry, drawn rather than imported so the shell carries an
// identity without pulling in an icon dependency or a raster asset.
export function BrandMark({ className = "h-9 w-9" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-flex shrink-0 items-center justify-center rounded-xl border border-accent/30 bg-accent/10 text-accent ${className}`}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        className="h-[60%] w-[60%]"
      >
        <rect x="5" y="5" width="14" height="14" />
        <rect x="5" y="5" width="14" height="14" transform="rotate(45 12 12)" />
      </svg>
    </span>
  );
}
