import { useEffect, useMemo, useState } from "react";
import { del, post, put } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { Empty, Modal, Spinner, Tabs, useConfirm } from "../../components/ui";
import { FX_KEYS, ItemIcon, SHAPES_BY_MATERIAL } from "../../components/ItemIcon";
import { fmtDate } from "../../lib/format";
import type { Bootstrap } from "./Admin";

type FieldDef = Bootstrap["content_types"][number]["fields"][number];

function FieldInput({ f, value, onChange, locked }: { f: FieldDef; value: any; onChange: (v: any) => void; locked?: boolean }) {
  if (locked || f.readonly) return <input value={value ?? ""} disabled />;
  switch (f.type) {
    case "bool":
      return <label className="switch"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /><span className="track" /></label>;
    case "enum":
      return (
        <select value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
          <option value="">（未設定）</option>
          {f.choices.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      );
    case "int":
    case "float":
      return <input type="number" step={f.type === "float" ? "any" : 1} value={value ?? ""} placeholder={f.min !== null ? `≥ ${f.min}` : ""}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />;
    case "json":
      return <textarea className="json" value={typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 1)}
        onChange={(e) => onChange(e.target.value)} spellCheck={false} />;
    case "text":
      return <textarea value={value ?? ""} onChange={(e) => onChange(e.target.value)} style={{ minHeight: 64 }} />;
    case "datetime":
      return <input type="datetime-local" value={value ? String(value).slice(0, 16) : ""} onChange={(e) => onChange(e.target.value ? new Date(e.target.value).toISOString() : null)} />;
    default:
      return <input value={value ?? ""} onChange={(e) => onChange(e.target.value)} />;
  }
}

export function AdminContent({ boot }: { boot: Bootstrap }) {
  const [type, setType] = useState(boot.content_types[0]?.key ?? "items");
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<any | null>(null);
  const [creating, setCreating] = useState(false);
  const [seed, setSeed] = useState<any | null>(null);
  const [showOverrides, setShowOverrides] = useState(false);

  const def = useMemo(() => boot.content_types.find((t) => t.key === type)!, [boot, type]);
  useEffect(() => {
    const t = window.setTimeout(() => { setDebounced(q); setPage(1); }, 280);
    return () => window.clearTimeout(t);
  }, [q]);

  const { data, loading, reload } = useApi<{ rows: any[]; total: number }>(`/api/admin/content/${type}`, { q: debounced, page });

  return (
    <div className="col" style={{ gap: 12 }}>
      <Tabs value={type} onChange={(t) => { setType(t); setPage(1); setQ(""); }}
        tabs={boot.content_types.map((t) => ({ key: t.key, label: t.label }))} />

      <div className="glass pad row-wrap">
        <input type="search" placeholder="検索" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 220px" }} />
        <button className="btn sm primary" onClick={() => setCreating(true)}>＋ 新規作成</button>
        <button className="btn sm ghost" onClick={() => setShowOverrides(true)}>一時変更一覧</button>
        <button className="btn sm ghost" onClick={() => reload()}>更新</button>
      </div>

      {loading && !data ? <Spinner /> : !data?.rows.length ? <Empty icon="📦">該当なし</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead>
              <tr>{def.list_fields.map((f) => <th key={f}>{def.fields.find((x) => x.name === f)?.label ?? f}</th>)}<th /></tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.key ?? r.id} style={r._override ? { background: "rgba(255,193,77,0.08)" } : undefined}>
                  {def.list_fields.map((f) => (
                    <td key={f} className={typeof r[f] === "number" ? "mono" : ""}>
                      {typeof r[f] === "boolean" ? (r[f] ? "✓" : "—") : String(r[f] ?? "—").slice(0, 48)}
                    </td>
                  ))}
                  <td>
                    <div className="row" style={{ gap: 4 }}>
                      {r._override && <span className="chip tiny" style={{ color: "var(--warn)" }}>一時変更中</span>}
                      <button className="btn xs" onClick={() => setEditing(r)}>編集</button>
                      <button className="btn xs ghost" title="この行をひな型に新規作成"
                              onClick={() => { setSeed({ ...r, key: `${r.key ?? "new"}_copy` }); setCreating(true); }}>複製</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.total > 50 && (
        <div className="row center" style={{ justifyContent: "center", gap: 8 }}>
          <button className="btn sm ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>前へ</button>
          <span className="mono small">{page} / {Math.ceil(data.total / 50)}</span>
          <button className="btn sm ghost" disabled={page >= Math.ceil(data.total / 50)} onClick={() => setPage((p) => p + 1)}>次へ</button>
        </div>
      )}

      {(editing || creating) && (
        <ContentEditor key={editing?.key ?? seed?.key ?? "new"} type={type} def={def} row={editing ?? seed} creating={creating}
          onClose={() => { setEditing(null); setCreating(false); setSeed(null); }}
          onSaved={() => { setEditing(null); setCreating(false); setSeed(null); reload(); }} />
      )}
      {showOverrides && <OverridesModal onClose={() => setShowOverrides(false)} />}
    </div>
  );
}


