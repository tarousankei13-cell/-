import { useEffect, useState } from "react";
import { useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { Empty, ErrorBox, Spinner, Tabs, UserChip } from "../components/ui";
import { fmtCompact, fmtDate, fmtInt, fmtOdds } from "../lib/format";

const BOARDS = [
  { key: "rolls", label: "Total Rolls" },
  { key: "best", label: "Best Rarity" },
  { key: "collection", label: "Collection" },
  { key: "achievements", label: "Achievements" },
  { key: "wealth", label: "Wealth" },
  { key: "firsts", label: "First Discoveries" },
  { key: "season", label: "Season" },
];

const SEASON_METRICS = [
  { key: "points", label: "Points" },
  { key: "rolls", label: "Rolls" },
  { key: "best", label: "Best" },
  { key: "firsts", label: "Firsts" },
];

export function Ranking() {
  const [board, setBoard] = useState("rolls");
  const [metric, setMetric] = useState("points");
  const [tick, setTick] = useState(0);
  const live = useGame((s) => s.liveRankings);
  const { data, loading, error, reload } = useApi<any>(`/api/rankings/${board}`, board === "season" ? { metric } : undefined, [tick]);

  // live push refreshes the visible board
  useEffect(() => {
    if (board !== "season" && live[board]) setTick((t) => t + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live[board]]);

  const fmtValue = (v: number) => {
    if (board === "best") return fmtOdds(v);
    if (board === "collection") return `${(v)} 種類`;
    if (board === "wealth") return `✦ ${fmtCompact(v)}`;
    return fmtCompact(v);
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Ranking</h1>
          <div className="sub">リアルタイム更新。シーズン終了時の順位は永久保存されます。</div>
        </div>
      </div>

      <Tabs value={board} onChange={setBoard} tabs={BOARDS} />
      {board === "season" && (
        <div style={{ marginTop: 8 }}>
          <Tabs value={metric} onChange={setMetric} tabs={SEASON_METRICS} />
        </div>
      )}

      <ErrorBox error={error} onRetry={reload} />
      <div className="glass pad" style={{ marginTop: 12 }}>
        {data?.season && (
          <div className="row" style={{ marginBottom: 10 }}>
            <div style={{ flex: 1 }}>
              <strong>{data.season.name}</strong>
              <div className="tiny muted">{fmtDate(data.season.starts_at)} 〜 {fmtDate(data.season.ends_at)}</div>
            </div>
            <span className="chip tiny">{data.final ? "確定" : data.season.status}</span>
          </div>
        )}
        {data?.me && (
          <div className="row" style={{ marginBottom: 10, padding: "7px 10px", borderRadius: 9, background: "rgba(120,160,255,0.12)", border: "1px solid var(--line-strong)" }}>
            <span className="mono" style={{ width: 52 }}>#{fmtInt(data.me.rank)}</span>
            <span style={{ flex: 1 }}>あなた</span>
            <span className="mono">{fmtValue(data.me.value)}</span>
          </div>
        )}
        {loading && !data ? <Spinner /> : !data?.entries?.length ? <Empty icon="📊">まだ記録がありません</Empty> : (
          <div className="col" style={{ gap: 3 }}>
            {data.entries.map((e: any) => (
              <div key={`${e.rank}-${e.user.id}`} className="row" style={{
                gap: 10, padding: "6px 9px", borderRadius: 9,
                background: e.rank <= 3 ? "rgba(255,210,120,0.09)" : "rgba(255,255,255,0.03)",
                border: e.rank <= 3 ? "1px solid rgba(255,210,120,0.28)" : "1px solid transparent",
              }}>
                <span className="mono display" style={{ width: 46, color: e.rank === 1 ? "var(--gold)" : e.rank <= 3 ? "var(--text)" : "var(--text-faint)", fontWeight: 700 }}>
                  {e.rank === 1 ? "👑" : `#${e.rank}`}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <UserChip user={e.user} size={22} />
                  {e.item && <div className="tiny"><span className={`r-${e.item.rarity}`}>{e.item.name}</span></div>}
                </div>
                <span className="mono" style={{ fontWeight: 600 }}>{fmtValue(e.value)}</span>
                {e.rate !== undefined && <span className="tiny faint">{(e.rate * 100).toFixed(1)}%</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
