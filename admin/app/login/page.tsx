import { BRAND_NAME, BRAND_TAGLINE } from "@/lib/brand";
import { ALERT_ERROR, BUTTON_PRIMARY, INPUT, LABEL } from "@/lib/ui";
import { BrandMark } from "@/app/_components/BrandMark";
import { login } from "./actions";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;

  return (
    <main className="relative flex flex-1 items-center justify-center overflow-hidden p-6">
      {/* The only decorative element in the whole dashboard: a single warm
          wash behind the sign-in card, so the first screen anyone sees is
          not a flat black rectangle. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-80 bg-[radial-gradient(60%_100%_at_50%_0%,rgba(212,176,106,0.12),transparent_70%)]"
      />

      <div className="relative w-full max-w-md rounded-3xl border border-border bg-surface p-8 shadow-2xl shadow-black/40 sm:p-10">
        <div className="mb-8 flex flex-col items-center text-center">
          <BrandMark className="h-12 w-12" />
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-foreground">
            {BRAND_NAME}
          </h1>
          <p className="mt-1 text-sm tracking-wide text-accent">{BRAND_TAGLINE}</p>
        </div>

        {error && (
          <p role="alert" className={`${ALERT_ERROR} mb-6`}>
            {error}
          </p>
        )}

        <form className="space-y-5">
          <div className="space-y-2">
            <label htmlFor="email" className={LABEL}>
              البريد الإلكتروني
            </label>
            <input
              id="email"
              name="email"
              type="email"
              required
              autoComplete="email"
              className={`${INPUT} w-full`}
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="password" className={LABEL}>
              كلمة المرور
            </label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autoComplete="current-password"
              className={`${INPUT} w-full`}
            />
          </div>

          <button formAction={login} type="submit" className={`${BUTTON_PRIMARY} w-full`}>
            دخول
          </button>
        </form>
      </div>
    </main>
  );
}
