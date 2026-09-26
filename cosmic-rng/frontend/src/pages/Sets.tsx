import { post } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { useGame } from "../store/game";
import { ItemIcon } from "../components/ItemIcon";
import { ErrorBox, Spinner } from "../components/ui";
import { fmtInt } from "../lib/format";

type SetItem = { key: string; name: string; name_ja: string; rarity: string; visual: any; owned: boolean };
type CollectionSet = {
  key: string; name: string; name_ja: string; luck_pct: number; stardust: number; cosmetic: string | null;
  items: SetItem[]; owned: number; total: number; complete: boolean; claimed: boolean;
};

export function Sets() {
  const { data, loading, error, reload } = useApi<{ sets: CollectionSet[]; claimed: number }>("/api/sets");
  const { busy, run } = useAction();
  const refreshHud = useGame((s) => s.refreshHud);

  const claim = (key: string) =>
    run(() => post("/api/sets/claim", { set_key: key }, true), { success: "セットボーナスを獲得しました" })
      .then(() => { reload(); refreshHud(); });

  if (loading && !data) return <Spinner label="セットを読み込み中…" />;
  const totalLuck = (data?.sets ?? []).filter((s) => s.claimed).reduce((a, s) => a + s.luck_pct, 0);

  return (
    <div className="page">
      <div className="page-head">
        <h1>セット収集</h1>
        <span className="sub">
          達成済み {data?.claimed ?? 0} / {data?.sets.length ?? 0} · 永続Luck +{totalLuck.toFixed(1)}%
        </span>
      </div>
      <ErrorBox error={error} onRetry={reload} />

      <div className="grid grid-2">
        {(data?.sets ?? []).map((s) => (
          <div key={s.key} className={`glass pad col set-card ${s.complete ? "complete" : ""}`} style={{ gap: 9 }}>
            <div className="row-wrap">
              <div className="col" style={{ flex: 1, minWidth: 0, gap: 0 }}>
                <h3 className="ellipsis">{s.name_ja}</h3>
                <span className="tiny faint ellipsis">{s.name}</span>
              </div>
              <span className="badge" style={{ color: s.claimed ? "var(--good)" : "var(--accent)" }}>
                永続Luck +{s.luck_pct}%
              </span>
            </div>

            <div className="row tiny">
              <span className="mono">{s.owned} / {s.total}</span>
              <div className="bar" style={{ flex: 1 }}><i style={{ width: `${(s.owned / Math.max(1, s.total)) * 100}%` }} /></div>
            </div>

            <div className="set-items">
              {s.items.map((it) => (
                <div key={it.key} className={`set-item ${it.owned ? "have" : ""}`} title={it.owned ? (it.name_ja || it.name) : "未発見"}>
                  {it.owned
                    ? <ItemIcon visual={it.visual} size={34} animate={false} />
                    : <span className="set-unknown">?</span>}
                  <span className="tiny ellipsis">{it.owned ? (it.name_ja || it.name) : "???"}</span>
                </div>
              ))}
            </div>

            <div className="row">
              <span className="tiny faint" style={{ flex: 1 }}>
                報酬: ✦{fmtInt(s.stardust)}{s.cosmetic ? " · 称号" : ""}
              </span>
              {s.claimed
                ? <span className="badge" style={{ color: "var(--good)" }}>受取済</span>
                : <button className="btn sm primary" disabled={!s.complete || busy} onClick={() => claim(s.key)}>
                    {s.complete ? "ボーナスを受け取る" : "収集中"}
                  </button>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
