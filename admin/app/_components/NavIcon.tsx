import type { NavIconName } from "@/lib/nav";
import type { ReactElement } from "react";

// Line icons drawn inline instead of pulled from an icon package: six
// glyphs do not justify a runtime dependency, and inline paths inherit
// currentColor so the active/inactive nav states need no second rule.
const PATHS: Record<NavIconName, ReactElement> = {
  home: (
    <>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
    </>
  ),
  hotels: (
    <>
      <path d="M4.5 20.5V4.75A1.25 1.25 0 0 1 5.75 3.5h7A1.25 1.25 0 0 1 14 4.75V20.5" />
      <path d="M14 10.5h4.25a1.25 1.25 0 0 1 1.25 1.25V20.5" />
      <path d="M3 20.5h18" />
      <path d="M7.5 7.5h3M7.5 11.5h3M7.5 15.5h3" />
    </>
  ),
  seasons: (
    <>
      <rect x="3.5" y="5" width="17" height="15.5" rx="1.75" />
      <path d="M3.5 9.75h17M8 3.5v3M16 3.5v3" />
    </>
  ),
  cost: (
    <>
      <path d="M3.9 12.7 12 4.6h5.5A1.9 1.9 0 0 1 19.4 6.5V12l-8.1 8.1a1.9 1.9 0 0 1-2.7 0l-4.7-4.7a1.9 1.9 0 0 1 0-2.7Z" />
      <circle cx="15.6" cy="8.4" r="1.15" />
    </>
  ),
  rules: (
    <>
      <path d="M3.5 6.5h16M3.5 12h16M3.5 17.5h16" />
      <circle cx="9" cy="6.5" r="2.1" />
      <circle cx="15" cy="12" r="2.1" />
      <circle cx="8" cy="17.5" r="2.1" />
    </>
  ),
  overrides: (
    <>
      <rect x="3.5" y="5" width="17" height="15.5" rx="1.75" />
      <path d="M3.5 9.75h17M8 3.5v3M16 3.5v3" />
      <path d="m12 12.25 2.25 2.25L12 16.75 9.75 14.5Z" />
    </>
  ),
  users: (
    <>
      <circle cx="10" cy="8.25" r="3.5" />
      <path d="M3.75 19.5a6.25 6.25 0 0 1 12.5 0" />
      <path d="m16.5 12.75 1.75 1.75 3.25-3.25" />
    </>
  ),
};

export function NavIcon({ name, className = "h-5 w-5" }: { name: NavIconName; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
    >
      {PATHS[name]}
    </svg>
  );
}
