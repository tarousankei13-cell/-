/** Continuation metrics: are players coming back, and where do they stop? */
import { useState } from "react";
import { useApi } from "../../lib/useApi";
import { ErrorBox, Spinner } from "../../components/ui";
import { fmtInt } from "../../lib/format";

type Point = { date: string; count: number };
type Cohort = { date: string; new_users: number; d1: number | null; d7: number | null };
type Step = { step: string; count: number; pct: number };
type Data = { days: number; dau: Point[]; new_users: Point[]; active_users: Point[]; cohorts: Cohort[]; funnel: Step[] };

function Bars({ points, color }: { points: Point[]; color: string }) {
  const max = Math.max(1, ...points.map((p) => p.count));
  if (!points.length) return <div className="muted small">データがありません</div>;
  return (
    <div className="sparkline" style={{ height: 88 }}>
      {points.map((p) => (
        <div className="spark-col" key={p.date} title={`${p.date}: ${fmtInt(p.count)}`}>
          <i style={{ height: `${Math.max(2, (p.count / max) * 100)}%`, background: color }} />
        </div>
      ))}
    </div>
  );
}

export function AdminRetention() {
  const [days, setDays] = useState(14);
  const { data, loading, error, reload } = useApi<Data>("/api/admin/retention", { days }, [days]);
  if (loading && !data) return <Spinner label="継続率を集計中…" />;

  return (
    <div className="col">
      <div className="row-wrap">
        <h2 style={{ flex: 1 }}>継続率</h2>
        {[7, 14, 30, 90].map((d) => (
          <button key={d} className={`btn xs ${d === days ? "active" : "ghost"}`} onClick={() => setDays(d)}>{d}日</button>
        ))}
      </div>
      <ErrorBox error={error} onRetry={reload} />

      <div className="grid grid-2">
        <div className="glass pad col">
          <h3>DAU（その日にRollした人）</h3>
          <Bars points={data?.dau ?? []} color="linear-gradient(180deg, #8ab4ff, rgba(90,110,255,0.35))" />
          <div className="row tiny faint">
            <span>{data?.dau[0]?.date ?? ""}</span><span className="spacer" /><span>{data?.dau[data.dau.length - 1]?.date ?? ""}</span>
          </div>
        </div>
        <div className="glass pad col">
          <h3>新規登録</h3>
          <Bars points={data?.new_users ?? []} color="linear-gradient(180deg, #4dffb8, rgba(0,166,255,0.3))" />
          <div className="tiny faint">期間合計 {fmtInt((data?.new_users ?? []).reduce((a, p) => a + p.count, 0))} 人</div>
        </div>
      </div>

      <div className="glass pad col">
        <h3>初回セッションのファネル（期間内の新規）</h3>
        <div className="col" style={{ gap: 6 }}>
          {(data?.funnel ?? []).map((s) => (
            <div className="row" key={s.step} style={{ gap: 10 }}>
              <span style={{ width: 128 }} className="small">{s.step}</span>
              <div className="bar" style={{ flex: 1 }}><i style={{ width: `${s.pct}%` }} /></div>
              <span className="mono small" style={{ width: 110, textAlign: "right" }}>{fmtInt(s.count)}（{s.pct}%）</span>
            </div>
          ))}
        </div>
      </div>

      <div className="glass pad col">
        <h3>コホート（登録日ごとの復帰率）</h3>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>登録日</th><th>新規</th><th>D1</th><th>D7</th></tr></thead>
            <tbody>
              {(data?.cohorts ?? []).slice().reverse().map((c) => (
                <tr key={c.date}>
                  <td className="mono">{c.date}</td>
                  <td className="mono">{fmtInt(c.new_users)}</td>
                  <td className="mono">{c.d1 == null ? "—" : `${c.d1}%`}</td>
                  <td className="mono">{c.d7 == null ? "—" : `${c.d7}%`}</td>
                </tr>
              ))}
              {!data?.cohorts.length && <tr><td colSpan={4} className="muted">まだ集計できるコホートがありません</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="tiny faint">D1 は登録翌日、D7 は登録から7日後にRollした人の割合。まだ日数が経っていないコホートは「—」。</div>
      </div>
    </div>
  );
}
