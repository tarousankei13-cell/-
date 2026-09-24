import { useEffect, useState } from "react";
import { post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";
import { ItemIcon } from "../components/ItemIcon";
import { Empty, ErrorBox, ItemCard, LockedFeature, Modal, RarityBadge, Spinner, Tabs, useConfirm } from "../components/ui";
import { fmtDate, fmtInt, fmtOdds } from "../lib/format";
import type { Instance, InventoryGroup, ItemInfo, RarityKey } from "../lib/types";

const RARITIES: RarityKey[] = ["common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic", "admin"];
const SORTS = [
  { key: "rarity:desc", label: "レア度 ↓" },
  { key: "rarity:asc", label: "レア度 ↑" },
  { key: "odds:desc", label: "確率 ↓" },
  { key: "obtained:desc", label: "取得順 ↓" },
  { key: "obtained:asc", label: "取得順 ↑" },
  { key: "price:desc", label: "価値 ↓" },
  { key: "count:desc", label: "所持数 ↓" },
  { key: "name:asc", label: "名前 A→Z" },
];

export function Inventory() {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [sort, setSort] = useState("rarity:desc");
  const [rarity, setRarity] = useState<RarityKey[]>([]);
  const [favOnly, setFavOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<ItemInfo | null>(null);
  const setStardust = useGame((s) => s.setStardust);
  const toast = useGame((s) => s.toast);
  const { confirm, node: confirmNode } = useConfirm();
  const { run, busy } = useAction();

  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebounced(q);
      setPage(1);
    }, 260);
    return () => window.clearTimeout(t);
  }, [q]);

  const { data, loading, error, locked, reload } = useApi<{
    groups: InventoryGroup[]; total: number; page: number; per_page: number; capacity: number; count: number;
  }>("/api/inventory", { q: debounced, sort, rarity, favorites: favOnly, page });

  const bulkSell = async () => {
    const preview = await run(() => post<{ count: number; estimated: number }>("/api/inventory/sell-bulk", { keep: 1, max_tier: 2, preview: true }, true));
    if (!preview) return;
    if (preview.count === 0) {
      toast("売却できる重複アイテムがありません", "info", "Common/Rareで2個目以降・ロック/お気に入り以外が対象です");
      return;
    }
    const ok = await confirm("重複アイテムをまとめて売却", (
      <div className="col">
        <p>Common / Rare の重複（各1個は残す）を売却します。</p>
        <div className="row-wrap">
          <span className="chip">{fmtInt(preview.count)} 個</span>
          <span className="chip" style={{ color: "var(--gold)" }}>✦ +{fmtInt(preview.estimated)}</span>
        </div>
        <p className="tiny muted">ロック中・お気に入り・出品中のアイテムは対象外です。</p>
      </div>
    ));
    if (!ok) return;
    const res = await run(() => post<{ sold: number; earned: number; stardust: number }>("/api/inventory/sell-bulk", { keep: 1, max_tier: 2 }, true));
    if (res) {
      audio.sfx("coin");
      setStardust(res.stardust);
      toast(`${fmtInt(res.sold)}個を売却`, "success", `✦ +${fmtInt(res.earned)}`);
      reload();
    }
  };

  if (locked !== null) return <LockedFeature feature="inventory" />;

  const pages = data ? Math.max(1, Math.ceil(data.total / data.per_page)) : 1;
  const capPct = data ? Math.min(100, (data.count / Math.max(1, data.capacity)) * 100) : 0;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Inventory</h1>
          {data && (
            <div className="sub">
              {fmtInt(data.count)} / {fmtInt(data.capacity)} 個 · {fmtInt(data.total)} 種類
            </div>
          )}
        </div>
        <button className="btn sm" onClick={bulkSell} disabled={busy}>重複を一括売却</button>
      </div>

      {data && (
        <div className="bar" style={{ marginBottom: 12 }}>
          <i style={{ width: `${capPct}%`, background: capPct > 90 ? "linear-gradient(90deg, var(--warn), var(--bad))" : undefined }} />
        </div>
      )}

      <div className="glass pad" style={{ marginBottom: 12, display: "flex", flexDirection: "column", gap: 10 }}>
        <div className="row-wrap">
          <input type="search" placeholder="アイテム名で検索" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 220px" }} />
          <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(1); }} style={{ width: "auto", minWidth: 150 }}>
            {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
          <label className="switch">
            <input type="checkbox" checked={favOnly} onChange={(e) => { setFavOnly(e.target.checked); setPage(1); }} />
            <span className="track" /><span className="tiny">お気に入り</span>
          </label>
        </div>
        <div className="row-wrap" style={{ gap: 5 }}>
          {RARITIES.map((r) => (
            <button key={r} className={`chip ${rarity.includes(r) ? "on" : ""} r-${r}`}
              onClick={() => { setRarity((cur) => cur.includes(r) ? cur.filter((x) => x !== r) : [...cur, r]); setPage(1); }}>
              {r}
            </button>
          ))}
          {rarity.length > 0 && <button className="btn xs ghost" onClick={() => setRarity([])}>クリア</button>}
        </div>
      </div>

      <ErrorBox error={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : !data || data.groups.length === 0 ? (
        <Empty icon="🎒">該当するアイテムがありません</Empty>
      ) : (
        <>
          <div className="grid grid-auto">
            {data.groups.map((g) => (
              <ItemCard
                key={g.item.id}
                item={g.item}
                count={g.count}
                corner={
                  <span className="row" style={{ gap: 3 }}>
                    {g.favorite && <span title="お気に入り（自動削除から保護）">⭐</span>}
                    {g.locked > 0 && <span title={`${g.locked}個ロック中`}>🔒</span>}
                    {g.listed > 0 && <span title={`${g.listed}個出品中`}>💱</span>}
                  </span>
                }
                onClick={() => setDetail(g.item)}
              />
            ))}
          </div>
          {pages > 1 && (
            <div className="row center" style={{ justifyContent: "center", marginTop: 14, gap: 8 }}>
              <button className="btn sm ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>前へ</button>
              <span className="mono small">{page} / {pages}</span>
              <button className="btn sm ghost" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>次へ</button>
            </div>
          )}
        </>
      )}

      {detail && <ItemDetailModal item={detail} onClose={() => setDetail(null)} onChanged={reload} />}
      {confirmNode}
    </div>
  );
}

