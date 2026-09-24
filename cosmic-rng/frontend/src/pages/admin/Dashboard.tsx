import { useEffect, useState } from "react";
import { useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { Spinner } from "../../components/ui";
import { fmtCompact, fmtDate, fmtInt } from "../../lib/format";

function Spark({ data, max }: { data: { t: string; n: number }[]; max?: number }) {
  const peak = Math.max(1, max ?? Math.max(...data.map((d) => d.n), 1));
  return (
    <div className="spark" title={`最大 ${peak}`}>
      {data.slice(-60).map((d, i) => (
        <i key={i} className={d.n === 0 ? "spark-zero" : undefined}
           style={{ height: `${Math.max(2, (d.n / peak) * 100)}%` }} title={`${d.t}: ${d.n}`} />
      ))}
    </div>
  );
}

export function AdminDashboard() {
  const { data, loading, reload } = useApi<any>("/api/admin/dashboard");
  const live = useGame((s) => s.adminStats);
  const [d, setD] = useState<any>(null);

  useEffect(() => {
    if (data) setD(data);
  }, [data]);
  useEffect(() => {
    if (live) setD((cur: any) => ({ ...(cur ?? {}), ...live }));
  }, [live]);
  useEffect(() => {
    const id = window.setInterval(() => reload(true), 20000);
    return () => window.clearInterval(id);
  }, [reload]);

  if (loading && !d) return <Spinner />;
  if (!d) return null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="kpi-grid">
        <div className="kpi good"><span className="k">Online</span><span className="v">{fmtInt(d.online)}</span><span className="tiny faint">/ {fmtInt(d.users)} users</span></div>
        <div className="kpi"><span className="k">Rolls / sec</span><span className="v">{d.rolls_per_sec?.toFixed(2)}</span><span className="tiny faint">worker {d.worker_rolls_per_sec?.toFixed(2)}</span></div>
        <div className="kpi"><span className="k">総Roll数</span><span className="v">{fmtCompact(d.total_rolls)}</span></div>
        <div className="kpi"><span className="k">Rare Drops 24h</span><span className="v">{fmtInt(d.rare_drops_24h)}</span></div>
        <div className="kpi"><span className="k">世界初発見</span><span className="v">{fmtInt(d.first_discoveries_24h)}</span><span className="tiny faint">24h</span></div>
        <div className="kpi"><span className="k">Market 24h</span><span className="v">{fmtInt(d.market_24h?.count)}</span><span className="tiny faint">✦{fmtCompact(d.market_24h?.volume)}</span></div>
        <div className="kpi"><span className="k">Trades 24h</span><span className="v">{fmtInt(d.trades_24h)}</span></div>
        <div className="kpi"><span className="k">Active Boosts</span><span className="v">{fmtInt(d.active_boosts)}</span></div>
        <div className={`kpi ${d.errors_24h > 0 ? "alert" : ""}`}><span className="k">Errors 24h</span><span className="v">{fmtInt(d.errors_24h)}</span></div>
        <div className={`kpi ${d.flagged_listings_24h > 0 ? "alert" : ""}`}><span className="k">Flagged 24h</span><span className="v">{fmtInt(d.flagged_listings_24h)}</span></div>
        <div className="kpi"><span className="k">Content ver</span><span className="v">{d.content_version}</span></div>
      </div>

      <div className="grid grid-2">
        <div className="glass pad col">
          <h3>Rolls / minute（直近60分）</h3>
          {d.rolls_per_minute?.length ? <Spark data={d.rolls_per_minute} /> : <div className="muted small">データなし</div>}
        </div>
        <div className="glass pad col">
          <h3>Rare Drops / hour（24h）</h3>
          {d.rare_per_hour?.length ? <Spark data={d.rare_per_hour} /> : <div className="muted small">データなし</div>}
        </div>
      </div>

      <div className="grid grid-2">
        <div className="glass pad col">
          <h3>Biome分布（オンライン）</h3>
          {!d.biomes?.length ? <div className="muted small">データなし</div> : (
            <div className="col" style={{ gap: 4 }}>
              {d.biomes.sort((a: any, b: any) => b.count - a.count).map((b: any) => {
                const pct = (b.count / Math.max(1, d.online)) * 100;
                return (
                  <div key={b.key} className="col" style={{ gap: 2 }}>
                    <div className="row tiny"><span style={{ flex: 1 }}>{b.name}</span><span className="mono">{b.count}</span></div>
                    <div className="bar"><i style={{ width: `${pct}%` }} /></div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <div className="glass pad col">
          <h3>レア度分布（直近1h）</h3>
          {!d.tier_distribution_1h?.length ? <div className="muted small">データなし</div> : (
            <div className="row-wrap">
              {d.tier_distribution_1h.sort((a: any, b: any) => a.tier - b.tier).map((t: any) => (
                <span key={t.tier} className="chip tiny">T{t.tier}: {fmtInt(t.n)}</span>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-2">
        <div className="glass pad col">
          <h3>管理者通知</h3>
          {!d.notifications?.length ? <div className="muted small">なし</div> : (
            <div className="col" style={{ gap: 4, maxHeight: 260, overflow: "auto" }}>
              {d.notifications.map((n: any) => (
                <div key={n.id} className="glass-2 pad-sm col" style={{ gap: 1 }}>
                  <div className="row"><strong className="small" style={{ flex: 1 }}>{n.title}</strong><span className="tiny faint">{fmtDate(n.created_at)}</span></div>
                  {n.body && <div className="tiny muted">{n.body}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="glass pad col">
          <h3>直近のエラー</h3>
          {!d.recent_errors?.length ? <div className="muted small">エラーはありません</div> : (
            <div className="col" style={{ gap: 4, maxHeight: 260, overflow: "auto" }}>
              {d.recent_errors.map((e: any) => (
                <div key={e.error_id} className="glass-2 pad-sm col" style={{ gap: 1, borderColor: "rgba(255,92,122,0.3)" }}>
                  <div className="row tiny"><span className="mono" style={{ flex: 1 }}>{e.path}</span><span className="faint">{fmtDate(e.created_at)}</span></div>
                  <div className="tiny" style={{ color: "var(--bad)" }}>{e.type}: {e.message}</div>
                  <div className="tiny faint mono">{e.error_id}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="tiny faint center">Server time: {fmtDate(d.server_time)} · 自動更新中</div>
    </div>
  );
}
