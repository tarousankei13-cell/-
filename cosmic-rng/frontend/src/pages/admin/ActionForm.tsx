import { useMemo, useState } from "react";
import { post } from "../../lib/api";
import { useAction } from "../../lib/useApi";
import { useConfirm } from "../../components/ui";
import { useGame } from "../../store/game";
import type { Bootstrap } from "./Admin";

/**
 * Admin operation form, generated from the server's own catalogue.
 *
 * The list of operations, their arguments and which ones are destructive all
 * come from the API. Nothing here restates them, so the panel cannot offer a
 * control the server does not implement, and the two cannot disagree about what
 * an argument is called — which is how a button ends up looking like it worked.
 */

export interface FieldDef {
  name: string;
  label: string;
  type: string;
  required?: boolean;
  choices?: string[];
  default?: unknown;
  min?: number;
  max?: number;
  help?: string;
}

export interface OperationDef {
  action: string;
  label: string;
  group: string;
  fields: FieldDef[];
  dangerous: boolean;
  help?: string;
}

/** Option lists for the reference types the catalogue uses. */
function optionsFor(type: string, boot: Bootstrap): { value: string; label: string }[] | null {
  const pair = (n: { name: string; name_ja?: string | null }) => (n.name_ja ? `${n.name}（${n.name_ja}）` : n.name);
  switch (type) {
    case "item": return boot.items.map((i) => ({ value: i.key, label: `${pair(i)} · ${i.rarity}` }));
    case "boost": return boot.boosts.map((b) => ({ value: b.key, label: pair(b) }));
    case "equipment": return (boot.equipment ?? []).map((e) => ({ value: e.key, label: `${pair(e)}${e.slot ? ` · ${e.slot}` : ""}` }));
    case "cosmetic": return (boot.cosmetics ?? []).map((c) => ({ value: c.key, label: `${pair(c)}${c.kind ? ` · ${c.kind}` : ""}` }));
    case "achievement": return (boot.achievements ?? []).map((a) => ({ value: a.key, label: pair(a) }));
    case "biome": return boot.biomes.map((b) => ({ value: b.key, label: `${pair(b as any)} · ${b.kind}` }));
    case "artifact": return (boot.artifacts ?? []).map((a: any) => ({ value: a.key, label: a.name ?? a.key }));
    case "rarity": return ["common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic"]
      .map((r) => ({ value: r, label: r }));
    default: return null;
  }
}

function Field({ f, boot, value, onChange }: {
  f: FieldDef; boot: Bootstrap; value: unknown; onChange: (v: unknown) => void;
}) {
  const opts = optionsFor(f.type, boot) ?? (f.choices ? f.choices.map((c) => ({ value: c, label: c })) : null);

  if (f.type === "bool") {
    return (
      <label className="switch">
        <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
        <span className="track" />
        <span className="small">{f.label}</span>
      </label>
    );
  }
  if (opts) {
    return (
      <select value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value || undefined)}>
        <option value="">選択してください</option>
        {opts.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    );
  }
  if (f.type === "int" || f.type === "float") {
    return (
      <input type="number" inputMode={f.type === "int" ? "numeric" : "decimal"}
             step={f.type === "int" ? 1 : "any"} min={f.min} max={f.max}
             placeholder={f.default !== undefined ? String(f.default) : ""}
             value={(value as string) ?? ""}
             onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))} />
    );
  }
  if (f.type === "ids") {
    return (
      <input value={(value as string) ?? ""} placeholder="123, 124, 125"
             onChange={(e) => onChange(e.target.value)} />
    );
  }
  return <input value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value || undefined)} />;
}

