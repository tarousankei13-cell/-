import { useEffect, useState } from "react";
import { get, post } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { Avatar, Empty, Modal, Spinner, Tabs, useConfirm } from "../../components/ui";
import { ItemIcon } from "../../components/ItemIcon";
import { fmtCompact, fmtDate, fmtInt, fmtLuck, fmtOdds } from "../../lib/format";
import type { Bootstrap } from "./Admin";

interface ActionDef {
  key: string;
  label: string;
  danger?: boolean;
  fields: { name: string; label: string; type: "text" | "number" | "select" | "checkbox"; options?: { value: string; label: string }[]; placeholder?: string; required?: boolean }[];
}

export function AdminUsers({ boot }: { boot: Bootstrap }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const { data, loading, reload } = useApi<{ users: any[] }>("/api/admin/users", { q: debounced });

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(q), 280);
    return () => window.clearTimeout(t);
  }, [q]);

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad row-wrap">
        <input type="search" placeholder="名前 / ユーザーID / Discord ID で検索" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 260px" }} />
        <button className="btn sm ghost" onClick={() => reload()}>更新</button>
      </div>

      {loading && !data ? <Spinner /> : !data?.users.length ? <Empty icon="👥">該当なし</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>プレイヤー</th><th>Lv</th><th>Rolls</th><th>Stardust</th><th>状態</th><th>最終ログイン</th><th /></tr></thead>
            <tbody>
              {data.users.map((u) => (
                <tr key={u.id}>
                  <td>
                    <div className="row" style={{ gap: 7 }}>
                      <Avatar user={u} size={22} />
                      <div>
                        <div>{u.name}</div>
                        <div className="tiny faint mono">#{u.id} · {u.discord_id}</div>
                      </div>
                    </div>
                  </td>
                  <td className="mono">{u.level}</td>
                  <td className="mono">{fmtCompact(u.total_rolls)}</td>
                  <td className="mono">✦{fmtCompact(u.stardust)}</td>
                  <td>
                    <span className="chip tiny" style={{ color: u.status === "active" ? "var(--good)" : u.status === "frozen" ? "var(--warn)" : "var(--bad)" }}>{u.status}</span>
                    {u.role === "admin" && <span className="chip tiny" style={{ color: "var(--gold)" }}>admin</span>}
                  </td>
                  <td className="tiny faint">{fmtDate(u.last_seen_at)}</td>
                  <td><button className="btn xs" onClick={() => setSelectedId(u.id)}>管理</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId && <UserDetail userId={selectedId} boot={boot} onClose={() => setSelectedId(null)} onChanged={reload} />}
    </div>
  );
}

function UserDetail({ userId, boot, onClose, onChanged }: { userId: number; boot: Bootstrap; onClose: () => void; onChanged: () => void }) {
  const { data, loading, reload } = useApi<any>(`/api/admin/users/${userId}`);
  const [tab, setTab] = useState<"overview" | "actions" | "inventory" | "rolls" | "audit">("overview");
  const [table, setTable] = useState<any>(null);

  const actions: ActionDef[] = [
    { key: "give_item", label: "アイテム付与", fields: [
      { name: "item_key", label: "アイテム", type: "select", options: boot.items.map((i) => ({ value: i.key, label: `${i.name} (${i.rarity})` })), required: true },
      { name: "qty", label: "個数", type: "number", placeholder: "1" }] },
    { key: "give_boost_items", label: "Boost付与", fields: [
      { name: "boost_key", label: "Boost", type: "select", options: boot.boosts.map((b) => ({ value: b.key, label: b.name })), required: true },
      { name: "qty", label: "個数", type: "number", placeholder: "1" }] },
    { key: "set_base_luck", label: "Base Luck変更", fields: [{ name: "value", label: "値", type: "number", placeholder: "1.0", required: true }] },
    { key: "set_biome", label: "Biome変更", fields: [
      { name: "biome_key", label: "Biome", type: "select", options: boot.biomes.map((b) => ({ value: b.key, label: `${b.name} (${b.kind})` })), required: true },
      { name: "duration", label: "継続秒数（空=既定）", type: "number" }] },
    { key: "force_next_item", label: "次回Roll結果を指定", fields: [
      { name: "item_key", label: "アイテム", type: "select", options: boot.items.map((i) => ({ value: i.key, label: i.name })), required: true },
      { name: "rolls", label: "適用Roll数", type: "number", placeholder: "1" }] },
    { key: "set_item_chance", label: "特定アイテムの確率変更", fields: [
      { name: "item_key", label: "アイテム", type: "select", options: boot.items.map((i) => ({ value: i.key, label: i.name })), required: true },
      { name: "mult", label: "倍率", type: "number", placeholder: "10", required: true },
      { name: "rolls", label: "Roll数（空=時間）", type: "number" },
      { name: "duration", label: "秒数", type: "number" }] },
    { key: "set_rarity_chance", label: "レア度の確率変更", fields: [
      { name: "tier", label: "レア度", type: "select", options: ["common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic"].map((t) => ({ value: t, label: t })), required: true },
      { name: "mult", label: "倍率", type: "number", placeholder: "5", required: true },
      { name: "duration", label: "秒数", type: "number" }] },
    { key: "add_effect", label: "任意の効果を付与", fields: [
      { name: "effect_type", label: "効果タイプ", type: "select", options: boot.effect_types.map((e) => ({ value: e, label: e })), required: true },
      { name: "value", label: "値", type: "number", placeholder: "100" },
      { name: "name", label: "表示名", type: "text", placeholder: "Admin Blessing" },
      { name: "stack_mode", label: "重複方式", type: "select", options: ["add", "multiply", "queue", "highest"].map((s) => ({ value: s, label: s })) },
      { name: "rolls", label: "Roll数", type: "number" },
      { name: "duration", label: "秒数", type: "number" }] },
    { key: "clear_effects", label: "効果をすべて解除", fields: [] },
    { key: "adjust_stardust", label: "Stardust増減", danger: true, fields: [{ name: "delta", label: "増減値（負数可）", type: "number", required: true }] },
    { key: "reset_cooldowns", label: "クールダウンをリセット", fields: [] },
    { key: "freeze", label: "アカウント凍結", danger: true, fields: [{ name: "hours", label: "時間（空=無期限）", type: "number" }] },
    { key: "ban", label: "BAN", danger: true, fields: [{ name: "hours", label: "時間（空=無期限）", type: "number" }] },
    { key: "unrestrict", label: "制限解除", fields: [] },
    { key: "revoke_sessions", label: "全セッション無効化", danger: true, fields: [] },
    { key: "set_role", label: "ロール変更（スーパー管理者のみ）", danger: true, fields: [
      { name: "role", label: "ロール", type: "select", options: [{ value: "player", label: "player" }, { value: "admin", label: "admin" }], required: true }] },
  ];

  const loadTable = async () => {
    setTable({ loading: true });
    try {
      setTable(await get(`/api/admin/users/${userId}/table`));
    } catch (e) {
      setTable({ error: (e as Error).message });
    }
  };

  const u = data?.user;
  return (
    <Modal open onClose={onClose} wide title={u ? <span className="row" style={{ gap: 8 }}><Avatar user={u} size={26} />{u.name} <span className="tiny faint mono">#{u.id}</span></span> : "ユーザー"}>
      {loading && !data ? <Spinner /> : !data ? null : (
        <div className="col" style={{ gap: 12 }}>
          <Tabs value={tab} onChange={setTab} tabs={[
            { key: "overview", label: "概要" }, { key: "actions", label: "操作" },
            { key: "inventory", label: "所持品" }, { key: "rolls", label: "Roll履歴" }, { key: "audit", label: "監査" },
          ]} />

          {tab === "overview" && (
            <div className="col" style={{ gap: 10 }}>
              <div className="kpi-grid">
                <div className="kpi"><span className="k">Level</span><span className="v">{u.level}</span></div>
                <div className="kpi"><span className="k">Stardust</span><span className="v">✦{fmtCompact(u.stardust)}</span></div>
                <div className="kpi"><span className="k">Base Luck</span><span className="v">{fmtLuck(u.base_luck)}</span></div>
                <div className="kpi"><span className="k">Rolls</span><span className="v">{fmtCompact(data.stats?.total_rolls ?? 0)}</span></div>
                <div className="kpi"><span className="k">Best</span><span className="v">{fmtOdds(data.stats?.best_odds)}</span></div>
                <div className="kpi"><span className="k">Firsts</span><span className="v">{fmtInt(data.stats?.first_discoveries ?? 0)}</span></div>
              </div>
              <div className="row-wrap tiny">
                <span className="chip">{u.status}{u.status_reason ? `: ${u.status_reason}` : ""}</span>
                <span className="chip">role: {u.role}</span>
                <span className="chip">discord: {u.discord_id}</span>
                <span className="chip">登録 {fmtDate(u.created_at)}</span>
                <span className="chip">最終 {fmtDate(u.last_seen_at)}</span>
                {u.auto_roll && <span className="chip" style={{ color: "var(--good)" }}>Auto Roll ON</span>}
              </div>
              {data.biome && (
                <div className="glass-2 pad-sm">
                  <div className="row"><strong style={{ flex: 1 }}>現在のBiome: {data.biome.name}</strong>
                    <button className="btn xs ghost" onClick={loadTable}>確率テーブルを見る</button></div>
                  {data.biome.reveal && (
                    <div className="tiny mono faint">次の変化: {data.biome.reveal.next_biome} @ {fmtDate(data.biome.reveal.next_change_at)}</div>
                  )}
                </div>
              )}
              {data.effects?.length > 0 && (
                <div className="glass-2 pad-sm col">
                  <strong className="small">有効な効果</strong>
                  <div className="row-wrap">
                    {data.effects.map((e: any) => (
                      <span key={e.id} className="chip tiny">{e.name} · {e.effect_type} {e.value}
                        {e.remaining_rolls !== null ? ` (${e.remaining_rolls}roll)` : ""}</span>
                    ))}
                  </div>
                </div>
              )}
              {data.artifacts?.length > 0 && (
                <div className="glass-2 pad-sm col">
                  <strong className="small">Admin Artifact</strong>
                  {data.artifacts.map((a: any) => (
                    <div key={a.instance_id} className="row tiny" style={{ gap: 7 }}>
                      <ItemIcon visual={a.artifact.visual} size={18} tier={8} animate={false} />
                      <span style={{ flex: 1 }}>{a.artifact.name}</span>
                      <RecallButton instanceId={a.instance_id} onDone={() => { reload(); onChanged(); }} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {tab === "actions" && <ActionPanel userId={userId} actions={actions} onDone={() => { reload(); onChanged(); }} />}

          {tab === "inventory" && (
            <div className="col" style={{ gap: 8 }}>
              <div className="row-wrap">
                {(data.inventory?.groups ?? []).slice(0, 80).map((g: any) => (
                  <span key={g.item.id} className={`chip tiny r-${g.item.rarity}`}>{g.item.name} ×{g.count}</span>
                ))}
              </div>
              {data.boosts?.length > 0 && (
                <>
                  <strong className="small">Boost</strong>
                  <div className="row-wrap">{data.boosts.map((b: any) => <span key={b.key} className="chip tiny">{b.name} ×{b.quantity}</span>)}</div>
                </>
              )}
              {data.equipment?.length > 0 && (
                <>
                  <strong className="small">装備</strong>
                  <div className="row-wrap">{data.equipment.map((e: any) => (
                    <span key={e.id} className={`chip tiny r-${e.rarity}`}>{e.name} ({e.quality_name}){e.equipped_slot ? " ●" : ""}</span>
                  ))}</div>
                </>
              )}
            </div>
          )}

          {tab === "rolls" && (
            <div className="table-wrap" style={{ maxHeight: "52vh" }}>
              <table className="table">
                <thead><tr><th>#</th><th>アイテム</th><th>Luck</th><th>Biome</th><th>Flags</th><th>日時</th></tr></thead>
                <tbody>
                  {(data.rolls ?? []).map((r: any) => (
                    <tr key={r.id}>
                      <td className="mono tiny">{fmtInt(r.number)}</td>
                      <td><span className={`r-${r.item?.rarity}`}>{r.item?.name}</span> <span className="tiny faint mono">{fmtOdds(r.item?.odds)}</span></td>
                      <td className="mono tiny">{fmtLuck(r.luck)}</td>
                      <td className="tiny">{r.biome}</td>
                      <td className="tiny mono faint">{r.flags}</td>
                      <td className="tiny faint">{fmtDate(r.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {tab === "audit" && (
            <div className="col" style={{ gap: 4, maxHeight: "52vh", overflow: "auto" }}>
              {!data.audit?.length ? <div className="muted small">記録なし</div> : data.audit.map((a: any) => (
                <div key={a.id} className="glass-2 pad-sm col" style={{ gap: 2 }}>
                  <div className="row tiny"><strong style={{ flex: 1 }}>{a.action}</strong><span className="faint">{fmtDate(a.created_at)}</span></div>
                  <div className="tiny faint">by {a.admin_name ?? a.admin_id} {a.reason ? `· ${a.reason}` : ""}</div>
                  {a.new && <div className="json-view">{JSON.stringify(a.new)}</div>}
                </div>
              ))}
            </div>
          )}

          {table && (
            <Modal open onClose={() => setTable(null)} title="現在の確率テーブル" wide>
              {table.loading ? <Spinner /> : table.error ? <div style={{ color: "var(--bad)" }}>{table.error}</div> : (
                <>
                  <div className="row-wrap" style={{ marginBottom: 8 }}>
                    <span className="chip">Luck {fmtLuck(table.luck.final)}</span>
                    <span className="chip">{table.biome}{table.state ? ` / ${table.state}` : ""}</span>
                    {table.forced && <span className="chip" style={{ color: "var(--r-admin)" }}>強制: {table.forced}</span>}
                    {table.min_tier > 0 && <span className="chip">最低Tier {table.min_tier}</span>}
                    {table.flatten !== 1 && <span className="chip">flatten {table.flatten}</span>}
                  </div>
                  <div className="table-wrap" style={{ maxHeight: "50vh" }}>
                    <table className="table">
                      <thead><tr><th>アイテム</th><th>基礎</th><th>現在</th></tr></thead>
                      <tbody>
                        {table.items.map((i: any) => (
                          <tr key={i.key}>
                            <td><span className={`r-${i.rarity}`}>{i.name}</span></td>
                            <td className="mono tiny">{fmtOdds(i.odds)}</td>
                            <td className="mono">{fmtOdds(1 / i.p)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </Modal>
          )}
        </div>
      )}
    </Modal>
  );
}

function RecallButton({ instanceId, onDone }: { instanceId: number; onDone: () => void }) {
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const click = async () => {
    const ok = await confirm("Artifactを回収", "このAdmin Artifactを回収します（監査ログに記録されます）", { danger: true });
    if (!ok) return;
    if (await run(() => post("/api/admin/artifacts/recall", { instance_id: instanceId, reason: "管理者による回収" }), { success: "回収しました" })) onDone();
  };
  return (<>
    <button className="btn xs danger" disabled={busy} onClick={click}>回収</button>
    {node}
  </>);
}

function ActionPanel({ userId, actions, onDone }: { userId: number; actions: ActionDef[]; onDone: () => void }) {
  const [sel, setSel] = useState<ActionDef>(actions[0]);
  const [params, setParams] = useState<Record<string, any>>({});
  const [reason, setReason] = useState("");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const toast = useGame((s) => s.toast);

  const submit = async () => {
    if (!reason.trim()) {
      toast("理由を入力してください", "warning");
      return;
    }
    if (sel.danger) {
      const ok = await confirm(`${sel.label} を実行`, (
        <div className="col">
          <p>この操作は取り消せない場合があります。監査ログに記録されます。</p>
          <div className="json-view">{JSON.stringify({ action: sel.key, params, reason }, null, 2)}</div>
        </div>
      ), { danger: true });
      if (!ok) return;
    }
    const payload: Record<string, any> = { ...params };
    if (sel.danger) payload.confirm = true;
    const res = await run(() => post(`/api/admin/users/${userId}/action`, { action: sel.key, params: payload, reason }),
      { success: `${sel.label} を実行しました` });
    if (res) {
      setParams({});
      setReason("");
      onDone();
    }
  };

  return (
    <div className="col" style={{ gap: 10 }}>
      <div>
        <label>操作</label>
        <select value={sel.key} onChange={(e) => { setSel(actions.find((a) => a.key === e.target.value)!); setParams({}); }}>
          {actions.map((a) => <option key={a.key} value={a.key}>{a.danger ? "⚠ " : ""}{a.label}</option>)}
        </select>
      </div>
      {sel.fields.length > 0 && (
        <div className="admin-form">
          {sel.fields.map((f) => (
            <div key={f.name}>
              <label>{f.label}{f.required && " *"}</label>
              {f.type === "select" ? (
                <select value={params[f.name] ?? ""} onChange={(e) => setParams((p) => ({ ...p, [f.name]: e.target.value }))}>
                  <option value="">選択</option>
                  {f.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              ) : f.type === "checkbox" ? (
                <label className="switch"><input type="checkbox" checked={!!params[f.name]} onChange={(e) => setParams((p) => ({ ...p, [f.name]: e.target.checked }))} /><span className="track" /></label>
              ) : (
                <input type={f.type} placeholder={f.placeholder} value={params[f.name] ?? ""}
                  onChange={(e) => setParams((p) => ({ ...p, [f.name]: f.type === "number" ? (e.target.value === "" ? undefined : Number(e.target.value)) : e.target.value }))} />
              )}
            </div>
          ))}
        </div>
      )}
      <div><label>理由 *（監査ログに記録）</label><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例: サポート対応 #1234" /></div>
      <button className={`btn ${sel.danger ? "danger" : "primary"}`} disabled={busy || !reason.trim()} onClick={submit}>{sel.label} を実行</button>
      {node}
    </div>
  );
}
