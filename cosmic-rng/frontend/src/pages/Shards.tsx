import { post } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { useGame } from "../store/game";
import { Empty, ErrorBox, Spinner, useConfirm } from "../components/ui";
import { fmtInt } from "../lib/format";

type Exchange = { key: string; name: string; cost: number; tier: string; description: string };
type ShardData = {
  shards: number;
  values: Record<string, number>;
  exchanges: Exchange[];
  convertible: { count: number; shards: number };
};

export function Shards() {
  const { data, loading, error, reload } = useApi<ShardData>("/api/shards");
  const { busy, run } = useAction();
  const { confirm, node } = useConfirm();
  const refreshHud = useGame((s) => s.refreshHud);

  const convert = async () => {
    const n = data?.convertible.count ?? 0;
    if (!n) return;
    const ok = await confirm("重複アイテムを欠片にする",
      `所持しているアイテムのうち、2つ目以降の ${fmtInt(n)} 個を星の欠片 ${fmtInt(data!.convertible.shards)} に変換します。お気に入り・ロック済み・出品中のものは残ります。`,
      { danger: true });
    if (!ok) return;
    await run(() => post("/api/shards/convert", {}, true), { success: "欠片に変換しました" });
    reload();
    refreshHud();
  };

  const exchange = (e: Exchange) =>
    run(() => post("/api/shards/exchange", { key: e.key }, true), { success: `${e.name} を有効化しました` })
      .then(() => { reload(); refreshHud(); });

  if (loading && !data) return <Spinner label="星の欠片を読み込み中…" />;

  return (
    <div className="page">
      {node}
      <div className="page-head">
        <h1>星の欠片</h1>
        <span className="sub">重複したアイテムを、次の1回の確定枠に変える</span>
      </div>
      <ErrorBox error={error} onRetry={reload} />

      <div className="glass pad row-wrap" style={{ marginBottom: 12, gap: 14 }}>
        <div className="stat"><span className="k">所持している欠片</span><span className="v mono">{fmtInt(data?.shards ?? 0)}</span></div>
        <span className="spacer" />
        <div className="col" style={{ gap: 4, alignItems: "flex-end" }}>
          <span className="small muted">
            変換できる重複: {fmtInt(data?.convertible.count ?? 0)} 個（+{fmtInt(data?.convertible.shards ?? 0)}）
          </span>
          <button className="btn primary" disabled={busy || !data?.convertible.count} onClick={convert}>重複をまとめて変換</button>
        </div>
      </div>

      <div className="glass pad col" style={{ marginBottom: 12 }}>
        <h3>交換</h3>
        <div className="muted small">確率表そのものは変わりません。次の1回に「最低レア度」を保証する効果がつきます。</div>
        <div className="grid grid-auto">
          {(data?.exchanges ?? []).map((e) => (
            <div key={e.key} className={`glass-2 pad-sm col r-${e.tier}`} style={{ gap: 6 }}>
              <strong>{e.name}</strong>
              <span className="tiny muted">{e.description}</span>
              <div className="row">
                <span className="mono" style={{ flex: 1 }}>{fmtInt(e.cost)} 欠片</span>
                <button className="btn xs" disabled={busy || (data?.shards ?? 0) < e.cost} onClick={() => exchange(e)}>交換</button>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="glass pad col">
        <h3>レア度ごとの欠片量</h3>
        {!data ? <Empty>—</Empty> : (
          <div className="row-wrap">
            {Object.entries(data.values).map(([tier, v]) => (
              <span key={tier} className="chip tiny mono">T{tier} → {v}</span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
