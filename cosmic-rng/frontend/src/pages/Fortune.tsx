import { post } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { useGame } from "../store/game";
import { ItemIcon } from "../components/ItemIcon";
import { ErrorBox, Spinner, useConfirm } from "../components/ui";
import { fmtInt, fmtOdds } from "../lib/format";

type TierRow = { tier: number; key?: string; name?: string; name_ja?: string; actual: number; expected: number; ratio: number | null };
type FortuneData = {
  total_rolls: number; best_odds: number; best_item: any;
  top_percent: number; players: number;
  tiers: TierRow[]; daily: { date: string; rolls: number }[];
};
type PrestigeData = { level: number; max_level: number; prestige: number; luck_pct_each: number; current_bonus_pct: number; ready: boolean };

function Sparkline({ days }: { days: { date: string; rolls: number }[] }) {
  const max = Math.max(1, ...days.map((d) => d.rolls));
  return (
    <div className="sparkline" role="img" aria-label="直近14日のRoll数">
      {days.map((d) => (
        <div key={d.date} className="spark-col" title={`${d.date}: ${fmtInt(d.rolls)} Roll`}>
          <i style={{ height: `${Math.max(2, (d.rolls / max) * 100)}%` }} />
        </div>
      ))}
    </div>
  );
}

function PrestigeCard() {
  const { data, reload } = useApi<PrestigeData>("/api/prestige");
  const { busy, run } = useAction();
  const { confirm, node } = useConfirm();
  const refreshHud = useGame((s) => s.refreshHud);
  if (!data) return null;

  const ascend = async () => {
    const ok = await confirm("転生する",
      `レベルが 1 に戻り、代わりに永続Luck +${data.luck_pct_each}% を得ます。アイテム・図鑑・Stardust・装備はそのまま残ります。`,
      { danger: true });
    if (!ok) return;
    await run(() => post("/api/prestige/ascend", {}, true), { success: "転生しました" });
    reload();
    refreshHud();
  };

  return (
    <div className="glass pad col">
      {node}
      <div className="row-wrap">
        <h3 style={{ flex: 1 }}>転生</h3>
        {data.prestige > 0 && <span className="badge" style={{ color: "var(--gold)" }}>★{data.prestige}</span>}
      </div>
      <div className="muted small">
        Lv.{data.max_level} に到達すると、レベルを 1 に戻して永続Luck +{data.luck_pct_each}% を得られます。回数に上限はありません。
      </div>
      <div className="row tiny">
        <span className="mono">Lv.{data.level} / {data.max_level}</span>
        <div className="bar" style={{ flex: 1 }}><i style={{ width: `${Math.min(100, (data.level / data.max_level) * 100)}%` }} /></div>
      </div>
      <div className="row">
        <span className="small" style={{ flex: 1 }}>現在の転生ボーナス: <strong style={{ color: "var(--good)" }}>+{data.current_bonus_pct}%</strong></span>
        <button className="btn sm primary" disabled={!data.ready || busy} onClick={ascend}>
          {data.ready ? "転生する" : "まだ到達していません"}
        </button>
      </div>
    </div>
  );
}

export function Fortune() {
  const { data, loading, error, reload } = useApi<FortuneData>("/api/fortune");
  if (loading && !data) return <Spinner label="運勢を計算中…" />;

  return (
    <div className="page">
      <div className="page-head">
        <h1>運勢レポート</h1>
        <span className="sub">あなたの引きを、期待値と並べて見る</span>
      </div>
      <ErrorBox error={error} onRetry={reload} />

      <div className="grid grid-2">
        <div className="glass pad col">
          <h3>これまで</h3>
          <div className="row-wrap" style={{ gap: 18 }}>
            <div className="stat"><span className="k">総Roll数</span><span className="v mono">{fmtInt(data?.total_rolls ?? 0)}</span></div>
            <div className="stat">
              <span className="k">プレイヤー中の順位</span>
              <span className="v mono" style={{ color: "var(--gold)" }}>上位 {data?.top_percent ?? 100}%</span>
            </div>
            <div className="stat"><span className="k">母数</span><span className="v mono">{fmtInt(data?.players ?? 0)}人</span></div>
          </div>
          {data?.best_item && (
            <div className="row glass-2 pad-sm">
              <ItemIcon visual={data.best_item.visual} tier={data.best_item.tier} size={40} />
              <div className="col" style={{ gap: 0, flex: 1, minWidth: 0 }}>
                <span className="tiny faint">最高記録</span>
                <span className={`ellipsis r-${data.best_item.rarity}`}>{data.best_item.name_ja || data.best_item.name}</span>
              </div>
              <span className="mono">{fmtOdds(data.best_odds)}</span>
            </div>
          )}
          <div className="col" style={{ gap: 4 }}>
            <span className="tiny faint">直近14日</span>
            <Sparkline days={data?.daily ?? []} />
          </div>
        </div>

        <div className="glass pad col">
          <h3>レア度ごとの引き</h3>
          <div className="muted small">期待値は、あなたが引いた回数ぶんの理論値です。1.00 より大きいほど出すぎ。</div>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>レア度</th><th>実際</th><th>期待</th><th>比</th></tr></thead>
              <tbody>
                {(data?.tiers ?? []).map((t) => (
                  <tr key={t.tier}>
                    <td className={`r-${t.key}`}>{t.name_ja || t.name || `T${t.tier}`}</td>
                    <td className="mono">{fmtInt(t.actual)}</td>
                    <td className="mono faint">{t.expected.toFixed(2)}</td>
                    <td className="mono" style={{ color: t.ratio == null ? undefined : t.ratio >= 1 ? "var(--good)" : "var(--text-dim)" }}>
                      {t.ratio == null ? "—" : `×${t.ratio.toFixed(2)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div style={{ marginTop: 12 }}><PrestigeCard /></div>
    </div>
  );
}
