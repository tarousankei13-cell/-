import { useState } from "react";
import { post } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { Empty, Modal, Spinner, Tabs, UserChip, useConfirm } from "../../components/ui";
import { fmtDate, fmtInt, fmtLuck, fmtOdds } from "../../lib/format";

export function AdminLogs() {
  const [tab, setTab] = useState<"audit" | "rolls" | "trades" | "market" | "errors">("audit");
  return (
    <div className="col" style={{ gap: 12 }}>
      <Tabs value={tab} onChange={setTab} tabs={[
        { key: "audit", label: "監査ログ" }, { key: "rolls", label: "Rollログ" }, { key: "trades", label: "Trade" },
        { key: "market", label: "Market" }, { key: "errors", label: "エラー" },
      ]} />
      {tab === "audit" && <AuditLog />}
      {tab === "rolls" && <RollLog />}
      {tab === "trades" && <TradeLog />}
      {tab === "market" && <MarketLog />}
      {tab === "errors" && <ErrorLog />}
    </div>
  );
}

function AuditLog() {
  const [filters, setFilters] = useState<{ action?: string; target_user_id?: string; admin_id?: string }>({});
  const { data, loading, reload } = useApi<{ logs: any[] }>("/api/admin/audit", filters);
  const [detail, setDetail] = useState<any>(null);
  return (
    <>
      <div className="glass pad row-wrap">
        <input placeholder="操作名で絞り込み" value={filters.action ?? ""} onChange={(e) => setFilters((f) => ({ ...f, action: e.target.value || undefined }))} style={{ flex: "1 1 160px" }} />
        <input placeholder="対象ユーザーID" value={filters.target_user_id ?? ""} onChange={(e) => setFilters((f) => ({ ...f, target_user_id: e.target.value || undefined }))} style={{ width: 150 }} />
        <input placeholder="管理者ID" value={filters.admin_id ?? ""} onChange={(e) => setFilters((f) => ({ ...f, admin_id: e.target.value || undefined }))} style={{ width: 130 }} />
        <button className="btn sm ghost" onClick={() => reload()}>更新</button>
      </div>
      {loading && !data ? <Spinner /> : !data?.logs.length ? <Empty icon="📜">記録なし</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>日時</th><th>管理者</th><th>操作</th><th>対象</th><th>理由</th><th /></tr></thead>
            <tbody>
              {data.logs.map((l) => (
                <tr key={l.id}>
                  <td className="tiny faint nowrap">{fmtDate(l.created_at)}</td>
                  <td className="tiny">{l.admin_name ?? l.admin_id ?? "system"}{l.admin_mode && <span className="chip tiny" style={{ color: "var(--gold)" }}>AM</span>}</td>
                  <td><span className="mono tiny">{l.action}</span></td>
                  <td className="tiny">{l.target_name ?? l.target_user_id ?? l.entity_id ?? "—"}</td>
                  <td className="tiny ellipsis" style={{ maxWidth: 200 }}>{l.reason}</td>
                  <td><button className="btn xs ghost" onClick={() => setDetail(l)}>詳細</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail && (
        <Modal open onClose={() => setDetail(null)} title={`監査ログ #${detail.id}`}>
          <div className="col" style={{ gap: 8 }}>
            <div className="row-wrap tiny">
              <span className="chip">{detail.action}</span>
              <span className="chip">{fmtDate(detail.created_at)}</span>
              {detail.ip && <span className="chip mono">{detail.ip}</span>}
              {detail.session && <span className="chip mono">sess:{detail.session}</span>}
            </div>
            <div><strong className="small">理由</strong><div className="small">{detail.reason || "—"}</div></div>
            {detail.old && <div><strong className="small">変更前</strong><div className="json-view">{JSON.stringify(detail.old, null, 2)}</div></div>}
            {detail.new && <div><strong className="small">変更後</strong><div className="json-view">{JSON.stringify(detail.new, null, 2)}</div></div>}
            {detail.user_agent && <div className="tiny faint">{detail.user_agent}</div>}
          </div>
        </Modal>
      )}
    </>
  );
}

function RollLog() {
  const [f, setF] = useState<{ user_id?: string; item_key?: string; min_tier?: string }>({ min_tier: "4" });
  const { data, loading, reload } = useApi<{ rolls: any[] }>("/api/admin/rolls", f);
  return (
    <>
      <div className="glass pad row-wrap">
        <input placeholder="ユーザーID" value={f.user_id ?? ""} onChange={(e) => setF((x) => ({ ...x, user_id: e.target.value || undefined }))} style={{ width: 130 }} />
        <input placeholder="アイテムkey" value={f.item_key ?? ""} onChange={(e) => setF((x) => ({ ...x, item_key: e.target.value || undefined }))} style={{ flex: "1 1 160px" }} />
        <select value={f.min_tier ?? ""} onChange={(e) => setF((x) => ({ ...x, min_tier: e.target.value || undefined }))} style={{ width: "auto" }}>
          <option value="">全レア度</option>
          {[2, 3, 4, 5, 6, 7, 8].map((t) => <option key={t} value={t}>Tier {t}+</option>)}
        </select>
        <button className="btn sm ghost" onClick={() => reload()}>更新</button>
      </div>
      {loading && !data ? <Spinner /> : !data?.rolls.length ? <Empty icon="🎲">記録なし</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>日時</th><th>プレイヤー</th><th>アイテム</th><th>Luck</th><th>確率</th><th>Biome</th><th>Ver</th></tr></thead>
            <tbody>
              {data.rolls.map((r) => (
                <tr key={r.id}>
                  <td className="tiny faint nowrap">{fmtDate(r.created_at)}</td>
                  <td><UserChip user={r.user} size={18} /></td>
                  <td><span className={`r-${r.item?.rarity}`}>{r.item?.name}</span></td>
                  <td className="mono tiny">{fmtLuck(r.luck)}</td>
                  <td className="mono tiny">{fmtOdds(r.base_odds)} → {r.final_chance ? fmtOdds(1 / r.final_chance) : "—"}</td>
                  <td className="tiny">{r.biome}{r.state ? `/${r.state}` : ""}</td>
                  <td className="tiny faint mono">r{r.rng_version}/c{r.content_version}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function TradeLog() {
  const [f, setF] = useState<{ user_id?: string; item_key?: string; instance_id?: string }>({});
  const { data, loading, reload } = useApi<{ trades: any[] }>("/api/admin/trades", f);
  return (
    <>
      <div className="glass pad row-wrap">
        <input placeholder="ユーザーID" value={f.user_id ?? ""} onChange={(e) => setF((x) => ({ ...x, user_id: e.target.value || undefined }))} style={{ width: 130 }} />
        <input placeholder="アイテムkey" value={f.item_key ?? ""} onChange={(e) => setF((x) => ({ ...x, item_key: e.target.value || undefined }))} style={{ flex: "1 1 150px" }} />
        <input placeholder="インスタンスID" value={f.instance_id ?? ""} onChange={(e) => setF((x) => ({ ...x, instance_id: e.target.value || undefined }))} style={{ width: 140 }} />
        <button className="btn sm ghost" onClick={() => reload()}>更新</button>
      </div>
      {loading && !data ? <Spinner /> : !data?.trades.length ? <Empty icon="🤝">記録なし</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>ID</th><th>From</th><th>To</th><th>内容</th><th>状態</th><th>日時</th></tr></thead>
            <tbody>
              {data.trades.map((t) => (
                <tr key={t.id}>
                  <td className="mono tiny">#{t.id}</td>
                  <td><UserChip user={t.from} size={18} /></td>
                  <td><UserChip user={t.to} size={18} /></td>
                  <td className="tiny">{t.offer.items.length}品/✦{fmtInt(t.offer.stardust)} ⇄ {t.request.items.length}品/✦{fmtInt(t.request.stardust)}</td>
                  <td><span className="chip tiny">{t.status}</span></td>
                  <td className="tiny faint">{fmtDate(t.completed_at ?? t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function MarketLog() {
  const [flagged, setFlagged] = useState(true);
  const { data, loading, reload } = useApi<{ listings: any[] }>("/api/admin/market", { flagged });
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const cancel = async (id: number) => {
    const ok = await confirm("出品を強制取消", "この出品を取り消します（監査ログに記録）", { danger: true });
    if (!ok) return;
    if (await run(() => post("/api/admin/market/cancel", { listing_id: id, reason: "管理者による取消" }), { success: "取り消しました" })) reload();
  };
  return (
    <>
      <div className="glass pad row-wrap">
        <label className="switch"><input type="checkbox" checked={flagged} onChange={(e) => setFlagged(e.target.checked)} /><span className="track" /><span className="tiny">異常検知のみ</span></label>
        <button className="btn sm ghost" onClick={() => reload()}>更新</button>
      </div>
      {loading && !data ? <Spinner /> : !data?.listings.length ? <Empty icon="💱">該当なし</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>ID</th><th>出品者</th><th>アイテム</th><th>価格</th><th>状態</th><th>検知理由</th><th /></tr></thead>
            <tbody>
              {data.listings.map((l) => (
                <tr key={l.id} style={l.flagged ? { background: "rgba(255,193,77,0.08)" } : undefined}>
                  <td className="mono tiny">#{l.id}</td>
                  <td><UserChip user={l.seller} size={18} /></td>
                  <td className={`r-${l.item?.rarity}`}>{l.item?.name}</td>
                  <td className="mono">✦{fmtInt(l.price)}</td>
                  <td><span className="chip tiny">{l.status}</span></td>
                  <td className="tiny" style={{ color: "var(--warn)" }}>{l.flag_reason ?? "—"}</td>
                  <td>{l.status === "active" && <button className="btn xs danger" disabled={busy} onClick={() => cancel(l.id)}>取消</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {node}
    </>
  );
}

function ErrorLog() {
  const { data, loading, reload } = useApi<{ errors: any[] }>("/api/admin/errors");
  const [detail, setDetail] = useState<any>(null);
  return (
    <>
      <div className="glass pad row"><button className="btn sm ghost" onClick={() => reload()}>更新</button></div>
      {loading && !data ? <Spinner /> : !data?.errors.length ? <Empty icon="✅">エラーはありません</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>日時</th><th>Error ID</th><th>パス</th><th>種類</th><th>メッセージ</th><th /></tr></thead>
            <tbody>
              {data.errors.map((e) => (
                <tr key={e.id}>
                  <td className="tiny faint nowrap">{fmtDate(e.created_at)}</td>
                  <td className="mono tiny">{e.error_id}</td>
                  <td className="mono tiny">{e.method} {e.path}</td>
                  <td className="tiny" style={{ color: "var(--bad)" }}>{e.type}</td>
                  <td className="tiny ellipsis" style={{ maxWidth: 240 }}>{e.message}</td>
                  <td><button className="btn xs ghost" onClick={() => setDetail(e)}>詳細</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail && (
        <Modal open onClose={() => setDetail(null)} title={`Error ${detail.error_id}`} wide>
          <div className="col" style={{ gap: 8 }}>
            <div className="row-wrap tiny">
              <span className="chip mono">{detail.method} {detail.path}</span>
              <span className="chip">{fmtDate(detail.created_at)}</span>
              {detail.user_id && <span className="chip">user #{detail.user_id}</span>}
            </div>
            <div style={{ color: "var(--bad)" }}>{detail.type}: {detail.message}</div>
            <div className="json-view" style={{ maxHeight: 400 }}>{detail.traceback}</div>
          </div>
        </Modal>
      )}
    </>
  );
}
