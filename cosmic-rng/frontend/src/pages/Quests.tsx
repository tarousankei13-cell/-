import { post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";
import { Countdown, Empty, ErrorBox, Spinner } from "../components/ui";
import { fmtInt } from "../lib/format";

interface Quest {
  id: number; key: string; name: string; kind: string; progress: number | null; target: number | null;
  status: string; rewards: any; chain_key: string | null; chain_step: number | null; chain_total?: number;
  description: string | null; hint?: string | null;
}
interface QuestData { daily: Quest[]; chains: Quest[]; hidden: Quest[]; reset_at: string; unlocked: boolean; unlock_level: number }

function Rewards({ r }: { r: any }) {
  if (!r) return null;
  return (
    <div className="row-wrap tiny" style={{ gap: 4 }}>
      {r.stardust > 0 && <span className="chip tiny" style={{ color: "var(--gold)" }}>✦ {fmtInt(r.stardust)}</span>}
      {r.xp > 0 && <span className="chip tiny">{fmtInt(r.xp)} XP</span>}
      {(r.boosts ?? []).map((b: any) => <span key={b.key} className="chip tiny" style={{ color: "var(--accent)" }}>{b.key} ×{b.qty}</span>)}
      {(r.cosmetics ?? []).map((c: string) => <span key={c} className="chip tiny" style={{ color: "var(--r-epic)" }}>{c}</span>)}
      {(r.items ?? []).map((i: any) => <span key={i.key} className="chip tiny">{i.key} ×{i.qty}</span>)}
    </div>
  );
}

function QuestRow({ q, onClaim, busy }: { q: Quest; onClaim: (q: Quest) => void; busy: boolean }) {
  const pct = q.target ? Math.min(100, ((q.progress ?? 0) / q.target) * 100) : 0;
  const done = q.status === "completed";
  const claimed = q.status === "claimed";
  return (
    <div className="glass-2 pad-sm col" style={{ gap: 7, borderColor: done ? "rgba(77,255,184,0.5)" : undefined }}>
      <div className="row" style={{ gap: 8 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 6 }}>
            <strong>{q.name}</strong>
            {q.chain_step && <span className="chip tiny">{q.chain_step}/{q.chain_total ?? "?"}</span>}
            {q.kind === "hidden" && <span className="chip tiny" style={{ color: "var(--gold)" }}>隠し</span>}
          </div>
          <div className="tiny muted">{q.description ?? (q.hint ? `💡 ${q.hint}` : "条件は不明…")}</div>
        </div>
        {done && <button className="btn sm gold" disabled={busy} onClick={() => onClaim(q)}>受け取る</button>}
        {claimed && <span className="chip tiny" style={{ color: "var(--good)" }}>受取済</span>}
      </div>
      {q.target !== null && (
        <>
          <div className="bar"><i style={{ width: `${pct}%`, background: done ? "linear-gradient(90deg, var(--good), var(--accent))" : undefined }} /></div>
          <div className="row tiny mono muted"><span style={{ flex: 1 }}>{fmtInt(q.progress ?? 0)} / {fmtInt(q.target)}</span></div>
        </>
      )}
      <Rewards r={q.rewards} />
    </div>
  );
}

export function Quests() {
  const { data, loading, error, reload } = useApi<QuestData>("/api/quests");
  const { run, busy } = useAction();
  const refreshHud = useGame((s) => s.refreshHud);
  const toast = useGame((s) => s.toast);

  const claim = async (q: Quest) => {
    const res = await run(() => post<{ granted: any; progress: any }>("/api/quests/claim", { user_quest_id: q.id }, true));
    if (res) {
      audio.sfx("quest");
      const g = res.granted;
      toast(`${q.name} の報酬を受け取りました`, "success",
        [g.stardust ? `✦ ${fmtInt(g.stardust)}` : "", g.xp ? `${fmtInt(g.xp)} XP` : ""].filter(Boolean).join(" / "));
      useGame.getState().announceAchievements(res.progress?.achievements ?? []);
      reload();
      refreshHud();
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>依頼<span className="h1-en">Quest</span></h1>
          {data && <div className="sub">デイリーは <Countdown to={data.reset_at} /> 後にリセット</div>}
        </div>
      </div>

      <ErrorBox error={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : !data ? null : (
        <div className="col" style={{ gap: 16 }}>
          <section className="glass pad col" style={{ gap: 10 }}>
            <h2>Daily</h2>
            {!data.unlocked ? (
              <div className="muted small">🔒 Lv.{data.unlock_level} でデイリークエストが解放されます</div>
            ) : data.daily.length === 0 ? (
              <div className="muted small">本日のクエストはありません</div>
            ) : (
              <div className="grid grid-2">{data.daily.map((q) => <QuestRow key={q.id} q={q} onClaim={claim} busy={busy} />)}</div>
            )}
          </section>

          <section className="glass pad col" style={{ gap: 10 }}>
            <h2>Chain</h2>
            {data.chains.length === 0 ? <div className="muted small">進行中のチェーンはありません</div> : (
              <div className="grid grid-2">{data.chains.map((q) => <QuestRow key={q.id} q={q} onClaim={claim} busy={busy} />)}</div>
            )}
          </section>

          <section className="glass pad col" style={{ gap: 10 }}>
            <h2>Hidden</h2>
            <div className="tiny muted">条件は明かされていません。ヒントを頼りに探してください。</div>
            {data.hidden.length === 0 ? <Empty icon="🔍">まだありません</Empty> : (
              <div className="grid grid-2">{data.hidden.map((q) => <QuestRow key={q.id} q={q} onClaim={claim} busy={busy} />)}</div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
