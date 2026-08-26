import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { AppUser } from "@/lib/types";
import { ROLE_LABELS } from "@/lib/roleLabels";
import { ALERT_ERROR, BUTTON_SECONDARY, CARD, HINT, PAGE } from "@/lib/ui";
import { BackLink } from "@/app/_components/BackLink";
import { updateAppRole, updateCanViewCost } from "./actions";

export default async function UsersPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }
  if (appUser.app_role !== "admin") {
    redirect("/dashboard");
  }

  const supabase = await createClient();
  const { data: users } = await supabase
    .from("app_users")
    .select("id, full_name, app_role, can_view_cost, is_active, created_at")
    .order("full_name")
    .overrideTypes<AppUser[], { merge: false }>();

  return (
    <main className={PAGE}>
      <BackLink href="/dashboard">لوحة التحكم</BackLink>
      <h1 className="text-2xl font-semibold text-foreground sm:text-3xl">الصلاحيات</h1>

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mt-4`}>
          {error}
        </p>
      )}
      <p className={`${HINT} mt-4`}>كل تغيير هنا يُسجَّل في سجل التغييرات — من غيّر، ماذا، ومتى.</p>

      <ul className="mt-8 space-y-3">
        {(users ?? []).map((user) => (
          <li key={user.id} className={CARD}>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <strong className="text-base font-medium text-foreground">{user.full_name}</strong>
                {!user.is_active && (
                  <span className="ms-2 text-sm text-muted-foreground">(غير نشط)</span>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                <RoleForm user={user} />
                <CanViewCostForm user={user} />
              </div>
            </div>
          </li>
        ))}
      </ul>
    </main>
  );
}

function RoleForm({ user }: { user: AppUser }) {
  const updateThisUsersRole = updateAppRole.bind(null, user.id);
  const nextRole = user.app_role === "admin" ? "sales" : "admin";
  return (
    <form action={updateThisUsersRole}>
      <input type="hidden" name="app_role" value={nextRole} />
      <button type="submit" className={BUTTON_SECONDARY}>
        الدور: {ROLE_LABELS[user.app_role]} — تحويل إلى {ROLE_LABELS[nextRole]}
      </button>
    </form>
  );
}

function CanViewCostForm({ user }: { user: AppUser }) {
  const updateThisUsersCostVisibility = updateCanViewCost.bind(null, user.id);
  const nextValue = user.can_view_cost ? "false" : "true";
  return (
    <form action={updateThisUsersCostVisibility}>
      <input type="hidden" name="can_view_cost" value={nextValue} />
      <button type="submit" className={BUTTON_SECONDARY}>
        عرض التكلفة: {user.can_view_cost ? "مفعّل" : "غير مفعّل"} —{" "}
        {user.can_view_cost ? "إلغاء" : "تفعيل"}
      </button>
    </form>
  );
}
