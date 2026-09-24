import { useEffect, useState } from "react";
import { get, post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { events, useGame } from "../store/game";
import { audio } from "../audio/engine";
import { ItemIcon } from "../components/ItemIcon";
import { Empty, ErrorBox, LockedFeature, Modal, Spinner, Tabs, UserChip, useConfirm } from "../components/ui";
import { fmtCompact, fmtDate, fmtInt, fmtOdds } from "../lib/format";
import type { Instance, InventoryGroup } from "../lib/types";

interface Listing {
  id: number; instance_id: number; item: any; price: number; status: string; seller: any;
  created_at: string; expires_at: string; sold_at: string | null; serial: number | null; fee: number;
}

export function Market() {
  const [tab, setTab] = useState<"browse" | "mine" | "sell">("browse");
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [sort, setSort] = useState("newest");
  const [page, setPage] = useState(1);
  const [history, setHistory] = useState<any>(null);
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const setStardust = useGame((s) => s.setStardust);
  const toast = useGame((s) => s.toast);
  const stardust = useGame((s) => s.hud?.stardust ?? 0);

  useEffect(() => {
    const t = window.setTimeout(() => { setDebounced(q); setPage(1); }, 260);
    return () => window.clearTimeout(t);
  }, [q]);

  const browse = useApi<{ listings: Listing[]; total: number; unlocked: boolean; unlock_level: number }>(
    tab === "browse" ? "/api/market" : null, { q: debounced, sort, page });
  const mine = useApi<{ listings: Listing[] }>(tab === "mine" ? "/api/market/mine" : null);

  useEffect(() => events.on("market", () => { browse.reload(true); mine.reload(true); }), [browse.reload, mine.reload]);

  if (browse.locked !== null) return <LockedFeature feature="market" />;

  const buy = async (l: Listing) => {
    const ok = await confirm("購入の確認", (
      <div className="col">
        <div className="row" style={{ gap: 10 }}>
          <ItemIcon visual={l.item?.visual} tier={l.item?.tier ?? 1} size={44} />
          <div><strong className={`r-${l.item?.rarity}`}>{l.item?.name}</strong>
            <div className="tiny muted mono">{fmtOdds(l.item?.odds, l.item?.display_odds)}</div></div>
        </div>
        <div className="row"><span style={{ flex: 1 }}>価格</span><strong className="mono" style={{ color: "var(--gold)" }}>✦ {fmtInt(l.price)}</strong></div>
        <div className="row tiny muted"><span style={{ flex: 1 }}>購入後の残高</span><span className="mono">✦ {fmtInt(stardust - l.price)}</span></div>
      </div>
    ));
    if (!ok) return;
    const res = await run(() => post<{ stardust: number }>("/api/market/buy", { listing_id: l.id }, true));
    if (res) {
      audio.sfx("coin");
      setStardust(res.stardust);
      toast("購入しました", "success", l.item?.name);
      browse.reload();
    }
  };

  const cancel = async (l: Listing) => {
    const ok = await confirm("出品の取り消し", `${l.item?.name} の出品を取り消します。`);
    if (!ok) return;
    if (await run(() => post("/api/market/cancel", { listing_id: l.id }))) mine.reload();
  };

  const showHistory = async (itemId: number) => {
    setHistory({ loading: true });
    try {
      setHistory(await get(`/api/market/history/${itemId}`));
    } catch {
      setHistory(null);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Market</h1>
          <div className="sub">プレイヤー間の売買。価格は自由。異常な価格は自動検知され運営に通知されます。</div>
        </div>
        <span className="chip mono" style={{ color: "var(--gold)" }}>✦ {fmtCompact(stardust)}</span>
      </div>

      <Tabs value={tab} onChange={setTab} tabs={[
        { key: "browse", label: "出品一覧" },
        { key: "mine", label: `自分の出品 (${mine.data?.listings.filter((l) => l.status === "active").length ?? 0})` },
        { key: "sell", label: "出品する" },
      ]} />

      <div style={{ marginTop: 12 }}>
        {tab === "browse" && (
          <>
            <div className="glass pad row-wrap" style={{ marginBottom: 12 }}>
              <input type="search" placeholder="アイテム名で検索" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 200px" }} />
              <select value={sort} onChange={(e) => setSort(e.target.value)} style={{ width: "auto" }}>
                <option value="newest">新着順</option>
                <option value="price_asc">価格が安い順</option>
                <option value="price_desc">価格が高い順</option>
                <option value="rarity">レア度順</option>
              </select>
            </div>
            <ErrorBox error={browse.error} onRetry={browse.reload} />
            {browse.loading && !browse.data ? <Spinner /> : !browse.data?.listings.length ? <Empty icon="💱">出品がありません</Empty> : (
              <div className="grid grid-auto">
                {browse.data.listings.map((l) => (
                  <div key={l.id} className={`item-card t${l.item?.tier ?? 1}`} style={{ cursor: "default" }}>
                    <div className="row" style={{ gap: 10 }}>
                      <ItemIcon visual={l.item?.visual} tier={l.item?.tier ?? 1} size={44} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className={`name r-${l.item?.rarity}`}>{l.item?.name}</div>
                        <div className="odds mono">{fmtOdds(l.item?.odds, l.item?.display_odds)}</div>
                      </div>
                    </div>
                    <div className="row tiny">
                      <UserChip user={l.seller} size={18} />
                      {l.serial && <span className="chip tiny">#{l.serial}</span>}
                    </div>
                    <div className="row" style={{ gap: 6 }}>
                      <button className="btn xs ghost" onClick={() => showHistory(l.item.id)}>相場</button>
                      <button className="btn sm gold" style={{ flex: 1 }} disabled={busy || stardust < l.price} onClick={() => buy(l)}>
                        ✦ {fmtInt(l.price)}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            {browse.data && browse.data.total > 40 && (
              <div className="row center" style={{ justifyContent: "center", marginTop: 14, gap: 8 }}>
                <button className="btn sm ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>前へ</button>
                <span className="mono small">{page} / {Math.ceil(browse.data.total / 40)}</span>
                <button className="btn sm ghost" disabled={page >= Math.ceil(browse.data.total / 40)} onClick={() => setPage((p) => p + 1)}>次へ</button>
              </div>
            )}
          </>
        )}

        {tab === "mine" && (
          mine.loading && !mine.data ? <Spinner /> : !mine.data?.listings.length ? <Empty icon="📦">出品履歴がありません</Empty> : (
            <div className="table-wrap glass">
              <table className="table">
                <thead><tr><th>アイテム</th><th>価格</th><th>状態</th><th>期限</th><th /></tr></thead>
                <tbody>
                  {mine.data.listings.map((l) => (
                    <tr key={l.id}>
                      <td><span className={`r-${l.item?.rarity}`}>{l.item?.name}</span></td>
                      <td className="mono">✦{fmtInt(l.price)}{l.fee > 0 && <span className="tiny faint"> -{fmtInt(l.fee)}</span>}</td>
                      <td>
                        <span className="chip tiny" style={{ color: l.status === "sold" ? "var(--good)" : l.status === "active" ? "var(--accent)" : "var(--text-faint)" }}>
                          {l.status}
                        </span>
                      </td>
                      <td className="tiny faint">{fmtDate(l.sold_at ?? l.expires_at)}</td>
                      <td>{l.status === "active" && <button className="btn xs ghost" disabled={busy} onClick={() => cancel(l)}>取消</button>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}

        {tab === "sell" && <SellPanel onListed={() => { setTab("mine"); mine.reload(); }} />}
      </div>

      <Modal open={!!history} onClose={() => setHistory(null)} title="価格の推移">
        {history?.loading ? <Spinner /> : history && (
          <div className="col" style={{ gap: 10 }}>
            <div className="row" style={{ gap: 10 }}>
              <ItemIcon visual={history.item?.visual} tier={history.item?.tier} size={40} />
              <div><strong className={`r-${history.item?.rarity}`}>{history.item?.name}</strong>
                <div className="tiny muted">参考価格 ✦{fmtInt(history.reference)}（{history.samples}件）</div></div>
            </div>
            {history.sales?.length ? (
              <div className="col" style={{ gap: 3, maxHeight: "40vh", overflow: "auto" }}>
                {history.sales.map((s: any, i: number) => (
                  <div key={i} className="row tiny" style={{ padding: "4px 8px", borderRadius: 7, background: "rgba(255,255,255,0.03)" }}>
                    <span className="mono" style={{ flex: 1 }}>✦{fmtInt(s.price)}</span>
                    <span className="faint">{fmtDate(s.at)}</span>
                  </div>
                ))}
              </div>
            ) : <div className="muted small">取引履歴がありません</div>}
          </div>
        )}
      </Modal>
      {node}
    </div>
  );
}

function SellPanel({ onListed }: { onListed: () => void }) {
  const [group, setGroup] = useState<InventoryGroup | null>(null);
  const [instance, setInstance] = useState<number | null>(null);
  const [price, setPrice] = useState("");
  const { data } = useApi<{ groups: InventoryGroup[] }>("/api/inventory", { per_page: 200, sort: "rarity:desc" });
  const { data: instances } = useApi<{ instances: Instance[] }>(group ? `/api/inventory/item/${group.item.id}` : null);
  const { run, busy } = useAction();

  const sellable = (data?.groups ?? []).filter((g) => g.item.tradeable && g.item.kind !== "admin_artifact");
  const avail = (instances?.instances ?? []).filter((i) => i.state === "owned" && !i.locked && !i.favorite);

  const submit = async () => {
    if (!instance || !price) return;
    const res = await run(() => post("/api/market/list", { instance_id: instance, price: Number(price) }, true),
      { success: "出品しました" });
    if (res) {
      audio.sfx("success");
      setGroup(null);
      setInstance(null);
      setPrice("");
      onListed();
    }
  };

  return (
    <div className="grid grid-2">
      <div className="glass pad col">
        <h3>1. アイテムを選ぶ</h3>
        <div className="col" style={{ gap: 4, maxHeight: "48vh", overflow: "auto" }}>
          {sellable.length === 0 ? <div className="muted small">出品できるアイテムがありません</div> : sellable.map((g) => (
            <button key={g.item.id} className="row" style={{
              gap: 8, padding: "6px 8px", borderRadius: 8, border: "1px solid transparent", textAlign: "left",
              background: group?.item.id === g.item.id ? "rgba(120,160,255,0.16)" : "rgba(255,255,255,0.03)",
              borderColor: group?.item.id === g.item.id ? "var(--accent)" : "transparent", cursor: "pointer",
            }} onClick={() => { setGroup(g); setInstance(null); }}>
              <ItemIcon visual={g.item.visual} tier={g.item.tier} size={26} animate={false} />
              <span className={`r-${g.item.rarity} ellipsis`} style={{ flex: 1 }}>{g.item.name}</span>
              <span className="mono tiny">×{g.count}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="glass pad col">
        <h3>2. 個体と価格</h3>
        {!group ? <div className="muted small">左でアイテムを選択してください</div> : (
          <>
            <div className="row" style={{ gap: 10 }}>
              <ItemIcon visual={group.item.visual} tier={group.item.tier} size={44} />
              <div>
                <strong className={`r-${group.item.rarity}`}>{group.item.name}</strong>
                <div className="tiny muted">売却価格 ✦{fmtInt(group.item.sell_value)}（参考）</div>
              </div>
            </div>
            <div>
              <label>出品する個体</label>
              <select value={instance ?? ""} onChange={(e) => setInstance(Number(e.target.value))}>
                <option value="">選択してください</option>
                {avail.map((i) => <option key={i.id} value={i.id}>{i.serial ? `#${i.serial}` : `id:${i.id}`} · {fmtDate(i.obtained_at)}</option>)}
              </select>
              {avail.length === 0 && <div className="tiny" style={{ color: "var(--warn)" }}>ロック/お気に入り解除が必要です</div>}
            </div>
            <div>
              <label>価格（Stardust）</label>
              <input type="number" min={1} value={price} onChange={(e) => setPrice(e.target.value.replace(/\D/g, ""))} placeholder="1000" inputMode="numeric" />
            </div>
            <div className="tiny muted">手数料5%が売却額から差し引かれます。出品中のアイテムは売却・トレードできません。</div>
            <button className="btn primary" disabled={!instance || !price || busy} onClick={submit}>出品する</button>
          </>
        )}
      </div>
    </div>
  );
}
