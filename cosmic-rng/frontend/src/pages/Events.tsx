import { useEffect } from "react";
import { events as bus } from "../store/game";
import { useApi } from "../lib/useApi";
import { Countdown, Empty, ErrorBox, Spinner } from "../components/ui";
import { fmtInt } from "../lib/format";

export type GameEventInfo = {
  key: string; name: string; description: string; type: string;
  starts_at: string | null; ends_at: string | null;
  mult?: number;
  progress?: number; target?: number; completed?: boolean; reward_mult?: number; reward_hours?: number;
};
export type EventsData = { active: GameEventInfo[]; upcoming: GameEventInfo[]; server_time: string };

export function CommunityBar({ e, compact }: { e: GameEventInfo; compact?: boolean }) {
  const pct = Math.min(100, ((e.progress ?? 0) / Math.max(1, e.target ?? 1)) * 100);
  return (
    <div className="col" style={{ gap: 5 }}>
      <div className="row tiny">
        <span className="badge" style={{ color: e.completed ? "var(--good)" : "var(--accent2)" }}>
          {e.completed ? "達成" : "世界目標"}
        </span>
        <span className="ellipsis" style={{ flex: 1 }}>{e.name}</span>
        <span className="mono">{fmtInt(e.progress ?? 0)} / {fmtInt(e.target ?? 0)}</span>
      </div>
      <div className="bar"><i style={{ width: `${pct}%` }} /></div>
      {!compact && (
        <div className="tiny faint">
          {e.completed
            ? `達成しました。全員に ${e.reward_hours}時間 Luck ×${e.reward_mult} が付与されています。`
            : `達成すると全員が ${e.reward_hours}時間 Luck ×${e.reward_mult}。みんなのRollが進捗になります。`}
        </div>
      )}
    </div>
  );
}

function EventCard({ e, upcoming }: { e: GameEventInfo; upcoming?: boolean }) {
  return (
    <div className={`glass pad col event-card ${e.type}`} style={{ gap: 8 }}>
      <div className="row-wrap" style={{ gap: 8 }}>
        <h3 style={{ flex: 1, minWidth: 0 }} className="ellipsis">{e.name}</h3>
        {e.type === "luck_multiplier" && e.mult && <span className="badge" style={{ color: "var(--gold)" }}>Luck ×{e.mult}</span>}
        {upcoming && <span className="badge" style={{ color: "var(--text-faint)" }}>開催予定</span>}
      </div>
      {e.description && <div className="muted small">{e.description}</div>}
      {e.type === "community_goal" && <CommunityBar e={e} />}
      <div className="row tiny faint" style={{ gap: 10, flexWrap: "wrap" }}>
        {upcoming
          ? <span>開始まで <Countdown to={e.starts_at} /></span>
          : e.ends_at && <span>のこり <Countdown to={e.ends_at} /></span>}
      </div>
    </div>
  );
}

export function Events() {
  const { data, loading, error, reload } = useApi<EventsData>("/api/events");
  useEffect(() => bus.on("game_event", () => reload()), [reload]);

  if (loading && !data) return <Spinner label="イベントを読み込み中…" />;
  const nothing = !data?.active.length && !data?.upcoming.length;

  return (
    <div className="page">
      <div className="page-head">
        <h1>イベント</h1>
        <span className="sub">開催中の補正と、みんなで進める目標</span>
      </div>
      <ErrorBox error={error} onRetry={reload} />
      {nothing ? (
        <Empty icon="🎏">いまは開催中のイベントがありません</Empty>
      ) : (
        <div className="grid grid-2">
          {data!.active.map((e) => <EventCard key={e.key} e={e} />)}
          {data!.upcoming.map((e) => <EventCard key={e.key} e={e} upcoming />)}
        </div>
      )}
    </div>
  );
}
