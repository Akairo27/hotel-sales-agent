"use client";

import { useState, useTransition } from "react";
import { formatRiyadhDateTime } from "@/lib/escalations";
import {
  MAX_REPLY_LENGTH,
  isWithinCustomerServiceWindow,
  replyDraftProblem,
  replyProblemHint,
  replyState,
  replyStateLabel,
  sendResultLabel,
} from "@/lib/staffReply";
import type { StaffReplyRow } from "@/lib/types";
import {
  ALERT_ERROR,
  ALERT_STATUS,
  BADGE,
  BADGE_ACCENT,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  HINT,
  INPUT,
} from "@/lib/ui";
import { retryStaffReply, sendStaffReply } from "../../replyActions";

export interface BoxReply extends StaffReplyRow {
  /** Null when the author's name could not be read. */
  authorName: string | null;
}

const OUTSIDE_WINDOW_NOTICE =
  "مرّ أكثر من 24 ساعة على آخر رسالة من العميل، فلا يقبل واتساب رداً حراً. " +
  "تواصل معه هاتفياً، أو استخدم قالب إعادة التواصل حين يتوفر.";

const QUEUED_HINT = "لم يُرسل هذا الرد بعد. يمكنك إعادة المحاولة.";

const ENDED_BEFORE_SEND_HINT = "انتهى الاستلام قبل إرسال هذا الرد، فلم يُرسل. تواصل مع العميل إن لزم.";

const TEMPLATE_UNAVAILABLE = "قالب إعادة التواصل غير متاح بعد.";

function ReplyEntry({
  reply,
  retryable,
  canRewrite,
  pending,
  now,
  onRetry,
  onRewrite,
}: {
  reply: BoxReply;
  retryable: boolean;
  canRewrite: boolean;
  pending: boolean;
  now: Date;
  onRetry: () => void;
  onRewrite: () => void;
}) {
  const state = replyState(reply);
  const hint = replyProblemHint(reply, now);
  return (
    <li className="min-w-0 rounded-xl border border-border p-3">
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span className={state === "sent" ? BADGE_ACCENT : BADGE}>{replyStateLabel(state)}</span>
        <span>{reply.authorName ?? "موظف"}</span>
        <span dir="ltr">{formatRiyadhDateTime(reply.created_at)}</span>
      </p>
      <p className="mt-1 whitespace-pre-wrap break-words">{reply.body}</p>
      {state === "queued" && (
        <p className={HINT}>{retryable ? QUEUED_HINT : ENDED_BEFORE_SEND_HINT}</p>
      )}
      {hint && <p className={HINT}>{hint}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {state === "queued" && retryable && (
          <button type="button" onClick={onRetry} disabled={pending} className={BUTTON_PRIMARY}>
            إعادة المحاولة
          </button>
        )}
        {state === "failed" && canRewrite && (
          <button type="button" onClick={onRewrite} disabled={pending} className={BUTTON_SECONDARY}>
            إعادة كتابته
          </button>
        )}
      </div>
    </li>
  );
}

// The staff member's reply box on a customer's page (staff notification
// step 3, PR B; owner decisions 2026-10-02). Only the holder of the active
// takeover sees it -- an admin who needs to reply ends the takeover and
// takes it over -- and the database decides in the end (staff_queue_reply).
// Free text is disabled once the customer's last message is 24 hours old,
// as the agent also refuses it; the re-engagement template that replaces it
// is PR C, so its button is shown but disabled. A failed or lost send is
// never repeated blindly (the message may have arrived): only a reply the
// agent never claimed is retried; any other is written again.
export function ReplyBox({
  conversationId,
  takeoverId,
  holderId,
  currentUserId,
  lastInboundAt,
  replies,
  loadFailed,
  now,
}: {
  conversationId: number;
  takeoverId: number | null;
  holderId: string | null;
  currentUserId: string;
  lastInboundAt: string | null;
  replies: BoxReply[];
  loadFailed: boolean;
  now: string;
}) {
  const [pending, startTransition] = useTransition();
  const [draft, setDraft] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (takeoverId === null || holderId !== currentUserId) {
    return null;
  }

  const current = new Date(now);
  const windowOpen = isWithinCustomerServiceWindow(lastInboundAt, current);
  const length = [...draft].length;

  const run = (action: () => Promise<void>) => {
    setStatus(null);
    setError(null);
    startTransition(action);
  };

  const show = (result: Awaited<ReturnType<typeof sendStaffReply>>) => {
    if ("error" in result) {
      setError(result.error);
    } else if (result.status === "sent") {
      setStatus(sendResultLabel(result.status));
    } else {
      setError(sendResultLabel(result.status));
    }
  };

  const send = () => {
    const problem = replyDraftProblem(draft);
    if (problem !== null) {
      setError(problem);
      return;
    }
    run(async () => {
      const result = await sendStaffReply(conversationId, draft);
      // A stored reply shows in the list below with its own state, so the
      // box is cleared; a refusal before it was stored keeps the text.
      if (!("error" in result)) {
        setDraft("");
      }
      show(result);
    });
  };

  const retry = (replyId: number) => run(async () => show(await retryStaffReply(replyId)));

  return (
    <div className="mt-4 grid gap-3 border-t border-border pt-4">
      <h3 className="text-sm font-semibold">الرد على العميل</h3>
      {!windowOpen && (
        <p className={HINT} role="note">
          {OUTSIDE_WINDOW_NOTICE}
        </p>
      )}
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        disabled={pending || !windowOpen}
        rows={4}
        dir="auto"
        aria-label="نص الرد"
        placeholder={windowOpen ? "اكتب ردك للعميل" : "الرد الحر غير متاح خارج نافذة 24 ساعة"}
        className={`${INPUT} w-full`}
      />
      <p className={HINT}>
        يُرسل الرد للعميل باسمك على واتساب. {length} / {MAX_REPLY_LENGTH}
      </p>
      {status && (
        <p role="status" className={ALERT_STATUS}>
          {status}
        </p>
      )}
      {error && (
        <p role="alert" className={ALERT_ERROR}>
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={send}
          disabled={pending || !windowOpen}
          className={BUTTON_PRIMARY}
        >
          إرسال الرد
        </button>
        {!windowOpen && (
          <>
            <button type="button" disabled title={TEMPLATE_UNAVAILABLE} className={BUTTON_SECONDARY}>
              إرسال قالب إعادة التواصل
            </button>
            <span className={HINT}>{TEMPLATE_UNAVAILABLE}</span>
          </>
        )}
      </div>
      {loadFailed && <p className={HINT}>تعذّر تحميل ردودك السابقة. أعد تحميل الصفحة.</p>}
      {replies.length > 0 && (
        <ol className="grid gap-2" aria-label="الردود المرسلة من اللوحة">
          {replies.map((reply) => (
            <ReplyEntry
              key={reply.id}
              reply={reply}
              retryable={reply.takeover_id === takeoverId && reply.sent_by === currentUserId}
              canRewrite={windowOpen}
              pending={pending}
              now={current}
              onRetry={() => retry(reply.id)}
              onRewrite={() => setDraft(reply.body)}
            />
          ))}
        </ol>
      )}
    </div>
  );
}
