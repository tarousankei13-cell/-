import { useState } from "react";
import { useApi } from "../lib/useApi";
import { Empty, ErrorBox, Modal, Spinner } from "../components/ui";
import { fmtInt, fmtOdds } from "../lib/format";
import { useGame } from "../store/game";

interface BiomeEntry {
  key: string;
  name: string;
  kind: string;
  description?: string;
  odds_per_sec?: number | null;
  duration_sec?: number;
  luck_mult?: number;
  min_level?: number;
  theme: any;
  hidden: boolean;
  states: { key: string; name: string; luck_mult: number; description: string }[];
  seen: boolean;
  locked: boolean;
  exclusive_items: { id: number; name: string; rarity: string; odds: number | null }[];
}

export function Biomes() {
  const { data, loading, error, reload } = useApi<{ biomes: BiomeEntry[] }>("/api/biomes");
  const current = useGame((s) => s.hud?.biome);
  const [sel, setSel] = useState<BiomeEntry | null>(null);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Biome</h1>
          <div className="sub">宇宙は毎秒揺らいでいる。稀なBiomeほどLuckも出現アイテムも別格。</div>
        </div>
        {data && <span className="chip">{data.biomes.filter((b) => b.seen).length} / {data.biomes.length} 遭遇</span>}
      </div>

      <ErrorBox error={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : !data?.biomes.length ? <Empty icon="🌌">データがありません</Empty> : (
        <div className="grid grid-auto">
          {data.biomes.map((b) => {
            const active = current?.key === b.key;
            const accent = b.theme?.accent ?? "#8ab4ff";
            return (
              <button key={b.key} className="item-card" onClick={() => setSel(b)}
                style={{
                  borderColor: active ? accent : b.seen ? `${accent}55` : undefined,
                  boxShadow: active ? `0 0 0 1px ${accent}, 0 0 24px ${accent}44` : undefined,
                  opacity: b.seen || b.kind === "default" ? 1 : 0.68,
                  background: b.theme?.bg ? `linear-gradient(135deg, ${b.theme.bg[1] ?? "#0a0f22"}, ${b.theme.bg[2] ?? "#141028"})` : undefined,
                }}>
                <div className="row">
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="row" style={{ gap: 6 }}>
                      <span className="name" style={{ color: accent }}>{b.name}</span>
                      {active && <span className="badge" style={{ color: "var(--good)" }}>NOW</span>}
                      {b.kind === "admin" && <span className="badge r-admin">ADMIN</span>}
                      {b.locked && <span className="badge" style={{ color: "var(--warn)" }}>Lv.{b.min_level}</span>}
                    </div>
                    <div className="tiny muted ellipsis">{b.seen || !b.hidden ? b.description : "未知のBiome"}</div>
                  </div>
                </div>
                <div className="row-wrap tiny" style={{ gap: 5 }}>
                  {b.odds_per_sec ? <span className="chip tiny mono">1/{fmtInt(b.odds_per_sec)} /秒</span> : b.kind === "default" ? <span className="chip tiny">通常空間</span> : <span className="chip tiny">特殊</span>}
                  {b.luck_mult !== undefined && <span className="chip tiny">Luck ×{b.luck_mult}</span>}
                  {b.duration_sec ? <span className="chip tiny">{b.duration_sec}s</span> : null}
                  {b.states.length > 0 && <span className="chip tiny" style={{ color: "var(--gold)" }}>特殊状態</span>}
                </div>
              </button>
            );
          })}
        </div>
      )}

      {sel && (
        <Modal open onClose={() => setSel(null)} title={sel.name} wide>
          <div className="col" style={{ gap: 14 }}>
            <p className="muted" style={{ margin: 0 }}>{sel.description || "未知のBiome"}</p>
            <div className="row-wrap">
              {sel.odds_per_sec ? <span className="chip mono">出現率 1/{fmtInt(sel.odds_per_sec)} 毎秒</span> : null}
              {sel.luck_mult !== undefined && <span className="chip">Luck ×{sel.luck_mult}</span>}
              {sel.duration_sec ? <span className="chip">継続 {sel.duration_sec}秒</span> : null}
              {sel.min_level && sel.min_level > 1 ? <span className="chip">Lv.{sel.min_level}以上</span> : null}
              <span className="chip">{sel.seen ? "遭遇済み" : "未遭遇"}</span>
            </div>

            {sel.states.length > 0 && (
              <div className="glass-2 pad-sm col">
                <div className="small" style={{ color: "var(--gold)" }}>特殊状態</div>
                {sel.states.map((st) => (
                  <div key={st.key} className="row" style={{ gap: 8 }}>
                    <span className="badge" style={{ color: "var(--gold)" }}>{st.name}</span>
                    <span className="small muted" style={{ flex: 1 }}>{st.description}</span>
                    <span className="mono tiny">Luck ×{st.luck_mult}</span>
                  </div>
                ))}
              </div>
            )}

            {sel.exclusive_items.length > 0 && (
              <div>
                <h3 style={{ marginBottom: 8 }}>このBiome限定アイテム</h3>
                <div className="col" style={{ gap: 4, maxHeight: "40vh", overflow: "auto" }}>
                  {sel.exclusive_items.map((it) => (
                    <div key={it.id} className="row" style={{ gap: 8, padding: "5px 8px", borderRadius: 8, background: "rgba(255,255,255,0.03)" }}>
                      <span className={`r-${it.rarity}`} style={{ flex: 1 }}>{it.name}</span>
                      <span className="mono tiny faint">{it.odds ? fmtOdds(it.odds) : "1 / ???"}</span>
                    </div>
                  ))}
                </div>
                <div className="tiny faint" style={{ marginTop: 6 }}>Biome終了後は通常状態では取得できません。</div>
              </div>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
