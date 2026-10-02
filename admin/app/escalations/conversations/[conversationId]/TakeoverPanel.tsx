"use client";

import { useState, useTransition } from "react";
import { formatAge } from "@/lib/escalations";
import {
  type NoticeState,
  canRetryNotice,
  noticeResultLabel,
  noticeStateLabel,
} from "@/lib/takeoverNotice";
import {
  ALERT_ERROR,
  ALERT_STATUS,
  BUTTON_PRIMARY,
  BUTTON_SECONDARY,
  HINT,
} from "@/lib/ui";
import {
  type CloseOutcome,
  closeConversation,
  sendTakeoverNotice,
  takeOverConversation,
} from "../../actions";

export interface PanelTakeover {
  id: number;
  holderId: string;
  holderName: string;
  takenOverAt: string;
  noticeState: NoticeState;
}

const CLOSE_CONFIRMATIONS: Record<CloseOutcome, string> = {
  resolved: "إغلاق كل تصعيدات هذا العميل المفتوحة؟",
  handed_back:
    "إعادة المحادثة للبوت وإغلاق تصعيداتها المفتوحة؟ سيرد البوت على رسالة العميل التالية.",
};

const CLOSE_DONE: Record<CloseOutcome, string> = {
  resolved: "أُغلقت تصعيدات العميل.",
  handed_back: "أُعيدت المحادثة للبوت وأُغلقت تصعيداتها.",
};

function statusText(
  takeover: PanelTakeover | null,
  openCount: number,
  currentUserId: string,
  now: Date,
): string {
  if (takeover) {
    const age = formatAge(takeover.takenOverAt, now);
    return takeover.holderId === currentUserId
      ? `استلمتها أنت ${age}. لا يرد البوت على العميل حتى تغلقها أو تعيدها للبوت.`
      : `استلمها ${takeover.holderName} ${age}. لا يرد البوت على العميل حتى تُغلق أو تُعاد للبوت.`;
  }
  return openCount > 0 ? "لم يستلم أحد هذه المحادثة بعد." : "كل تصعيدات هذا العميل مغلقة.";
}

// Take over, resolve and hand back for one customer (staff notification
// step 2a, owner decisions 2026-10-02). Which buttons show mirrors
// migration 0034's policies -- anyone active takes over; the holder or an
// admin ends a takeover; resolve also closes escalations nobody holds --
// but the database decides; a refusal comes back as a message.
export function TakeoverPanel({
  conversationId,
  openCount,
  takeover,
  currentUserId,
  isAdmin,
  now,
  loadFailed,
}: {
  conversationId: number;
  openCount: number;
  takeover: PanelTakeover | null;
  currentUserId: string;
  isAdmin: boolean;
  now: string;
  loadFailed: boolean;
}) {
  const [pending, startTransition] = useTransition();
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (loadFailed) {
    return <p className={HINT}>تعذّر تحميل حالة الاستلام. أعد تحميل الصفحة.</p>;
  }

  const mayEnd = takeover !== null && (takeover.holderId === currentUserId || isAdmin);
  // Nothing was claimed: the agent could not be reached when it was taken
  // over, so the notice can be asked for again.
  const noticeRetryId = mayEnd && takeover?.noticeState === "not_sent" ? takeover.id : null;

  const run = (action: () => Promise<void>) => {
    setStatus(null);
    setError(null);
    startTransition(action);
  };

  const takeOver = () =>
    run(async () => {
      const result = await takeOverConversation(conversationId);
      if (result.outcome === "won") {
        setStatus(`استلمت المحادثة. ${noticeResultLabel(result.notice)}`);
      } else if (result.outcome === "already_yours") {
        setStatus("المحادثة مستلمة باسمك بالفعل.");
      } else if (result.outcome === "lost") {
        setError(`سبقك إليها ${result.holderName}.`);
      } else {
        setError(result.message);
      }
    });

  const close = (closeOutcome: CloseOutcome) => {
    if (!window.confirm(CLOSE_CONFIRMATIONS[closeOutcome])) {
      return;
    }
    run(async () => {
      const result = await closeConversation(conversationId, closeOutcome);
      if ("error" in result) {
        setError(result.error);
      } else {
        setStatus(CLOSE_DONE[closeOutcome]);
      }
    });
  };

  const retryNotice = (takeoverId: number) =>
    run(async () => {
      const result = await sendTakeoverNotice(takeoverId);
      if ("error" in result) {
        setError(result.error);
        return;
      }
      if (canRetryNotice(result.notice)) {
        setError(noticeResultLabel(result.notice));
      } else {
        setStatus(noticeResultLabel(result.notice));
      }
    });

  return (
    <div className="mt-3 grid gap-3">
      <p className={HINT}>{statusText(takeover, openCount, currentUserId, new Date(now))}</p>
      {takeover && <p className={HINT}>{noticeStateLabel(takeover.noticeState)}</p>}
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
      <div className="flex flex-wrap gap-2">
        {!takeover && openCount > 0 && (
          <button type="button" onClick={takeOver} disabled={pending} className={BUTTON_PRIMARY}>
            استلام المحادثة
          </button>
        )}
        {noticeRetryId !== null && (
          <button
            type="button"
            onClick={() => retryNotice(noticeRetryId)}
            disabled={pending}
            className={BUTTON_PRIMARY}
          >
            إرسال إشعار الاستلام
          </button>
        )}
        {(mayEnd || (!takeover && openCount > 0)) && (
          <button
            type="button"
            onClick={() => close("resolved")}
            disabled={pending}
            className={BUTTON_SECONDARY}
          >
            إغلاق
          </button>
        )}
        {mayEnd && (
          <button
            type="button"
            onClick={() => close("handed_back")}
            disabled={pending}
            className={BUTTON_SECONDARY}
          >
            إعادة للبوت
          </button>
        )}
      </div>
    </div>
  );
}
