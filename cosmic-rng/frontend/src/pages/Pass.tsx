import { post } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { useGame } from "../store/game";
import { Empty, ErrorBox, Spinner } from "../components/ui";
import { fmtInt } from "../lib/format";

type Tier = { idx: number; points: number; rewards: any; summary: string; reached: boolean; claimed: boolean };
type PassData = { season: { id: number; name: string } | null; points: number; tiers: Tier[] };

export function Pass() {
  const { data, loading, error, reload } = useApi<PassData>("/api/season/pass");
  const { busy, run } = useAction();
  const refreshHud = useGame((s) => s.refreshHud);

  const claim = (index: number) =>
    run(() => post("/api/season/pass/claim", { tier: index }, true), { success: "報酬を受け取りました" })
      .then(() => { reload(); refreshHud(); });

  if (loading && !data) return <Spinner label="パスを読み込み中…" />;
  if (!data?.season) return <div className="page"><Empty icon="🎟">進行中のシーズンがありません</Empty></div>;

  const next = data.tiers.find((t) => !t.reached);
  const claimable = data.tiers.filter((t) => t.reached && !t.claimed).length;

  return (
    <div className="page">
      <div className="page-head">
        <h1>シーズンパス</h1>
        <span className="sub">{data.season.name} · 無料で全段解放</span>
      </div>
      <ErrorBox error={error} onRetry={reload} />

      <div className="glass pad col" style={{ marginBottom: 12 }}>
        <div className="row-wrap">
          <div className="stat"><span className="k">シーズンポイント</span><span className="v mono">{fmtInt(data.points)}</span></div>
          <span className="spacer" />
          {claimable > 0 && <span className="badge" style={{ color: "var(--gold)" }}>受け取り可能 {claimable}</span>}
          {next && <span className="small muted">次の段まで あと {fmtInt(next.points - data.points)} pt</span>}
        </div>
        <div className="bar">
          <i style={{ width: `${Math.min(100, (data.points / Math.max(1, data.tiers[data.tiers.length - 1].points)) * 100)}%` }} />
        </div>
      </div>

      <div className="pass-track">
        {data.tiers.map((t) => (
          <div key={t.idx} className={`pass-tier ${t.reached ? "reached" : ""} ${t.claimed ? "claimed" : ""}`}>
            <div className="row">
              <span className="pass-n mono">{t.idx + 1}</span>
              <div className="col" style={{ flex: 1, gap: 2, minWidth: 0 }}>
                <span className="tiny faint mono">{fmtInt(t.points)} pt</span>
                <span className="small">{t.summary}</span>
              </div>
              {t.claimed ? (
                <span className="badge" style={{ color: "var(--good)" }}>受取済</span>
              ) : t.reached ? (
                <button className="btn xs primary" disabled={busy} onClick={() => claim(t.idx)}>受け取る</button>
              ) : (
                <span className="tiny faint">未到達</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
