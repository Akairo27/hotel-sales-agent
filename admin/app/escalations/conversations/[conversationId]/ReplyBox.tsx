"use client";

import { type KeyboardEvent, useState, useTransition } from "react";
import { formatRiyadhDateTime, formatTimeLeft } from "@/lib/escalations";
import {
  MAX_REPLY_LENGTH,
  WINDOW_LOW_MS,
  replyDraftProblem,
  replyProblemHint,
  replyState,
  replyStateLabel,
  sendResultLabel,
  templateButtonProblem,
  windowRemainingMs,
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
import { retryStaffReply, sendReengagementTemplate, sendStaffReply } from "../../replyActions";
import { useWorkspace } from "./ChatWorkspace";

export interface BoxReply extends StaffReplyRow {
  /** Null when the author's name could not be read. */
  authorName: string | null;
}

const OUTSIDE_WINDOW_NOTICE =
  "مرّ أكثر من 24 ساعة على آخر رسالة من العميل، فلا يقبل واتساب رداً حراً. " +
  "تواصل معه هاتفياً، أو استخدم قالب إعادة التواصل حين يتوفر.";

const QUEUED_HINT = "لم يُرسل هذا الرد بعد. يمكنك إعادة المحاولة.";

const TEMPLATE_CONFIRMATION =
  "إرسال قالب إعادة التواصل الجاهز للعميل؟ لن يصله إلا هذا القالب، وحين يرد تفتح نافذة الرد الحر.";

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
      {state === "queued" && retryable && <p className={HINT}>{QUEUED_HINT}</p>}
      {hint && <p className={HINT}>{hint}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {state === "queued" && retryable && (
          <button type="button" onClick={onRetry} disabled={pending} className={BUTTON_PRIMARY}>
            إعادة المحاولة
          </button>
        )}
        {state === "failed" && canRewrite && reply.kind === "text" && (
          <button type="button" onClick={onRewrite} disabled={pending} className={BUTTON_SECONDARY}>
            إعادة كتابته
          </button>
        )}
      </div>
    </li>
  );
}

// The staff member's reply box, docked under the conversation (staff
// notification step 3, PR B; owner decisions 2026-10-02; chat layout the
// same day). Only the holder of the active takeover sees it -- an admin who
// needs to reply ends the takeover and takes it over -- and the database
// decides in the end (staff_queue_reply). Free text is disabled once the
// customer's last message is 24 hours old, as the agent also refuses it; the
// indicator above the box counts the window down, and the re-engagement
// template that replaces it is PR C, so its button is shown but disabled. A
// failed or lost send is never repeated blindly (the message may have
// arrived): only a reply the agent never claimed is retried; any other is
// written again. Replies sent fine are in the conversation as staff
// messages, so the box lists only those that are not.
export function ReplyBox({
  conversationId,
  takeoverId,
  holderId,
  currentUserId,
  replies,
  loadFailed,
  templateEnabled,
}: {
  conversationId: number;
  takeoverId: number | null;
  holderId: string | null;
  currentUserId: string;
  replies: BoxReply[];
  loadFailed: boolean;
  /** Whether the agent can send the re-engagement template (agent.env). */
  templateEnabled: boolean;
}) {
  const { now, windowOpen, lastInboundAt, draft, setDraft, replyInput } = useWorkspace();
  const [pending, startTransition] = useTransition();
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (takeoverId === null || holderId !== currentUserId) {
    return null;
  }

  const length = [...draft].length;
  const thisTakeover = replies.filter((reply) => reply.takeover_id === takeoverId);
  const unsent = thisTakeover.filter((reply) => replyState(reply) !== "sent");
  const remainingMs = windowRemainingMs(lastInboundAt, now);
  const templateProblem = templateButtonProblem(windowOpen, templateEnabled);

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
    if (pending || !windowOpen) {
      return;
    }
    const problem = replyDraftProblem(draft);
    if (problem !== null) {
      setError(problem);
      return;
    }
    run(async () => {
      const result = await sendStaffReply(conversationId, draft);
      // A stored reply shows in the conversation or the list below with its
      // own state, so the box is cleared; a refusal before it was stored
      // keeps the text.
      if (!("error" in result)) {
        setDraft("");
      }
      show(result);
    });
  };

  const sendTemplate = () => {
    if (pending || !window.confirm(TEMPLATE_CONFIRMATION)) {
      return;
    }
    run(async () => show(await sendReengagementTemplate(conversationId)));
  };

  const sendOnShortcut = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // isComposing: Enter that confirms an input-method candidate is not a send.
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };

  const retry = (replyId: number) => run(async () => show(await retryStaffReply(replyId)));

  return (
    <div className="shrink-0 border-t border-border bg-surface px-3 py-3 sm:px-4">
      {windowOpen ? (
        <p className={`${remainingMs < WINDOW_LOW_MS ? "text-danger" : "text-muted-foreground"} mb-2 text-xs`}>
          نافذة الرد الحر مفتوحة · {formatTimeLeft(remainingMs)}
        </p>
      ) : (
        <p className="mb-2 text-xs text-danger" role="note">
          {OUTSIDE_WINDOW_NOTICE}
        </p>
      )}
      {status && (
        <p role="status" className={`${ALERT_STATUS} mb-2`}>
          {status}
        </p>
      )}
      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-2`}>
          {error}
        </p>
      )}
      {loadFailed && <p className={`${HINT} mb-2`}>تعذّر تحميل ردودك السابقة. أعد تحميل الصفحة.</p>}
      {unsent.length > 0 && (
        <ol className="mb-2 grid max-h-44 gap-2 overflow-y-auto" aria-label="ردود لم تُرسل">
          {unsent.map((reply) => (
            <ReplyEntry
              key={reply.id}
              reply={reply}
              retryable={reply.sent_by === currentUserId}
              canRewrite={windowOpen}
              pending={pending}
              now={now}
              onRetry={() => retry(reply.id)}
              onRewrite={() => setDraft(reply.body)}
            />
          ))}
        </ol>
      )}
      <textarea
        ref={replyInput}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={sendOnShortcut}
        disabled={pending || !windowOpen}
        rows={2}
        dir="auto"
        aria-label="نص الرد"
        placeholder={windowOpen ? "اكتب ردك للعميل" : "الرد الحر غير متاح خارج نافذة 24 ساعة"}
        className={`${INPUT} w-full`}
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
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
            <button
              type="button"
              onClick={sendTemplate}
              disabled={pending || templateProblem !== null}
              title={templateProblem ?? undefined}
              className={BUTTON_SECONDARY}
            >
              إرسال قالب إعادة التواصل
            </button>
            {templateProblem !== null && <span className={HINT}>{templateProblem}</span>}
          </>
        )}
        <span className={`${HINT} ms-auto text-xs`}>
          <span className="hidden sm:inline">Ctrl/⌘ + Enter للإرسال · </span>
          {length} / {MAX_REPLY_LENGTH}
        </span>
      </div>
    </div>
  );
}
