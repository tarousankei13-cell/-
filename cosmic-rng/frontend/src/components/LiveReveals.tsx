/** Rare finds from other players, sliding in at the edge of the screen. */
import { useEffect } from "react";
import { useGame } from "../store/game";
import { ItemIcon } from "./ItemIcon";
import { fmtOdds } from "../lib/format";

export function LiveReveals() {
  const live = useGame((s) => s.live);
  const shift = useGame((s) => s.shiftLive);
  const first = live[0];

  useEffect(() => {
    if (!first) return;
    const t = window.setTimeout(shift, 6500);
    return () => window.clearTimeout(t);
  }, [first, shift]);

  if (!live.length) return null;
  return (
    <div className="live-stack" aria-live="polite">
      {live.map((r, i) => (
        <div key={`${r.user?.id}-${i}`} className={`live-card t${r.item?.tier ?? 5}`} onClick={shift}>
          <ItemIcon visual={r.item?.visual} tier={r.item?.tier} size={34} />
          <div className="col" style={{ gap: 0, minWidth: 0 }}>
            <span className="tiny faint ellipsis">{r.user?.name} が発見</span>
            <span className={`ellipsis r-${r.item?.rarity}`}>{r.item?.name_ja || r.item?.name}</span>
            <span className="tiny mono faint">{fmtOdds(r.odds)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}
