"use client";

import { useState } from "react";
import type { Season } from "@/lib/types";
import { SeasonList } from "./SeasonList";
import { CalendarPreview } from "./CalendarPreview";
import { reorderSeasons } from "./actions";

interface SeasonsWorkspaceProps {
  initialSeasons: Season[];
  isAdmin: boolean;
}

// Holds the one live "seasons" array that both the editor list and the
// calendar preview read from, so an edit or a drag-reorder shows up in the
// preview instantly (before the Server Action round-trip completes) —
// the "يتحدث فوراً مع كل تعديل" requirement the calendar design was built
// around.
export function SeasonsWorkspace({ initialSeasons, isAdmin }: SeasonsWorkspaceProps) {
  const [seasons, setSeasons] = useState<Season[]>(initialSeasons);
  const [reorderError, setReorderError] = useState<string | null>(null);

  // Resets local draft state when the server delivers a genuinely new
  // `initialSeasons` snapshot (a fresh page render after a save, or a
  // revalidated payload after reorderSeasons). Adjusting state during
  // render — not in a useEffect — per React's own guidance for "reset
  // state when a prop changes": it avoids an extra commit-then-effect
  // render pass.
  const [prevInitialSeasons, setPrevInitialSeasons] = useState(initialSeasons);
  if (initialSeasons !== prevInitialSeasons) {
    setPrevInitialSeasons(initialSeasons);
    setSeasons(initialSeasons);
  }

  function handleSeasonChange(updated: Season): void {
    setSeasons((prev) => prev.map((season) => (season.id === updated.id ? updated : season)));
  }

  function handleReorder(orderedSeasonIds: number[]): void {
    setReorderError(null);
    const highestPriority = orderedSeasonIds.length - 1;
    setSeasons((prev) =>
      prev.map((season) => {
        const index = orderedSeasonIds.indexOf(season.id);
        return index === -1 ? season : { ...season, priority: highestPriority - index };
      })
    );
    void reorderSeasons(orderedSeasonIds).then((result) => {
      if (result.error) {
        setReorderError(result.error);
      }
    });
  }

  return (
    <div className="flex flex-wrap gap-8">
      <div className="min-w-80 flex-1">
        <SeasonList
          seasons={seasons}
          isAdmin={isAdmin}
          onSeasonChange={handleSeasonChange}
          onReorder={handleReorder}
          reorderError={reorderError}
        />
      </div>
      <div className="min-w-[30rem] flex-[2]">
        <CalendarPreview seasons={seasons} />
      </div>
    </div>
  );
}
