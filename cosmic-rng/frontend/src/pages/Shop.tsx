import { useState } from "react";
import { post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";
import { ItemIcon } from "../components/ItemIcon";
import { Empty, ErrorBox, LockedFeature, Spinner, useConfirm } from "../components/ui";
import { fmtCompact, fmtInt } from "../lib/format";

interface Product {
  key: string; name: string; description: string; product_type: string; product_key: string; quantity: number;
  price: number; limit: number | null; period: string | null; remaining: number | null; locked_reason: string | null;
  owned: boolean; rarity: string; visual: any; detail: string; luck_bonus?: number; speed_bonus?: number; slot?: string;
}
interface ShopData {
  shops: { key: string; name: string; description: string; biome_key: string | null; available: boolean; items: Product[]; locked_reason?: string }[];
  biome: string;
  stardust: number;
}

export function Shop() {
  const { data, loading, error, locked, reload } = useApi<ShopData>("/api/shop");
  const [qty, setQty] = useState<Record<string, number>>({});
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const setStardust = useGame((s) => s.setStardust);
  const toast = useGame((s) => s.toast);
  const refreshHud = useGame((s) => s.refreshHud);
  const stardust = useGame((s) => s.hud?.stardust ?? 0);

  if (locked !== null) return <LockedFeature feature="shop" />;

  const buy = async (p: Product) => {
    const n = qty[p.key] ?? 1;
    const total = p.price * n;
    const ok = await confirm("購入の確認", (
      <div className="col">
        <div className="row" style={{ gap: 10 }}>
          <ItemIcon visual={p.visual} size={40} tier={3} />
          <div><strong>{p.name}</strong> ×{n * p.quantity}<div className="tiny muted">{p.description || p.detail}</div></div>
        </div>
        <div className="row"><span style={{ flex: 1 }}>合計</span><strong className="mono" style={{ color: "var(--gold)" }}>✦ {fmtInt(total)}</strong></div>
        <div className="row tiny muted"><span style={{ flex: 1 }}>購入後の残高</span><span className="mono">✦ {fmtInt(stardust - total)}</span></div>
      </div>
    ));
    if (!ok) return;
    const res = await run(() => post<{ spent: number; stardust: number; delivered: any }>("/api/shop/purchase", { shop_item_key: p.key, quantity: n }, true));
    if (res) {
      audio.sfx("coin");
      setStardust(res.stardust);
      toast(`${p.name} を購入しました`, "success", `✦ -${fmtInt(res.spent)}`);
      reload();
      refreshHud();
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Shop</h1>
          <div className="sub">Boost・装備・アップグレードを Stardust で購入。Biome限定ショップはそのBiome中のみ開きます。</div>
        </div>
        <span className="chip mono" style={{ color: "var(--gold)" }}>✦ {fmtCompact(stardust)}</span>
      </div>

      <ErrorBox error={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : (data?.shops ?? []).map((shop) => (
        <section key={shop.key} className="glass pad" style={{ marginBottom: 14, opacity: shop.available ? 1 : 0.55 }}>
          <div className="row" style={{ marginBottom: 10 }}>
            <div style={{ flex: 1 }}>
              <h2>{shop.name}</h2>
              <div className="small muted">{shop.description}</div>
            </div>
            {shop.biome_key && (
              <span className="chip tiny" style={{ color: shop.available ? "var(--good)" : "var(--warn)" }}>
                {shop.available ? "OPEN" : `${shop.biome_key} 限定`}
              </span>
            )}
          </div>
          {!shop.available ? (
            <div className="muted small">このショップは {shop.biome_key} Biome の間だけ出現します。</div>
          ) : shop.items.length === 0 ? (
            <div className="muted small">商品がありません</div>
          ) : (
            <div className="grid grid-auto">
              {shop.items.map((p) => {
                const disabled = !!p.locked_reason || p.owned || (p.remaining !== null && p.remaining <= 0) || stardust < p.price;
                return (
                  <div key={p.key} className={`item-card t${p.rarity === "legendary" ? 4 : p.rarity === "epic" ? 3 : 1}`} style={{ cursor: "default" }}>
                    <div className="row" style={{ gap: 10 }}>
                      <ItemIcon visual={p.visual} size={42} tier={p.rarity === "legendary" ? 4 : 2} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className={`name r-${p.rarity}`}>{p.name}</div>
                        <div className="tiny muted">{p.description || p.detail}</div>
                      </div>
                    </div>
                    <div className="row-wrap tiny" style={{ gap: 5 }}>
                      {p.product_type === "unlock" && <span className="chip tiny">アップグレード</span>}
                      {p.quantity > 1 && <span className="chip tiny">×{p.quantity}</span>}
                      {p.remaining !== null && <span className="chip tiny">{p.period === "daily" ? "本日" : "残り"} {p.remaining}回</span>}
                      {p.luck_bonus ? <span className="chip tiny" style={{ color: "var(--good)" }}>Luck +{(p.luck_bonus * 100).toFixed(0)}%</span> : null}
                      {p.speed_bonus ? <span className="chip tiny" style={{ color: "var(--good)" }}>速度 +{(p.speed_bonus * 100).toFixed(0)}%</span> : null}
                    </div>
                    <div className="row" style={{ gap: 6 }}>
                      {p.product_type !== "unlock" && (
                        <select value={qty[p.key] ?? 1} onChange={(e) => setQty((q) => ({ ...q, [p.key]: Number(e.target.value) }))}
                          style={{ width: 66, minHeight: 32, padding: "3px 6px" }} disabled={disabled}>
                          {[1, 5, 10, 25].filter((n) => p.remaining === null || n <= p.remaining).map((n) => <option key={n} value={n}>{n}</option>)}
                        </select>
                      )}
                      <button className={`btn sm ${disabled ? "" : "gold"}`} style={{ flex: 1 }} disabled={disabled || busy} onClick={() => buy(p)}>
                        {p.owned ? "所持済み" : p.locked_reason ? p.locked_reason : `✦ ${fmtInt(p.price * (qty[p.key] ?? 1))}`}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>
      ))}
      {!loading && !data?.shops.length && <Empty icon="🛒">ショップがありません</Empty>}
      {node}
    </div>
  );
}
