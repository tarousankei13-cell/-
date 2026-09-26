import { useMemo, useState } from "react";
import { useApi } from "../lib/useApi";
import { ItemIcon } from "../components/ItemIcon";
import { Empty, ErrorBox, Modal, RarityBadge, Spinner, Tabs, Name } from "../components/ui";
import { fmtDate, fmtInt, fmtOdds, RARITY_LABEL_JA } from "../lib/format";
import type { ItemInfo, RarityKey } from "../lib/types";
import { ItemDetailModal } from "./Inventory";

interface Entry {
  id: number;
  state: "owned" | "discovered" | "undiscovered";
  key?: string;
  name: string;
  description?: string;
  lore?: string;
  rarity: RarityKey;
  tier: number;
  odds: number | null;
  display_odds: string | null;
  visual: any;
  biomes: string[];
  owned?: number;
  times_obtained?: number;
  first_obtained_at?: string | null;
  hint?: string | null;
  discovered_by_world?: boolean;
  world?: {
    discovery_count: number; owner_count: number; trade_count: number;
    first_discoverer: { id: number; name: string } | null; first_discovered_at: string | null;
  };
}

export function Collection() {
  const { data, loading, error, reload } = useApi<{ entries: Entry[]; discovered: number; total: number; rate: number; generated: ItemInfo[] }>("/api/collection");
  const [filter, setFilter] = useState<"all" | "owned" | "discovered" | "undiscovered">("all");
  const [rarity, setRarity] = useState<RarityKey | "all">("all");
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<Entry | null>(null);
  const [genSel, setGenSel] = useState<ItemInfo | null>(null);
  const [tab, setTab] = useState<"fixed" | "generated">("fixed");

  const entries = useMemo(() => {
    let list = data?.entries ?? [];
    if (filter !== "all") list = list.filter((e) => e.state === filter);
    if (rarity !== "all") list = list.filter((e) => e.rarity === rarity);
    if (q) list = list.filter((e) => e.name.toLowerCase().includes(q.toLowerCase()));
    return list;
  }, [data, filter, rarity, q]);

  const byRarity = useMemo(() => {
    const m: Record<string, { total: number; found: number }> = {};
    for (const e of data?.entries ?? []) {
      m[e.rarity] ??= { total: 0, found: 0 };
      m[e.rarity].total++;
      if (e.state !== "undiscovered") m[e.rarity].found++;
    }
    return m;
  }, [data]);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>図鑑<span className="h1-en">Collection</span></h1>
          {data && <div className="sub">{fmtInt(data.discovered)} / {fmtInt(data.total)} 種類発見（{(data.rate * 100).toFixed(1)}%）</div>}
        </div>
      </div>

      {data && (
        <div className="glass pad" style={{ marginBottom: 12 }}>
          <div className="bar" style={{ marginBottom: 10 }}><i style={{ width: `${data.rate * 100}%` }} /></div>
          <div className="row-wrap" style={{ gap: 6 }}>
            {Object.entries(byRarity).map(([r, v]) => (
              <span key={r} className={`chip tiny r-${r}`}>{RARITY_LABEL_JA[r as RarityKey] ?? r} {v.found}/{v.total}</span>
            ))}
          </div>
        </div>
      )}

      <Tabs tabs={[{ key: "fixed", label: "図鑑" }, { key: "generated", label: `自動生成 (${data?.generated.length ?? 0})` }]} value={tab} onChange={setTab} />

      {tab === "fixed" ? (
        <>
          <div className="glass pad" style={{ margin: "12px 0", display: "flex", flexDirection: "column", gap: 10 }}>
            <div className="row-wrap">
              <input type="search" placeholder="名前で検索" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 200px" }} />
              <select value={filter} onChange={(e) => setFilter(e.target.value as any)} style={{ width: "auto" }}>
                <option value="all">すべて</option>
                <option value="owned">所有中</option>
                <option value="discovered">発見済み（未所持）</option>
                <option value="undiscovered">未発見</option>
              </select>
              <select value={rarity} onChange={(e) => setRarity(e.target.value as any)} style={{ width: "auto" }}>
                <option value="all">全レア度</option>
                {Object.keys(byRarity).map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
          </div>

          <ErrorBox error={error} onRetry={reload} />
          {loading && !data ? <Spinner /> : entries.length === 0 ? <Empty icon="📖">該当なし</Empty> : (
            <div className="grid grid-auto">
              {entries.map((e) => (
                <button key={e.id} className={`item-card t${e.state === "undiscovered" ? 1 : e.tier}`}
                  style={e.state === "undiscovered" ? { opacity: 0.62 } : undefined} onClick={() => setSel(e)}>
                  <div className="corner tiny">
                    {e.state === "owned" ? <span title="所有中" style={{ color: "var(--good)" }}>●</span>
                      : e.state === "discovered" ? <span title="発見済み" className="faint">○</span>
                      : e.discovered_by_world ? <span title="他プレイヤーが発見済み" className="faint">·</span> : null}
                  </div>
                  <div className="row" style={{ gap: 10 }}>
                    <ItemIcon visual={e.visual} tier={e.tier} size={44} silhouette={e.state === "undiscovered"} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className={`name ${e.state === "undiscovered" ? "faint" : `r-${e.rarity}`}`}><Name en={e.name} ja={(e as any).name_ja} block /></div>
                      <div className="odds mono">{e.odds ? fmtOdds(e.odds, e.display_odds) : e.display_odds ?? "1 / ???"}</div>
                    </div>
                    {!!e.owned && <span className="mono tiny">×{fmtInt(e.owned)}</span>}
                  </div>
                  {e.state === "undiscovered" && e.hint && <div className="tiny faint ellipsis">💡 {e.hint}</div>}
                </button>
              ))}
            </div>
          )}
        </>
      ) : (
        <div style={{ marginTop: 12 }}>
          {!data?.generated.length ? <Empty icon="⚗">自動生成アイテムはまだ発見していません</Empty> : (
            <div className="grid grid-auto">
              {data.generated.map((g: any) => (
                <button key={g.id} className={`item-card t${g.tier}`} onClick={() => setGenSel(g)}>
                  <div className="row" style={{ gap: 10 }}>
                    <ItemIcon visual={g.visual} tier={g.tier} size={44} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className={`name r-${g.rarity}`}><Name en={g.name} ja={(g as any).name_ja} block /></div>
                      <div className="odds mono">{fmtOdds(g.odds)}</div>
                    </div>
                    <span className="mono tiny">×{fmtInt(g.owned ?? 0)}</span>
                  </div>
                  {g.first_discoverer_is_me && <span className="badge" style={{ color: "var(--gold)" }}>世界初発見</span>}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {sel && (
        <Modal open onClose={() => setSel(null)} title={sel.state === "undiscovered" ? "未発見のアイテム" : ((sel as any).name_ja ? `${sel.name} / ${(sel as any).name_ja}` : sel.name)}>
          <div className="col" style={{ gap: 14 }}>
            <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
              <ItemIcon visual={sel.visual} tier={sel.tier} size={86} silhouette={sel.state === "undiscovered"} />
              <div className="col" style={{ gap: 6, flex: 1 }}>
                <div className="row-wrap">
                  <RarityBadge rarity={sel.rarity} tier={sel.tier} />
                  <span className="chip mono">{sel.odds ? fmtOdds(sel.odds, sel.display_odds) : sel.display_odds ?? "1 / ???"}</span>
                </div>
                {sel.state === "undiscovered" ? (
                  <>
                    <p className="muted small" style={{ margin: 0 }}>まだ発見していません。詳細は発見後に解放されます。</p>
                    {sel.hint && <p className="small" style={{ color: "var(--gold)", margin: 0 }}>💡 {sel.hint}</p>}
                    {sel.biomes.length > 0 && (
                      <div className="row-wrap tiny"><span className="faint">出現Biome:</span>{sel.biomes.map((b) => <span key={b} className="chip tiny">{b}</span>)}</div>
                    )}
                    {sel.discovered_by_world && <div className="tiny faint">※ 他のプレイヤーは既に発見しています</div>}
                  </>
                ) : (
                  <>
                    <p className="muted small" style={{ margin: 0 }}>{sel.description}</p>
                    {sel.lore && <p className="tiny" style={{ fontStyle: "italic", opacity: 0.75, margin: 0 }}>「{sel.lore}」</p>}
                  </>
                )}
              </div>
            </div>
            {sel.state !== "undiscovered" && sel.world && (
              <div className="grid grid-2">
                <div className="glass-2 pad-sm col">
                  <div className="row"><span className="muted small" style={{ flex: 1 }}>世界の発見回数</span><span className="mono">{fmtInt(sel.world.discovery_count)}</span></div>
                  <div className="row"><span className="muted small" style={{ flex: 1 }}>所有者数</span><span className="mono">{fmtInt(sel.world.owner_count)}</span></div>
                  <div className="row"><span className="muted small" style={{ flex: 1 }}>取引回数</span><span className="mono">{fmtInt(sel.world.trade_count)}</span></div>
                </div>
                <div className="glass-2 pad-sm col">
                  <div className="row"><span className="muted small" style={{ flex: 1 }}>自分の取得</span><span className="mono">{fmtInt(sel.times_obtained)}回</span></div>
                  <div className="row"><span className="muted small" style={{ flex: 1 }}>所持</span><span className="mono">{fmtInt(sel.owned ?? 0)}</span></div>
                  <div className="tiny faint">初取得 {fmtDate(sel.first_obtained_at)}</div>
                  {sel.world.first_discoverer && (
                    <div className="tiny">世界初: <strong style={{ color: "var(--gold)" }}>{sel.world.first_discoverer.name}</strong></div>
                  )}
                </div>
              </div>
            )}
          </div>
        </Modal>
      )}
      {genSel && <ItemDetailModal item={genSel} onClose={() => setGenSel(null)} />}
    </div>
  );
}
