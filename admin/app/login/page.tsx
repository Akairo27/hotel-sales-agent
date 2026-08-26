import { login } from "./actions";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div className="w-full max-w-sm rounded-3xl border border-border bg-surface p-8 shadow-sm shadow-black/5 sm:p-10">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold text-foreground">تسجيل الدخول</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            لوحة تحكم المبيعات والتسعير
          </p>
        </div>

        {error && (
          <p
            role="alert"
            className="mb-6 rounded-xl border border-danger/20 bg-danger-background px-4 py-3 text-sm text-danger"
          >
            {error}
          </p>
        )}

        <form className="space-y-5">
          <div className="space-y-2">
            <label htmlFor="email" className="block text-sm font-medium text-foreground">
              البريد الإلكتروني
            </label>
            <input
              id="email"
              name="email"
              type="email"
              required
              autoComplete="email"
              className="w-full rounded-xl border border-border bg-background px-4 py-2.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/25"
            />
          </div>

          <div className="space-y-2">
            <label htmlFor="password" className="block text-sm font-medium text-foreground">
              كلمة المرور
            </label>
            <input
              id="password"
              name="password"
              type="password"
              required
              autoComplete="current-password"
              className="w-full rounded-xl border border-border bg-background px-4 py-2.5 text-sm text-foreground outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/25"
            />
          </div>

          <button
            formAction={login}
            type="submit"
            className="w-full rounded-xl bg-accent px-4 py-2.5 text-sm font-medium text-accent-foreground transition hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-accent/40 focus:ring-offset-2 focus:ring-offset-surface"
          >
            دخول
          </button>
        </form>
      </div>
    </main>
  );
}