export function ActionForm({ operations, boot, endpoint, scopeLabel, onDone, extraFields, extraParams }: {
  operations: OperationDef[];
  boot: Bootstrap;
  /** Where to POST {action|op, params, reason}. */
  endpoint: string;
  /** "このプレイヤー" / "全プレイヤー" — shown on the confirmation. */
  scopeLabel: string;
  onDone?: () => void;
  /** Rendered above the reason field (the bulk panel uses it for the audience). */
  extraFields?: React.ReactNode;
  /** Merged into every request (the bulk panel uses it for the audience filter). */
  extraParams?: Record<string, unknown>;
}) {
  const [query, setQuery] = useState("");
  const [key, setKey] = useState(operations[0]?.action ?? "");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [reason, setReason] = useState("");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const toast = useGame((s) => s.toast);

  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = new Map<string, OperationDef[]>();
    for (const op of operations) {
      if (q && !`${op.label} ${op.action} ${op.group}`.toLowerCase().includes(q)) continue;
      if (!out.has(op.group)) out.set(op.group, []);
      out.get(op.group)!.push(op);
    }
    return out;
  }, [operations, query]);

  const sel = operations.find((o) => o.action === key) ?? operations[0];
  if (!sel) return <div className="muted">操作がありません。</div>;

  const missing = sel.fields.filter((f) => f.required && (params[f.name] === undefined || params[f.name] === ""));

  const submit = async () => {
    if (!reason.trim()) { toast("理由を入力してください", "warning"); return; }
    if (missing.length) { toast(`${missing[0].label} を入力してください`, "warning"); return; }
    const payload: Record<string, unknown> = { ...extraParams, ...params };
    // Defaults live in the catalogue; send them so the server sees what the form showed.
    for (const f of sel.fields) if (payload[f.name] === undefined && f.default !== undefined) payload[f.name] = f.default;
    if (f_ids(sel)) payload[f_ids(sel)!] = String(payload[f_ids(sel)!] ?? "")
      .split(",").map((x) => x.trim()).filter(Boolean).map(Number);

    if (sel.dangerous) {
      const ok = await confirm(`${sel.label}`, (
        <div className="col" style={{ gap: 8 }}>
          <p style={{ margin: 0 }}><strong>{scopeLabel}</strong> に対して実行します。監査ログに記録されます。</p>
          {sel.help && <p className="muted small" style={{ margin: 0 }}>{sel.help}</p>}
          <div className="json-view">{JSON.stringify({ action: sel.action, params: payload, reason }, null, 2)}</div>
        </div>
      ), { danger: true });
      if (!ok) return;
      payload.confirm = true;
    }
    const isBulk = endpoint.endsWith("/bulk");
    const res = await run(
      () => post(endpoint, isBulk ? { op: sel.action, params: payload, reason } : { action: sel.action, params: payload, reason }),
      { success: `${sel.label} を実行しました` },
    );
    if (res) { setParams({}); setReason(""); onDone?.(); }
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div>
        <label>操作を検索</label>
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="例: 付与 / luck / ban" />
      </div>
      <div>
        <label>操作（{operations.length}種）</label>
        <select value={sel.action} onChange={(e) => { setKey(e.target.value); setParams({}); }}>
          {[...grouped.entries()].map(([group, ops]) => (
            <optgroup key={group} label={group}>
              {ops.map((o) => <option key={o.action} value={o.action}>{o.dangerous ? "⚠ " : ""}{o.label}</option>)}
            </optgroup>
          ))}
        </select>
        {sel.help && <div className="field-help">{sel.help}</div>}
      </div>

      {sel.fields.length > 0 && (
        <div className="admin-form">
          {sel.fields.map((f) => (
            <div key={f.name}>
              {f.type !== "bool" && <label>{f.label}{f.required && " *"}</label>}
              <Field f={f} boot={boot} value={params[f.name]}
                     onChange={(v) => setParams((p) => ({ ...p, [f.name]: v }))} />
              {f.help && <div className="field-help">{f.help}</div>}
            </div>
          ))}
        </div>
      )}

      {extraFields}

      <div>
        <label>理由 *（監査ログに残ります）</label>
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例: サポート対応 #1234" />
      </div>
      <button className={`btn ${sel.dangerous ? "danger" : "primary"}`}
              disabled={busy || !reason.trim() || missing.length > 0} onClick={submit}>
        {sel.dangerous && "⚠ "}{sel.label}
      </button>
      {node}
    </div>
  );
}

/** Name of the one "ids" field, if this operation has one. */
function f_ids(op: OperationDef): string | null {
  return op.fields.find((f) => f.type === "ids")?.name ?? null;
}
