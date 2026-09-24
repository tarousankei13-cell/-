import { useState } from "react";
import { get, post } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { ItemIcon } from "../../components/ItemIcon";
import { Avatar, Empty, Modal, Spinner, Tabs, UserChip, useConfirm } from "../../components/ui";
import { fmtDate } from "../../lib/format";
import type { Bootstrap } from "./Admin";
import type { UserBrief } from "../../lib/types";

const THEME_COLOR: Record<string, string> = {
  cosmic: "#b8a0ff", divine: "#ffd24d", void: "#8a2cff", reality: "#ff5cf0", system: "#7affc0", time: "#bfe0ff", space: "#5c8aff",
};

export function AdminArtifacts({ boot }: { boot: Bootstrap }) {
  const [tab, setTab] = useState<"catalog" | "holders">("catalog");
  const [granting, setGranting] = useState<any | null>(null);
  const { data: holders, reload: reloadHolders } = useApi<{ holders: any[] }>(tab === "holders" ? "/api/admin/artifacts/holders" : null);
  const { data: mine, reload: reloadMine } = useApi<{ artifacts: any[] }>("/api/artifacts");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const enqueue = useGame((s) => s.enqueueReveal);
  const refreshHud = useGame((s) => s.refreshHud);
  const me = useGame((s) => s.me);
  const toast = useGame((s) => s.toast);

  const grantSelf = async (key: string) => {
    const res = await run(() => post("/api/admin/artifacts/grant", {
      target_user_id: me!.user.id, artifact_key: key, can_use: true, reason: "管理者自己付与",
    }), { success: "自分に付与しました" });
    if (res) reloadMine();
  };

  const useArtifact = async (a: any) => {
    const art = a.artifact;
    let params: any = {};
    if (art.effect?.type === "choose_biome") {
      const key = window.prompt(`Biomeキーを入力\n${boot.biomes.filter((b) => b.kind === "natural").map((b) => b.key).join(", ")}`);
      if (!key) return;
      params.biome_key = key;
    } else if (art.target === "user") {
      const id = window.prompt("対象プレイヤーIDを入力");
      if (!id) return;
      params.target_user_id = Number(id);
    } else if (art.effect?.type === "force_item") {
      const key = window.prompt("指定するアイテムkey（空欄でLegendary保証）") ?? "";
      if (key) params.item_key = key;
    } else if (art.effect?.type === "upgrade_equipment") {
      const id = window.prompt("装備インスタンスIDを入力");
      if (!id) return;
      params.equipment_id = Number(id);
    }
    if (art.target === "global") {
      const ok = await confirm("ワールドイベントの発動", `${art.name} は全プレイヤーに影響します。実行しますか？`, { danger: true });
      if (!ok) return;
    }
    const res = await run(() => post<any>("/api/artifacts/use", { instance_id: a.instance_id, params }, true));
    if (res) {
      enqueue({ type: "artifact", cinematic: res.cinematic, result: res.result, global: art.target === "global" });
      refreshHud();
      reloadMine();
      if (res.result?.burst) enqueue({ type: "offline", summary: res.result.burst });
      if (res.result?.item) toast(`生成: ${res.result.item.name}`, "cosmic");
    }
  };

  const recall = async (instanceId: number) => {
    const ok = await confirm("Artifactを回収", "このArtifactを回収します（監査ログに記録）", { danger: true });
    if (!ok) return;
    if (await run(() => post("/api/admin/artifacts/recall", { instance_id: instanceId, reason: "管理者による回収" }), { success: "回収しました" })) {
      reloadHolders();
      reloadMine();
    }
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <Tabs value={tab} onChange={setTab} tabs={[
        { key: "catalog", label: `カタログ (${boot.artifacts.length})` },
        { key: "holders", label: "所持者一覧" },
      ]} />

      {tab === "catalog" ? (
        <>
          {mine?.artifacts.length ? (
            <section className="glass pad col">
              <h3>自分が所持しているArtifact</h3>
              <div className="grid grid-auto">
                {mine.artifacts.map((a) => (
                  <div key={a.instance_id} className="item-card artifact-card" style={{ cursor: "default" }}>
                    <div className="row" style={{ gap: 9 }}>
                      <ItemIcon visual={a.artifact.visual} tier={8} size={40} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="name r-admin">{a.artifact.name}</div>
                        <div className="tiny faint">{a.artifact.theme} · T{a.artifact.tier} · {a.artifact.target}</div>
                      </div>
                    </div>
                    <div className="tiny muted">{a.artifact.ability}</div>
                    <div className="row" style={{ gap: 5 }}>
                      <button className="btn xs gold" disabled={busy} onClick={() => useArtifact(a)}>使用</button>
                      <button className="btn xs ghost" disabled={busy} onClick={() => recall(a.instance_id)}>破棄</button>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ) : null}

          <div className="grid grid-auto">
            {boot.artifacts.map((a) => (
              <div key={a.key} className="item-card artifact-card" style={{ cursor: "default", borderColor: `${THEME_COLOR[a.theme] ?? "#fff"}66` }}>
                <div className="row" style={{ gap: 9 }}>
                  <ItemIcon visual={a.visual} tier={8} size={44} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="name" style={{ color: THEME_COLOR[a.theme] }}>{a.name}</div>
                    <div className="tiny faint">{a.theme.toUpperCase()} · TIER {a.tier} · {a.target}</div>
                  </div>
                  {!a.is_active && <span className="chip tiny" style={{ color: "var(--bad)" }}>無効</span>}
                </div>
                <div className="tiny" style={{ fontStyle: "italic", opacity: 0.7 }}>「{a.lore}」</div>
                <div className="small">{a.ability}</div>
                <div className="row-wrap tiny" style={{ gap: 4 }}>
                  <span className="chip tiny">CD {a.cooldown_sec}s</span>
                  {a.duration_sec && <span className="chip tiny">{a.duration_sec}s</span>}
                  {a.player_usable && <span className="chip tiny" style={{ color: "var(--good)" }}>付与時プレイヤー使用可</span>}
                </div>
                <div className="row" style={{ gap: 5 }}>
                  <button className="btn xs" disabled={busy} onClick={() => grantSelf(a.key)}>自分に付与</button>
                  <button className="btn xs ghost" onClick={() => setGranting(a)}>プレイヤーに付与</button>
                </div>
              </div>
            ))}
          </div>
        </>
      ) : (
        !holders ? <Spinner /> : !holders.holders.length ? <Empty icon="✨">所持者はいません</Empty> : (
          <div className="table-wrap glass">
            <table className="table">
              <thead><tr><th>Artifact</th><th>所持者</th><th>状態</th><th>付与日時</th><th /></tr></thead>
              <tbody>
                {holders.holders.map((h) => (
                  <tr key={h.instance_id}>
                    <td className="r-admin">{h.artifact}</td>
                    <td><UserChip user={h.user} size={20} /></td>
                    <td className="tiny">{h.state}{h.meta?.granted_by ? ` (by #${h.meta.granted_by})` : ""}</td>
                    <td className="tiny faint">{fmtDate(h.obtained_at)}</td>
                    <td><button className="btn xs danger" disabled={busy} onClick={() => recall(h.instance_id)}>回収</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {granting && <GrantModal artifact={granting} onClose={() => setGranting(null)} onDone={() => { setGranting(null); reloadHolders(); }} />}
      {node}
    </div>
  );
}

function GrantModal({ artifact, onClose, onDone }: { artifact: any; onClose: () => void; onDone: () => void }) {
  const [target, setTarget] = useState<UserBrief | null>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<UserBrief[]>([]);
  const [canUse, setCanUse] = useState(false);
  const [uses, setUses] = useState("");
  const [hours, setHours] = useState("");
  const [reason, setReason] = useState("");
  const { run, busy } = useAction();

  const search = async (v: string) => {
    setQ(v);
    if (v.length < 1) return setResults([]);
    try {
      const r = await get<{ users: UserBrief[] }>("/api/users/search", { q: v });
      setResults(r.users);
    } catch {
      setResults([]);
    }
  };

  const submit = async () => {
    if (!target || !reason.trim()) return;
    const res = await run(() => post("/api/admin/artifacts/grant", {
      target_user_id: target.id, artifact_key: artifact.key, can_use: canUse,
      uses: uses ? Number(uses) : null, hours: hours ? Number(hours) : null, reason,
    }), { success: `${target.name} に付与しました` });
    if (res) onDone();
  };

  return (
    <Modal open onClose={onClose} title={`${artifact.name} を付与`}
      footer={<><button className="btn ghost" onClick={onClose}>キャンセル</button>
        <button className="btn gold" disabled={!target || !reason.trim() || busy} onClick={submit}>付与する</button></>}>
      <div className="col" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 10 }}>
          <ItemIcon visual={artifact.visual} tier={8} size={48} />
          <div><strong className="r-admin">{artifact.name}</strong><div className="tiny muted">{artifact.ability}</div></div>
        </div>
        <div>
          <label>対象プレイヤー</label>
          <input value={q} onChange={(e) => search(e.target.value)} placeholder="名前 / ID" />
          {results.length > 0 && (
            <div className="col" style={{ gap: 3, maxHeight: 160, overflow: "auto", marginTop: 4 }}>
              {results.map((u) => (
                <button key={u.id} className="row" style={{ gap: 8, padding: "5px 8px", borderRadius: 8, background: "rgba(255,255,255,0.04)", cursor: "pointer", border: "1px solid transparent", textAlign: "left" }}
                  onClick={() => { setTarget(u); setResults([]); setQ(u.name); }}>
                  <Avatar user={u} size={20} /><span style={{ flex: 1 }}>{u.name}</span><span className="tiny faint">#{u.id}</span>
                </button>
              ))}
            </div>
          )}
          {target && <div className="chip" style={{ marginTop: 6 }}>{target.name} (#{target.id})</div>}
        </div>
        <div className="admin-form">
          <div>
            <label>プレイヤーによる使用を許可</label>
            <label className="switch">
              <input type="checkbox" checked={canUse} disabled={!artifact.player_usable} onChange={(e) => setCanUse(e.target.checked)} />
              <span className="track" /><span className="tiny">{artifact.player_usable ? "許可する" : "このArtifactは管理者専用"}</span>
            </label>
          </div>
          <div><label>使用回数制限（空=無制限）</label><input type="number" min={1} value={uses} onChange={(e) => setUses(e.target.value)} /></div>
          <div><label>有効期限（時間・空=無期限）</label><input type="number" min={1} value={hours} onChange={(e) => setHours(e.target.value)} /></div>
        </div>
        <div><label>理由 *</label><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例: イベント報酬" /></div>
        <div className="tiny faint">付与後も売却・トレード・Gift は不可。管理者はいつでも回収できます。</div>
      </div>
    </Modal>
  );
}