const MATERIAL_LABEL: Record<string, string> = {
  faceted: "結晶・多面体", sphere: "球体・有機体", metal: "金属・器物", organic: "自然物", cosmic: "天体・エネルギー",
};
const FX_LABEL: Record<string, string> = {
  none: "なし", pulse: "脈動", sparkle: "きらめき", spin: "回転", orbit: "周回",
  rainbow: "虹色", glitch: "グリッチ", flame: "炎", void: "虚無", artifact: "遺物",
};
const PALETTES: { name: string; colors: [string, string, string]; glow: string }[] = [
  { name: "氷晶", colors: ["#a8e6ff", "#2f6f9e", "#ffffff"], glow: "#8fd8ff" },
  { name: "紅蓮", colors: ["#ff8a50", "#8f2412", "#ffe0a8"], glow: "#ff6a2a" },
  { name: "翠緑", colors: ["#8ef0a8", "#1f7a45", "#e6fff0"], glow: "#6ee89a" },
  { name: "紫電", colors: ["#c9a0ff", "#4a2a8f", "#f0e4ff"], glow: "#b07cff" },
  { name: "黄金", colors: ["#ffd76a", "#8a5c12", "#fff6d8"], glow: "#ffc640" },
  { name: "深淵", colors: ["#6a6f9e", "#14162c", "#b8bfe8"], glow: "#7a6fd0" },
  { name: "血赤", colors: ["#ff5c7a", "#7a0f28", "#ffd4de"], glow: "#ff3b63" },
  { name: "銀鋼", colors: ["#d8dcea", "#5a6078", "#ffffff"], glow: "#aab2cc" },
];

/**
 * Visual designer for content that carries a `visual` descriptor.
 *
 * Adding an item used to mean typing JSON and finding out what it looked like
 * after a save and a reload. Everything here writes straight into that same
 * JSON, so the raw field stays the escape hatch and the picker stays honest —
 * and the preview is the real renderer, at the real tier, not a mock-up.
 */
