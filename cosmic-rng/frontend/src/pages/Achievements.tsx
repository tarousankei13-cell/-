import { useMemo, useState } from "react";
import { useApi } from "../lib/useApi";
import { Empty, ErrorBox, Spinner, Tabs } from "../components/ui";
import { fmtDate, fmtInt } from "../lib/format";

interface Ach {
  key: string; name: string; description: string | null; hint: string | null; category: string; tier: string;
  hidden: boolean; achieved: boolean; achieved_at: string | null; world_first: boolean; rewards: any;
  achiever_count: number; first_achiever: { id: number; name: string } | null;
}

const TIER_COLOR: Record<string, string> = { bronze: "#c98b5a", silver: "#c8d2e8", gold: "var(--gold)", cosmic: "var(--r-ultra_secret)" };
const CAT_LABEL: Record<string, string> = {
  rolls: "Roll", rarity: "レア度", collection: "コレクション", biome: "Biome", economy: "経済", social: "交流",
  crafting: "クラフト", luck: "Luck", discovery: "発見", progress: "進行", hidden: "隠し",
};

export function Achievements() {
  const { data, loading, error, reload } = useApi<{ achievements: Ach[]; achieved: number; total: number }>("/api/achievements");
  const [cat, setCat] = useState<string>("all");
  const [onlyMissing, setOnlyMissing] = useState(false);

  const cats = useMemo(() => Array.from(new Set((data?.achievements ?? []).map((a) => a.category))), [data]);
  const list = useMemo(() => {
    let l = data?.achievements ?? [];
    if (cat !== "all") l = l.filter((a) => a.category === cat);
    if (onlyMissing) l = l.filter((a) => !a.achieved);
    return l;
  }, [data, cat, onlyMissing]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Achievements</h1>
          {data && <div className="sub">{fmtInt(data.achieved)} / {fmtInt(data.total)} 達成</div>}
        </div>
        <label className="switch">
          <input type="checkbox" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} />
          <span className="track" /><span className="tiny">未達成のみ</span>
        </label>
      </div>

      {data && <div className="bar" style={{ marginBottom: 12 }}><i style={{ width: `${(data.achieved / Math.max(1, data.total)) * 100}%` }} /></div>}

      <Tabs value={cat} onChange={setCat} tabs={[{ key: "all", label: "すべて" }, ...cats.map((c) => ({ key: c, label: CAT_LABEL[c] ?? c }))]} />

      <ErrorBox error={error} onRetry={reload} />
      <div style={{ marginTop: 12 }}>
        {loading && !data ? <Spinner /> : list.length === 0 ? <Empty icon="🏆">該当なし</Empty> : (
          <div className="grid grid-auto">
            {list.map((a) => (
              <div key={a.key} className="item-card" style={{
                cursor: "default",
                opacity: a.achieved ? 1 : 0.72,
                borderColor: a.world_first ? "rgba(255,210,120,0.6)" : a.achieved ? `${TIER_COLOR[a.tier]}66` : undefined,
              }}>
                <div className="row" style={{ gap: 9 }}>
                  <div style={{ fontSize: "1.5rem", filter: a.achieved ? `drop-shadow(0 0 8px ${TIER_COLOR[a.tier]})` : "grayscale(1)", opacity: a.achieved ? 1 : 0.5 }}>
                    {a.world_first ? "👑" : a.achieved ? "🏆" : a.hidden ? "❔" : "🔓"}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="row" style={{ gap: 6 }}>
                      <span className="name" style={{ color: a.achieved ? TIER_COLOR[a.tier] : undefined }}>{a.name}</span>
                      {a.world_first && <span className="badge" style={{ color: "var(--gold)" }}>世界初</span>}
                    </div>
                    <div className="tiny muted">{a.description ?? (a.hint ? `💡 ${a.hint}` : "???")}</div>
                  </div>
                </div>
                <div className="row-wrap tiny" style={{ gap: 4 }}>
                  <span className="chip tiny" style={{ color: TIER_COLOR[a.tier] }}>{a.tier}</span>
                  {a.rewards?.stardust > 0 && <span className="chip tiny" style={{ color: "var(--gold)" }}>✦ {fmtInt(a.rewards.stardust)}</span>}
                  {(a.rewards?.cosmetics ?? []).map((c: string) => <span key={c} className="chip tiny" style={{ color: "var(--r-epic)" }}>{c}</span>)}
                  <span className="chip tiny faint">{fmtInt(a.achiever_count)}人達成</span>
                </div>
                {a.achieved && <div className="tiny faint">{fmtDate(a.achieved_at)}</div>}
                {!a.achieved && a.first_achiever && <div className="tiny faint">世界初: {a.first_achiever.name}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
