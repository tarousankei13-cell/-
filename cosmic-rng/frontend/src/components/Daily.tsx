/** Login bonus: a small card that only appears on the day it can be claimed. */
import { useEffect, useState } from "react";
import { post } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { useGame } from "../store/game";
import { fmtInt } from "../lib/format";

type Daily = {
  available: boolean; streak: number; next_streak: number; cycle_day: number;
  reward: { stardust?: number; shards?: number; boosts?: { key: string; qty: number }[] };
  days: any[];
};

export function DailyCard() {
  const { data, reload } = useApi<Daily>("/api/daily");
  const { busy, run } = useAction();
  const refreshHud = useGame((s) => s.refreshHud);
  const toast = useGame((s) => s.toast);
  const [claimed, setClaimed] = useState(false);

  useEffect(() => { if (data?.available) setClaimed(false); }, [data?.available]);

  if (!data || (!data.available && !claimed)) return null;

  const claim = async () => {
    const res = await run<any>(() => post("/api/daily/claim", {}, true));
    if (!res) return;
    setClaimed(true);
    toast(`ログインボーナス ${res.streak}日目`, "cosmic",
      `✦${fmtInt(res.granted?.stardust ?? 0)}${res.granted?.shards ? ` · 星の欠片 ×${res.granted.shards}` : ""}`);
    reload();
    refreshHud();
  };

  return (
    <div className="glass pad daily-card">
      <div className="row-wrap" style={{ gap: 10 }}>
        <span className="daily-icon" aria-hidden>🎁</span>
        <div className="col" style={{ flex: 1, gap: 2, minWidth: 0 }}>
          <strong>{claimed ? "受け取りました" : "ログインボーナス"}</strong>
          <span className="tiny faint">
            {claimed ? `${data.streak}日連続` : `${data.next_streak}日目 · ✦${fmtInt(data.reward?.stardust ?? 0)}`}
            {!claimed && data.reward?.shards ? ` · 星の欠片 ×${data.reward.shards}` : ""}
          </span>
        </div>
        {!claimed && <button className="btn primary sm" disabled={busy} onClick={claim}>受け取る</button>}
      </div>
      <div className="daily-dots" aria-hidden>
        {(data.days ?? []).map((_, i) => (
          <span key={i} className={`dot7 ${i < ((claimed ? data.streak : data.next_streak - 1) % 7) ? "on" : ""} ${i === 6 ? "big" : ""}`} />
        ))}
      </div>
    </div>
  );
}