function VisualDesigner({ json, onChange, tier }: { json: string; onChange: (next: string) => void; tier: number }) {
  const parsed = useMemo(() => {
    try { return JSON.parse(json || "{}"); } catch { return null; }
  }, [json]);
  const [previewTier, setPreviewTier] = useState(tier);
  useEffect(() => setPreviewTier(tier), [tier]);

  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return <div className="field-help" style={{ color: "var(--warn)" }}>JSONを直せばプレビューが再開します。</div>;
  }
  const v = parsed as Record<string, any>;
  const colors: string[] = Array.isArray(v.colors) ? v.colors : [];
  const col = (i: number) => colors[i] ?? ["#c0c8e0", "#7080a8", "#ffffff"][i];
  const patch = (next: Record<string, any>) => onChange(JSON.stringify({ ...v, ...next }, null, 1));
  const setColor = (i: number, value: string) => {
    const c = [col(0), col(1), col(2)];
    c[i] = value;
    patch({ colors: c });
  };

  return (
    <div className="visual-designer">
      <div className="vd-preview">
        <ItemIcon visual={v} size={128} tier={previewTier} />
        <label className="tiny faint" htmlFor="vd-tier">見た目の確認Tier {previewTier}</label>
        <input id="vd-tier" type="range" min={1} max={9} value={previewTier}
               onChange={(e) => setPreviewTier(Number(e.target.value))} />
      </div>
      <div className="vd-controls">
        <div>
          <label>形状</label>
          <select value={v.shape ?? "orb"} onChange={(e) => patch({ shape: e.target.value })}>
            {Object.entries(SHAPES_BY_MATERIAL).map(([mat, shapes]) => (
              <optgroup key={mat} label={MATERIAL_LABEL[mat] ?? mat}>
                {shapes.map((sh) => <option key={sh} value={sh}>{sh}</option>)}
              </optgroup>
            ))}
          </select>
        </div>
        <div>
          <label>エフェクト</label>
          <select value={v.fx ?? "none"} onChange={(e) => patch({ fx: e.target.value })}>
            {FX_KEYS.map((k) => <option key={k} value={k}>{FX_LABEL[k] ?? k}</option>)}
          </select>
        </div>
        <div className="full">
          <label>配色</label>
          <div className="row-wrap" style={{ gap: 8 }}>
            {["本体", "陰影", "ハイライト"].map((lbl, i) => (
              <label key={lbl} className="vd-swatch">
                <input type="color" value={col(i)} onChange={(e) => setColor(i, e.target.value)} aria-label={lbl} />
                <span className="tiny faint">{lbl}</span>
              </label>
            ))}
            <label className="vd-swatch">
              <input type="color" value={v.glow ?? col(0)} onChange={(e) => patch({ glow: e.target.value })} aria-label="発光" />
              <span className="tiny faint">発光</span>
            </label>
          </div>
        </div>
        <div className="full">
          <label>プリセット配色</label>
          <div className="row-wrap" style={{ gap: 6 }}>
            {PALETTES.map((pal) => (
              <button key={pal.name} type="button" className="btn xs ghost"
                      onClick={() => patch({ colors: [...pal.colors], glow: pal.glow })}>
                <span className="vd-dot" style={{ background: `linear-gradient(135deg, ${pal.colors[0]}, ${pal.colors[1]})` }} />
                {pal.name}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function ContentEditor({ type, def, row, creating, onClose, onSaved }: {
  type: string; def: Bootstrap["content_types"][number]; row: any | null; creating: boolean; onClose: () => void; onSaved: () => void;
}) {
  const [values, setValues] = useState<Record<string, any>>(() => {
    const v: Record<string, any> = {};
    for (const f of def.fields) {
      const raw = row?.[f.name];
      v[f.name] = f.type === "json" ? JSON.stringify(raw ?? (f.name.endsWith("s") ? [] : {}), null, 1) : raw ?? (f.type === "bool" ? true : "");
    }
    return v;
  });
  const [reason, setReason] = useState("");
  const [temporary, setTemporary] = useState(false);
  const [hours, setHours] = useState("1");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const toast = useGame((s) => s.toast);

  /** The identity column: askable only while creating, frozen afterwards
   *  because update_row drops it — an editable box there would lie. */
  const isKey = (f: FieldDef) => f.name === (def.key_field ?? "key");

  const buildPayload = () => {
    const out: Record<string, any> = {};
    for (const f of def.fields) {
      if (f.readonly && !creating) continue;
      const v = values[f.name];
      if (!creating && row && f.type !== "json" && v === (row[f.name] ?? (f.type === "bool" ? false : ""))) continue;
      if (f.type === "json") {
        try {
          const parsed = JSON.parse(v || "{}");
          if (!creating && row && JSON.stringify(parsed) === JSON.stringify(row[f.name] ?? {})) continue;
          out[f.name] = parsed;
        } catch {
          throw new Error(`${f.label}: JSONの構文が不正です`);
        }
      } else if (v !== "" || creating) {
        out[f.name] = v === "" ? null : v;
      }
    }
    return out;
  };

  const save = async () => {
    let payload: Record<string, any>;
    try {
      payload = buildPayload();
    } catch (e) {
      toast((e as Error).message, "error");
      return;
    }
    if (!creating && Object.keys(payload).length === 0) {
      toast("変更がありません", "info");
      return;
    }
    if (!reason.trim()) {
      toast("変更理由を入力してください", "warning");
      return;
    }
    const ok = await confirm(creating ? "新規作成" : temporary ? "一時変更を適用" : "永久変更を適用", (
      <div className="col">
        <p>{temporary ? `${hours}時間後に自動で元に戻ります。` : "この変更は即座に全ワーカーへ反映されます。"}</p>
        <div className="json-view">{JSON.stringify(payload, null, 2)}</div>
      </div>
    ), { danger: !temporary && !creating });
    if (!ok) return;
    const res = await run(() => creating
      ? post(`/api/admin/content/${type}`, { data: payload, reason })
      : put(`/api/admin/content/${type}/${row.key ?? row.id}`, { data: payload, reason, temporary_hours: temporary ? Number(hours) : null }),
      { success: creating ? "作成しました" : "更新しました" });
    if (res) onSaved();
  };

  const remove = async () => {
    const ok = await confirm("削除 / 無効化", `${row.key} を削除（または無効化）します。`, { danger: true, phrase: String(row.key) });
    if (!ok) return;
    if (await run(() => del(`/api/admin/content/${type}/${row.key ?? row.id}`, { reason: reason || "削除" }), { success: "削除しました" })) onSaved();
  };

  return (
    <Modal open onClose={onClose} wide title={creating ? `新規 ${def.label}` : `${def.label}: ${row?.key ?? row?.id}`}
      footer={
        <>
          {!creating && <button className="btn sm danger" style={{ marginRight: "auto" }} disabled={busy} onClick={remove}>削除</button>}
          <button className="btn ghost" onClick={onClose}>キャンセル</button>
          <button className="btn primary" disabled={busy} onClick={save}>{creating ? "作成" : "保存"}</button>
        </>
      }>
      <div className="col" style={{ gap: 12 }}>
        <div className="admin-form">
          {def.fields.filter((f) => !(creating && f.readonly && !isKey(f))).map((f) => (
            <div key={f.name} className={f.type === "json" || f.type === "text" ? "full" : ""}>
              <label>{f.label}{f.required && " *"}
                {isKey(f) && !creating && <span className="tiny faint"> (作成後は変更できません)</span>}
                {f.readonly && !(isKey(f) && creating) && <span className="tiny faint"> (読取専用)</span>}
              </label>
              <FieldInput f={{ ...f, readonly: f.readonly && !(isKey(f) && creating) }} locked={isKey(f) && !creating}
                          value={values[f.name]} onChange={(v) => setValues((s) => ({ ...s, [f.name]: v }))} />
              {f.help && <div className="field-help">{f.help}</div>}
              {f.name === "visual" && f.type === "json" && (
                <VisualDesigner json={typeof values.visual === "string" ? values.visual : JSON.stringify(values.visual ?? {}, null, 1)}
                                tier={Number(row?.tier ?? values.tier ?? 1) || 1}
                                onChange={(next) => setValues((s) => ({ ...s, visual: next }))} />
              )}
            </div>
          ))}
        </div>
        <div className="glass-2 pad-sm col">
          <div><label>変更理由 *</label><input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例: バランス調整" /></div>
          {!creating && def.overridable && (
            <div className="row-wrap">
              <label className="switch"><input type="checkbox" checked={temporary} onChange={(e) => setTemporary(e.target.checked)} /><span className="track" /><span className="tiny">一時変更（自動で元に戻す）</span></label>
              {temporary && (
                <div className="row" style={{ gap: 6 }}>
                  <input type="number" min={0.1} step={0.1} value={hours} onChange={(e) => setHours(e.target.value)} style={{ width: 90 }} />
                  <span className="tiny">時間後に解除</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {node}
    </Modal>
  );
}

function OverridesModal({ onClose }: { onClose: () => void }) {
  const { data, loading, reload } = useApi<{ overrides: any[] }>("/api/admin/overrides");
  const { run, busy } = useAction();
  const revoke = async (id: number) => {
    if (await run(() => del(`/api/admin/overrides/${id}`, { reason: "手動解除" }), { success: "解除しました" })) reload();
  };
  return (
    <Modal open onClose={onClose} title="一時変更（自動解除）" wide>
      {loading ? <Spinner /> : !data?.overrides.length ? <Empty icon="⏱">一時変更はありません</Empty> : (
        <div className="col" style={{ gap: 6 }}>
          {data.overrides.map((o) => (
            <div key={o.id} className="glass-2 pad-sm col" style={{ gap: 3, opacity: o.active ? 1 : 0.5 }}>
              <div className="row">
                <strong className="small" style={{ flex: 1 }}>{o.entity_type}: {o.entity_key}</strong>
                <span className="chip tiny" style={{ color: o.active ? "var(--warn)" : "var(--text-faint)" }}>{o.active ? "適用中" : "解除済"}</span>
                {o.active && <button className="btn xs danger" disabled={busy} onClick={() => revoke(o.id)}>解除</button>}
              </div>
              <div className="json-view">{JSON.stringify(o.patch)}</div>
              <div className="tiny faint">{o.reason} · 期限 {fmtDate(o.expires_at)}</div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
