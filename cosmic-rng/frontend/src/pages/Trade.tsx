import { useEffect, useState } from "react";
import { get, post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { events, useGame } from "../store/game";
import { audio } from "../audio/engine";
import { ItemIcon } from "../components/ItemIcon";
import { Avatar, Empty, ErrorBox, LockedFeature, Modal, Spinner, Tabs, UserChip, useConfirm } from "../components/ui";
import { fmtDate, fmtInt } from "../lib/format";
import type { Instance, InventoryGroup, UserBrief } from "../lib/types";

interface TradeSide { items: { instance_id: number; item_id: number; serial: number | null; item: any }[]; stardust: number }
interface TradeT {
  id: number; status: string; revision: number; message: string | null; from: UserBrief; to: UserBrief;
  offer: TradeSide; request: TradeSide; created_at: string; expires_at: string; completed_at: string | null;
}

export function Trade() {
  const [tab, setTab] = useState<"inbox" | "sent" | "new" | "gift">("inbox");
  const me = useGame((s) => s.me);
  const { data, loading, error, locked, reload } = useApi<{ trades: TradeT[]; unlocked: boolean; unlock_level: number }>("/api/trades");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const [detail, setDetail] = useState<TradeT | null>(null);
  const refreshHud = useGame((s) => s.refreshHud);

  useEffect(() => events.on("trade", () => reload(true)), [reload]);

  if (locked !== null) return <LockedFeature feature="trade" />;

  const inbox = (data?.trades ?? []).filter((t) => t.to.id === me?.user.id && t.status === "pending");
  const sent = (data?.trades ?? []).filter((t) => t.from.id === me?.user.id || t.status !== "pending");

  const respond = async (t: TradeT, action: "accept" | "decline" | "cancel") => {
    if (action === "accept") {
      const ok = await confirm("トレードの確認", <TradeSummary t={t} me={me?.user.id ?? 0} />, { danger: false });
      if (!ok) return;
    }
    const res = await run(() => post(`/api/trades/${t.id}/respond`, { action, revision: t.revision }, true));
    if (res) {
      audio.sfx(action === "accept" ? "success" : "click");
      setDetail(null);
      reload();
      refreshHud();
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>取引<span className="h1-en">Trade</span></h1>
          <div className="sub">1対1の交換。内容が変更された提案は承認できません（revision照合）。</div>
        </div>
      </div>

      <Tabs value={tab} onChange={setTab} tabs={[
        { key: "inbox", label: "受信", badge: inbox.length || undefined },
        { key: "sent", label: "履歴" },
        { key: "new", label: "提案する" },
        { key: "gift", label: "Gift" },
      ]} />

      <div style={{ marginTop: 12 }}>
        <ErrorBox error={error} onRetry={reload} />
        {tab === "inbox" && (loading && !data ? <Spinner /> : inbox.length === 0 ? <Empty icon="📭">受信中のトレードはありません</Empty> : (
          <div className="col" style={{ gap: 10 }}>
            {inbox.map((t) => (
              <div key={t.id} className="glass pad col" style={{ gap: 10 }}>
                <div className="row"><UserChip user={t.from} /><span className="spacer" /><span className="tiny faint">期限 {fmtDate(t.expires_at)}</span></div>
                <TradeSummary t={t} me={me?.user.id ?? 0} />
                {t.message && <div className="small" style={{ fontStyle: "italic" }}>「{t.message}」</div>}
                <div className="row" style={{ gap: 8 }}>
                  <button className="btn sm primary" disabled={busy} onClick={() => respond(t, "accept")}>承認する</button>
                  <button className="btn sm ghost" disabled={busy} onClick={() => respond(t, "decline")}>拒否</button>
                  <button className="btn sm ghost" onClick={() => setDetail(t)}>詳細</button>
                </div>
              </div>
            ))}
          </div>
        ))}

        {tab === "sent" && (loading && !data ? <Spinner /> : sent.length === 0 ? <Empty icon="📜">履歴がありません</Empty> : (
          <div className="table-wrap glass">
            <table className="table">
              <thead><tr><th>相手</th><th>内容</th><th>状態</th><th>日時</th><th /></tr></thead>
              <tbody>
                {sent.map((t) => {
                  const other = t.from.id === me?.user.id ? t.to : t.from;
                  return (
                    <tr key={t.id}>
                      <td><UserChip user={other} size={18} /></td>
                      <td className="tiny">{t.offer.items.length}品 / ✦{fmtInt(t.offer.stardust)} ⇄ {t.request.items.length}品 / ✦{fmtInt(t.request.stardust)}</td>
                      <td><span className="chip tiny" style={{ color: t.status === "accepted" ? "var(--good)" : t.status === "pending" ? "var(--accent)" : "var(--text-faint)" }}>{t.status}</span></td>
                      <td className="tiny faint">{fmtDate(t.completed_at ?? t.created_at)}</td>
                      <td>
                        {t.status === "pending" && t.from.id === me?.user.id && <button className="btn xs ghost" disabled={busy} onClick={() => respond(t, "cancel")}>取消</button>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))}

        {tab === "new" && <NewTrade onSent={() => { setTab("sent"); reload(); }} />}
        {tab === "gift" && <GiftPanel />}
      </div>

      {detail && (
        <Modal open onClose={() => setDetail(null)} title={`Trade #${detail.id}`}>
          <TradeSummary t={detail} me={me?.user.id ?? 0} detailed />
        </Modal>
      )}
      {node}
    </div>
  );
}

function TradeSummary({ t, me, detailed }: { t: TradeT; me: number; detailed?: boolean }) {
  const iAmSender = t.from.id === me;
  const give = iAmSender ? t.offer : t.request;
  const receive = iAmSender ? t.request : t.offer;
  return (
    <div className="grid grid-2">
      <div className="glass-2 pad-sm col">
        <div className="tiny faint">渡すもの</div>
        {give.items.length === 0 && give.stardust === 0 && <span className="muted small">なし</span>}
        {give.stardust > 0 && <span className="chip" style={{ color: "var(--gold)" }}>✦ {fmtInt(give.stardust)}</span>}
        {give.items.map((i) => (
          <div key={i.instance_id} className="row tiny" style={{ gap: 6 }}>
            <ItemIcon visual={i.item?.visual} tier={i.item?.tier ?? 1} size={18} animate={false} />
            <span className={`r-${i.item?.rarity} ellipsis`} style={{ flex: 1 }}>{i.item?.name}</span>
            {detailed && i.serial && <span className="mono faint">#{i.serial}</span>}
          </div>
        ))}
      </div>
      <div className="glass-2 pad-sm col">
        <div className="tiny faint" style={{ color: "var(--good)" }}>受け取るもの</div>
        {receive.items.length === 0 && receive.stardust === 0 && <span className="muted small">なし</span>}
        {receive.stardust > 0 && <span className="chip" style={{ color: "var(--gold)" }}>✦ {fmtInt(receive.stardust)}</span>}
        {receive.items.map((i) => (
          <div key={i.instance_id} className="row tiny" style={{ gap: 6 }}>
            <ItemIcon visual={i.item?.visual} tier={i.item?.tier ?? 1} size={18} animate={false} />
            <span className={`r-${i.item?.rarity} ellipsis`} style={{ flex: 1 }}>{i.item?.name}</span>
            {detailed && i.serial && <span className="mono faint">#{i.serial}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

function UserSearch({ onPick, label = "相手を検索" }: { onPick: (u: UserBrief) => void; label?: string }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<UserBrief[]>([]);
  useEffect(() => {
    if (q.length < 1) {
      setResults([]);
      return;
    }
    const t = window.setTimeout(async () => {
      try {
        const r = await get<{ users: UserBrief[] }>("/api/users/search", { q });
        setResults(r.users);
      } catch {
        setResults([]);
      }
    }, 300);
    return () => window.clearTimeout(t);
  }, [q]);
  return (
    <div className="col">
      <label>{label}</label>
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="プレイヤー名 または ID" />
      {results.length > 0 && (
        <div className="col" style={{ gap: 3, maxHeight: 180, overflow: "auto" }}>
          {results.map((u) => (
            <button key={u.id} className="row" style={{ gap: 8, padding: "5px 8px", borderRadius: 8, background: "rgba(255,255,255,0.04)", cursor: "pointer", border: "1px solid transparent", textAlign: "left" }}
              onClick={() => { onPick(u); setQ(""); setResults([]); }}>
              <Avatar user={u} size={22} />
              <span style={{ flex: 1 }}>{u.name}</span>
              <span className="tiny faint">Lv.{u.level}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function InstancePicker({ instances, selected, onToggle, title }: {
  instances: Instance[]; selected: Set<number>; onToggle: (id: number) => void; title: string;
}) {
  return (
    <div className="col" style={{ gap: 4 }}>
      <div className="tiny faint">{title}</div>
      <div className="col" style={{ gap: 3, maxHeight: 220, overflow: "auto" }}>
        {instances.length === 0 ? <span className="muted small">対象アイテムがありません</span> : instances.map((i) => (
          <label key={i.id} className="row" style={{ gap: 7, padding: "4px 7px", borderRadius: 7, background: selected.has(i.id) ? "rgba(120,160,255,0.16)" : "rgba(255,255,255,0.03)" }}>
            <input type="checkbox" checked={selected.has(i.id)} onChange={() => onToggle(i.id)} />
            <ItemIcon visual={i.item?.visual} tier={i.item?.tier ?? 1} size={18} animate={false} />
            <span className={`r-${i.item?.rarity} ellipsis tiny`} style={{ flex: 1 }}>{i.item?.name}</span>
            {i.serial && <span className="mono tiny faint">#{i.serial}</span>}
          </label>
        ))}
      </div>
    </div>
  );
}

function NewTrade({ onSent }: { onSent: () => void }) {
  const [target, setTarget] = useState<UserBrief | null>(null);
  const [offerItems, setOfferItems] = useState<Set<number>>(new Set());
  const [requestItems, setRequestItems] = useState<Set<number>>(new Set());
  const [offerSd, setOfferSd] = useState("");
  const [requestSd, setRequestSd] = useState("");
  const [message, setMessage] = useState("");
  const { run, busy } = useAction();
  const { data: myInv } = useApi<{ groups: InventoryGroup[] }>("/api/inventory", { per_page: 200 });
  const [myInstances, setMyInstances] = useState<Instance[]>([]);
  const { data: theirs } = useApi<{ instances: Instance[]; private: boolean }>(target ? `/api/partner-inventory/${target.id}` : null);

  useEffect(() => {
    (async () => {
      const groups = (myInv?.groups ?? []).filter((g) => g.item.tradeable && g.item.kind !== "admin_artifact").slice(0, 40);
      const all: Instance[] = [];
      for (const g of groups) {
        try {
          const r = await get<{ instances: Instance[] }>(`/api/inventory/item/${g.item.id}`, { per_page: 20 });
          all.push(...r.instances.filter((i) => i.state === "owned" && !i.locked));
        } catch {
          /* ignore */
        }
      }
      setMyInstances(all);
    })();
  }, [myInv]);

  const toggle = (set: Set<number>, setter: (s: Set<number>) => void) => (id: number) => {
    const n = new Set(set);
    n.has(id) ? n.delete(id) : n.add(id);
    setter(n);
  };

  const submit = async () => {
    if (!target) return;
    const res = await run(() => post("/api/trades", {
      to_user_id: target.id,
      offer_items: [...offerItems], offer_stardust: Number(offerSd || 0),
      request_items: [...requestItems], request_stardust: Number(requestSd || 0),
      message: message || null,
    }, true), { success: "トレードを提案しました" });
    if (res) {
      audio.sfx("success");
      onSent();
    }
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad">
        <UserSearch onPick={setTarget} />
        {target && (
          <div className="row" style={{ marginTop: 8, gap: 8 }}>
            <Avatar user={target} size={28} />
            <strong>{target.name}</strong>
            <button className="btn xs ghost" onClick={() => { setTarget(null); setRequestItems(new Set()); }}>変更</button>
          </div>
        )}
      </div>

      {target && (
        <>
          <div className="grid grid-2">
            <div className="glass pad col">
              <h3>あなたが渡すもの</h3>
              <InstancePicker instances={myInstances} selected={offerItems} onToggle={toggle(offerItems, setOfferItems)} title="アイテム（ロック中は除外）" />
              <div><label>Stardust</label><input type="number" min={0} value={offerSd} onChange={(e) => setOfferSd(e.target.value.replace(/\D/g, ""))} /></div>
            </div>
            <div className="glass pad col">
              <h3>相手に求めるもの</h3>
              {theirs?.private ? <div className="muted small">相手はインベントリを非公開にしています</div> : (
                <InstancePicker instances={theirs?.instances ?? []} selected={requestItems} onToggle={toggle(requestItems, setRequestItems)} title="相手のアイテム" />
              )}
              <div><label>Stardust</label><input type="number" min={0} value={requestSd} onChange={(e) => setRequestSd(e.target.value.replace(/\D/g, ""))} /></div>
            </div>
          </div>
          <div className="glass pad col">
            <div><label>メッセージ（任意）</label><input value={message} onChange={(e) => setMessage(e.target.value)} maxLength={200} /></div>
            <button className="btn primary" disabled={busy || (!offerItems.size && !requestItems.size && !offerSd && !requestSd)} onClick={submit}>
              トレードを提案する
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function GiftPanel() {
  const [target, setTarget] = useState<UserBrief | null>(null);
  const [stardust, setStardust] = useState("");
  const [items, setItems] = useState<Set<number>>(new Set());
  const [message, setMessage] = useState("");
  const { run, busy } = useAction();
  const { data: history, reload } = useApi<{ gifts: any[] }>("/api/gifts");
  const { data: myInv } = useApi<{ groups: InventoryGroup[] }>("/api/inventory", { per_page: 60 });
  const [myInstances, setMyInstances] = useState<Instance[]>([]);
  const refreshHud = useGame((s) => s.refreshHud);
  const me = useGame((s) => s.me);

  useEffect(() => {
    (async () => {
      const groups = (myInv?.groups ?? []).filter((g) => g.item.tradeable && g.item.kind !== "admin_artifact").slice(0, 25);
      const all: Instance[] = [];
      for (const g of groups) {
        try {
          const r = await get<{ instances: Instance[] }>(`/api/inventory/item/${g.item.id}`, { per_page: 8 });
          all.push(...r.instances.filter((i) => i.state === "owned" && !i.locked));
        } catch {
          /* ignore */
        }
      }
      setMyInstances(all);
    })();
  }, [myInv]);

  const submit = async () => {
    if (!target) return;
    const res = await run(() => post("/api/gifts", {
      to_user_id: target.id, instance_ids: [...items], stardust: Number(stardust || 0), message: message || null,
    }, true), { success: "贈り物を送りました" });
    if (res) {
      audio.sfx("success");
      setItems(new Set());
      setStardust("");
      setMessage("");
      reload();
      refreshHud();
    }
  };

  return (
    <div className="grid grid-2">
      <div className="glass pad col">
        <h3>Gift を贈る</h3>
        <UserSearch onPick={setTarget} label="贈る相手" />
        {target && <div className="row"><Avatar user={target} size={24} /><strong>{target.name}</strong></div>}
        <InstancePicker instances={myInstances} selected={items} onToggle={(id) => setItems((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; })} title="アイテム（最大10個）" />
        <div><label>Stardust</label><input type="number" min={0} value={stardust} onChange={(e) => setStardust(e.target.value.replace(/\D/g, ""))} /></div>
        <div><label>メッセージ</label><input value={message} onChange={(e) => setMessage(e.target.value)} maxLength={200} /></div>
        <div className="tiny muted">クールダウンと1日の上限があります。Admin Artifactは贈れません。</div>
        <button className="btn primary" disabled={!target || busy || (items.size === 0 && !stardust)} onClick={submit}>贈る</button>
      </div>
      <div className="glass pad col">
        <h3>履歴</h3>
        {!history?.gifts.length ? <div className="muted small">まだありません</div> : (
          <div className="col" style={{ gap: 4, maxHeight: "50vh", overflow: "auto" }}>
            {history.gifts.map((g) => (
              <div key={g.id} className="row tiny" style={{ gap: 7, padding: "5px 8px", borderRadius: 8, background: "rgba(255,255,255,0.03)" }}>
                <span className="chip tiny">{g.from === me?.user.id ? "送信" : "受信"}</span>
                <span className="ellipsis" style={{ flex: 1 }}>{g.item?.name ?? `✦${fmtInt(g.stardust)}`}</span>
                <span className="faint">{fmtDate(g.created_at)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