export function ItemDetailModal({ item, onClose, onChanged }: { item: ItemInfo; onClose: () => void; onChanged?: () => void }) {
  const [tab, setTab] = useState<"info" | "instances">("info");
  const { data: detail } = useApi<any>(`/api/items/${item.id}`);
  const { data: instances, reload: reloadInstances } = useApi<{ instances: Instance[]; total: number }>(`/api/inventory/item/${item.id}`);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const { run, busy } = useAction();
  const setStardust = useGame((s) => s.setStardust);
  const toast = useGame((s) => s.toast);
  const { confirm, node: confirmNode } = useConfirm();

  const list = instances?.instances ?? [];
  const allSelected = list.length > 0 && selected.size === list.filter((i) => !i.locked && i.state === "owned").length;

  const toggle = (id: number) => setSelected((s) => {
    const n = new Set(s);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  const act = async (kind: "lock" | "unlock" | "fav" | "unfav" | "sell") => {
    const ids = [...selected];
    if (!ids.length) return;
    if (kind === "sell") {
      const ok = await confirm("売却", `${ids.length}個を売却します。よろしいですか？`, { danger: true });
      if (!ok) return;
      const res = await run(() => post<{ sold: number; earned: number; stardust: number }>("/api/inventory/sell", { instance_ids: ids }, true));
      if (res) {
        audio.sfx("coin");
        setStardust(res.stardust);
        toast(`${res.sold}個を売却`, "success", `✦ +${fmtInt(res.earned)}`);
      }
    } else {
      const body: any = { instance_ids: ids };
      if (kind === "lock" || kind === "unlock") body.locked = kind === "lock";
      if (kind === "fav" || kind === "unfav") body.favorite = kind === "fav";
      await run(() => post("/api/inventory/flags", body));
    }
    setSelected(new Set());
    reloadInstances();
    onChanged?.();
  };

  const toggleItemFavorite = async (fav: boolean) => {
    await run(() => post("/api/inventory/item-favorite", { item_id: item.id, favorite: fav }), { success: fav ? "お気に入りに追加（自動削除から保護）" : "お気に入りを解除" });
    onChanged?.();
  };

  const world = detail?.world;
  return (
    <Modal open onClose={onClose} title={item.name} wide
      footer={tab === "instances" && selected.size > 0 ? (
        <>
          <span className="muted small" style={{ marginRight: "auto" }}>{selected.size}個選択中</span>
          <button className="btn sm" disabled={busy} onClick={() => act("lock")}>🔒 ロック</button>
          <button className="btn sm" disabled={busy} onClick={() => act("unlock")}>解除</button>
          <button className="btn sm danger" disabled={busy} onClick={() => act("sell")}>売却</button>
        </>
      ) : undefined}>
      <div className="col" style={{ gap: 14 }}>
        <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
          <ItemIcon visual={item.visual} tier={item.tier} size={92} />
          <div className="col" style={{ gap: 6, flex: 1, minWidth: 0 }}>
            <div className="row-wrap">
              <RarityBadge rarity={item.rarity} tier={item.tier} />
              <span className="chip mono">{fmtOdds(item.odds, item.display_odds)}</span>
              {item.kind === "generated" && <span className="chip tiny">自動生成</span>}
              {!item.tradeable && <span className="chip tiny" style={{ color: "var(--warn)" }}>取引不可</span>}
            </div>
            <p className="muted small" style={{ margin: 0 }}>{item.description}</p>
            {detail?.lore && <p className="tiny" style={{ fontStyle: "italic", opacity: 0.75, margin: 0 }}>「{detail.lore}」</p>}
            {item.biomes.length > 0 && (
              <div className="row-wrap tiny">
                <span className="faint">Biome限定:</span>
                {item.biomes.map((b) => <span key={b} className="chip tiny">{b}</span>)}
              </div>
            )}
          </div>
        </div>

        <Tabs tabs={[{ key: "info", label: "世界の記録" }, { key: "instances", label: `所持 (${instances?.total ?? 0})` }]} value={tab} onChange={setTab} />

        {tab === "info" ? (
          <div className="grid grid-2">
            <div className="glass-2 pad-sm col">
              <div className="row"><span className="muted small" style={{ flex: 1 }}>発見回数</span><span className="mono">{fmtInt(world?.discovery_count)}</span></div>
              <div className="row"><span className="muted small" style={{ flex: 1 }}>所有者数</span><span className="mono">{fmtInt(world?.owner_count)}</span></div>
              <div className="row"><span className="muted small" style={{ flex: 1 }}>取引回数</span><span className="mono">{fmtInt(world?.trade_count)}</span></div>
              <div className="row"><span className="muted small" style={{ flex: 1 }}>市場参考価格</span><span className="mono">✦{fmtInt(world?.market_value)}</span></div>
              <div className="row"><span className="muted small" style={{ flex: 1 }}>売却価格</span><span className="mono">✦{fmtInt(item.sell_value)}</span></div>
            </div>
            <div className="glass-2 pad-sm col">
              <div className="small muted">世界初発見</div>
              {world?.first_discoverer ? (
                <>
                  <div className="row"><strong style={{ color: "var(--gold)" }}>{world.first_discoverer.name}</strong></div>
                  <div className="tiny faint">{fmtDate(world.first_discovered_at)}</div>
                </>
              ) : <div className="tiny faint">まだ誰も発見していません</div>}
              {world?.fastest && (
                <div className="tiny">最速: {world.fastest.user.name}（{fmtInt(world.fastest.rolls)} rolls）</div>
              )}
              {detail?.mine && (
                <div className="tiny" style={{ marginTop: 6 }}>
                  自分: {fmtInt(detail.mine.times_obtained)}回取得 / 初回 {fmtDate(detail.mine.first_obtained_at)}
                </div>
              )}
            </div>
            {detail?.artifact && (
              <div className="glass-2 pad-sm col" style={{ gridColumn: "1 / -1", borderColor: "rgba(255,210,120,0.5)" }}>
                <div className="row"><span className="badge r-admin">ADMIN ARTIFACT</span><span className="chip tiny">{detail.artifact.theme}</span></div>
                <div className="small">{detail.artifact.ability}</div>
                <div className="tiny faint">
                  対象: {detail.artifact.target} / CD {detail.artifact.cooldown_sec}s
                  {detail.artifact.duration_sec ? ` / 効果 ${detail.artifact.duration_sec}s` : ""}
                  {detail.artifact.player_usable ? " / プレイヤー使用可（付与時）" : " / 管理者専用"}
                </div>
              </div>
            )}
            <div className="row-wrap" style={{ gridColumn: "1 / -1" }}>
              <button className="btn sm" onClick={() => toggleItemFavorite(true)} disabled={busy}>⭐ お気に入り登録</button>
              <button className="btn sm ghost" onClick={() => toggleItemFavorite(false)} disabled={busy}>解除</button>
              <span className="tiny faint">お気に入りは自動削除フィルターから常に保護されます</span>
            </div>
          </div>
        ) : (
          <div className="col" style={{ gap: 6 }}>
            <div className="row-wrap">
              <button className="btn xs ghost" onClick={() => setSelected(allSelected ? new Set() : new Set(list.filter((i) => !i.locked && i.state === "owned").map((i) => i.id)))}>
                {allSelected ? "選択解除" : "すべて選択"}
              </button>
            </div>
            <div style={{ maxHeight: "44vh", overflow: "auto" }} className="col">
              {list.map((inst) => (
                <label key={inst.id} className="row" style={{ gap: 9, padding: "6px 8px", borderRadius: 8, background: selected.has(inst.id) ? "rgba(120,160,255,0.14)" : "rgba(255,255,255,0.03)" }}>
                  <input type="checkbox" checked={selected.has(inst.id)} disabled={inst.locked || inst.state !== "owned"} onChange={() => toggle(inst.id)} />
                  <span className="mono tiny" style={{ flex: 1 }}>
                    {inst.serial ? `#${fmtInt(inst.serial)}` : `id:${inst.id}`}
                    {inst.meta?.luck ? ` · Luck ${fmtInt(inst.meta.luck)}` : ""}
                  </span>
                  <span className="tiny faint">{inst.source}</span>
                  {inst.locked && <span title="ロック中">🔒</span>}
                  {inst.state !== "owned" && <span className="tiny" style={{ color: "var(--warn)" }}>{inst.state}</span>}
                  <span className="tiny faint">{fmtDate(inst.obtained_at)}</span>
                </label>
              ))}
            </div>
          </div>
        )}
      </div>
      {confirmNode}
    </Modal>
  );
}
