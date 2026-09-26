import { useMemo, useState } from "react";
import { put } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { Spinner, useConfirm } from "../../components/ui";

interface Def {
  key: string; type: string; default: any; group: string; label: string; description: string;
  min: number | null; max: number | null; choices: string[]; dangerous: boolean;
}

const GROUP_LABEL: Record<string, string> = {
  features: "機能ON/OFF", roll: "Roll / RNG", luck: "Luck", offline: "オフラインRoll", biome: "Biome",
  inventory: "インベントリ", feed: "World Feed", discord: "Discord通知", market: "Market", trade: "Trade",
  gift: "Gift", progression: "進行/解放", quests: "クエスト", economy: "経済", retention: "データ保持", security: "セキュリティ",
};

export function AdminSettingsPage() {
  const { data, loading, reload } = useApi<{ definitions: Def[]; values: Record<string, any> }>("/api/admin/settings");
  const [edits, setEdits] = useState<Record<string, any>>({});
  const [reason, setReason] = useState("");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const toast = useGame((s) => s.toast);

  const groups = useMemo(() => {
    const g: Record<string, Def[]> = {};
    for (const d of data?.definitions ?? []) (g[d.group] ??= []).push(d);
    return g;
  }, [data]);

  const current = (k: string) => (k in edits ? edits[k] : data?.values[k]);
  const dirty = Object.keys(edits).filter((k) => JSON.stringify(edits[k]) !== JSON.stringify(data?.values[k]));

  const save = async () => {
    if (!dirty.length) return;
    const values: Record<string, any> = {};
    for (const k of dirty) {
      const def = data!.definitions.find((d) => d.key === k)!;
      let v = edits[k];
      if (def.type === "json" && typeof v === "string") {
        try {
          v = JSON.parse(v);
        } catch {
          toast(`${def.label}: JSONが不正です`, "error");
          return;
        }
      }
      values[k] = v;
    }
    const hasDanger = dirty.some((k) => data!.definitions.find((d) => d.key === k)?.dangerous);
    if (!reason.trim() && hasDanger) {
      toast("危険な設定の変更には理由が必要です", "warning");
      return;
    }
    const ok = await confirm("ゲーム設定の変更", (
      <div className="col">
        <p>{dirty.length}件の設定を変更します。全プレイヤーに即座に反映されます。</p>
        <div className="json-view">{JSON.stringify(values, null, 2)}</div>
      </div>
    ), { danger: hasDanger });
    if (!ok) return;
    const res = await run(() => put("/api/admin/settings", { values, reason }), { success: "設定を更新しました" });
    if (res) {
      setEdits({});
      setReason("");
      reload();
    }
  };

  if (loading && !data) return <Spinner />;

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad row-wrap" style={{ position: "sticky", top: 8, zIndex: 20 }}>
        <input placeholder="変更理由（危険な設定では必須）" value={reason} onChange={(e) => setReason(e.target.value)} style={{ flex: "1 1 240px" }} />
        <button className="btn primary" disabled={!dirty.length || busy} onClick={save}>{dirty.length}件を保存</button>
        {dirty.length > 0 && <button className="btn ghost sm" onClick={() => setEdits({})}>破棄</button>}
      </div>

      {Object.entries(groups).map(([g, defs]) => (
        <section key={g} className="glass pad col" style={{ gap: 8 }}>
          <h3>{GROUP_LABEL[g] ?? g}</h3>
          {defs.map((d) => {
            const changed = dirty.includes(d.key);
            const v = current(d.key);
            return (
              <div key={d.key} className="row" style={{
                gap: 12, flexWrap: "wrap", padding: "6px 8px", borderRadius: 8,
                background: changed ? "rgba(255,208,90,0.1)" : undefined, borderBottom: "1px solid rgba(255,255,255,0.05)",
              }}>
                <div style={{ flex: "1 1 240px", minWidth: 0 }}>
                  <div className="row" style={{ gap: 6 }}>
                    <span>{d.label}</span>
                    {d.dangerous && <span className="chip tiny" style={{ color: "var(--bad)" }}>危険</span>}
                  </div>
                  <div className="tiny faint mono">{d.key}</div>
                  {d.description && <div className="tiny faint">{d.description}</div>}
                </div>
                <div style={{ flex: "0 1 300px" }}>
                  {d.type === "bool" ? (
                    <label className="switch"><input type="checkbox" checked={!!v} onChange={(e) => setEdits((s) => ({ ...s, [d.key]: e.target.checked }))} /><span className="track" /></label>
                  ) : d.type === "enum" || d.type === "tier" ? (
                    <select value={v ?? ""} onChange={(e) => setEdits((s) => ({ ...s, [d.key]: e.target.value }))}>
                      {d.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  ) : d.type === "json" ? (
                    <textarea className="json" value={typeof v === "string" ? v : JSON.stringify(v, null, 1)}
                      onChange={(e) => setEdits((s) => ({ ...s, [d.key]: e.target.value }))} spellCheck={false} />
                  ) : d.type === "str" ? (
                    <input value={v ?? ""} onChange={(e) => setEdits((s) => ({ ...s, [d.key]: e.target.value }))} />
                  ) : (
                    <input type="number" step={d.type === "float" ? "any" : 1} min={d.min ?? undefined} max={d.max ?? undefined}
                      value={v ?? ""} onChange={(e) => setEdits((s) => ({ ...s, [d.key]: e.target.value === "" ? null : Number(e.target.value) }))} />
                  )}
                </div>
              </div>
            );
          })}
        </section>
      ))}
      {node}
    </div>
  );
}
