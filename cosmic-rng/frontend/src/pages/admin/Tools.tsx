import { useState } from "react";
import { post } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { Empty, Spinner, Tabs, useConfirm } from "../../components/ui";
import { fmtDate, fmtInt, fmtOdds, fmtPercent } from "../../lib/format";
import type { Bootstrap } from "./Admin";

export function AdminTools({ boot, superAdmin }: { boot: Bootstrap; superAdmin: boolean }) {
  const [tab, setTab] = useState<"simulate" | "broadcast" | "biome" | "backup">("simulate");
  return (
    <div className="col" style={{ gap: 12 }}>
      <Tabs value={tab} onChange={setTab} tabs={[
        { key: "simulate", label: "RNGシミュレーション" }, { key: "biome", label: "Biome移動" },
        { key: "broadcast", label: "全体通知" }, { key: "backup", label: "バックアップ" },
      ]} />
      {tab === "simulate" && <Simulate boot={boot} />}
      {tab === "biome" && <BiomeTool boot={boot} />}
      {tab === "broadcast" && <Broadcast />}
      {tab === "backup" && <Backup superAdmin={superAdmin} />}
    </div>
  );
}

function Simulate({ boot }: { boot: Bootstrap }) {
  const [luck, setLuck] = useState("1");
  const [biome, setBiome] = useState("stellar_drift");
  const [n, setN] = useState("200000");
  const [special, setSpecial] = useState(false);
  const [result, setResult] = useState<any>(null);
  const { run, busy } = useAction();

  const go = async () => {
    setResult(null);
    const res = await run(() => post("/api/admin/rng/simulate", {
      luck: Number(luck), biome_key: biome, n: Number(n), special,
    }));
    if (res) setResult(res);
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad col">
        <h3>RNG Simulation</h3>
        <p className="tiny muted">設定確率と実際の分布を比較し、乖離がないか統計検定（カイ二乗）で検証します。</p>
        <div className="admin-form">
          <div><label>Luck</label><input type="number" step="any" value={luck} onChange={(e) => setLuck(e.target.value)} /></div>
          <div>
            <label>Biome</label>
            <select value={biome} onChange={(e) => setBiome(e.target.value)}>
              {boot.biomes.map((b) => <option key={b.key} value={b.key}>{b.name}</option>)}
            </select>
          </div>
          <div><label>試行回数（最大2,000,000）</label><input type="number" value={n} onChange={(e) => setN(e.target.value)} /></div>
          <div>
            <label>Special Roll として計算</label>
            <label className="switch"><input type="checkbox" checked={special} onChange={(e) => setSpecial(e.target.checked)} /><span className="track" /></label>
          </div>
        </div>
        <button className="btn primary" disabled={busy} onClick={go}>{busy ? "計算中…" : "シミュレーション実行"}</button>
      </div>

      {busy && <Spinner label="大量Rollをシミュレーション中…" />}
      {result && (
        <div className="glass pad col">
          <div className="row-wrap">
            <span className={`chip ${result.verdict === "OK" ? "" : ""}`} style={{ color: result.verdict === "OK" ? "var(--good)" : "var(--bad)" }}>
              {result.verdict === "OK" ? "✓ 理論値と一致" : "⚠ 乖離を検出"}
            </span>
            <span className="chip mono">χ² = {result.chi2.toFixed(1)}</span>
            <span className="chip mono">dof = {result.dof}</span>
            <span className="chip mono">z = {result.chi2_z.toFixed(2)}</span>
            <span className="chip">{fmtInt(result.n)} rolls</span>
          </div>
          <div className="table-wrap" style={{ maxHeight: "52vh" }}>
            <table className="table">
              <thead><tr><th>アイテム</th><th>基礎</th><th>理論確率</th><th>期待値</th><th>実測</th><th>z</th></tr></thead>
              <tbody>
                {result.items.filter((i: any) => i.expected >= 0.5 || i.observed > 0).map((i: any) => (
                  <tr key={i.key}>
                    <td><span className={`r-${i.rarity}`}>{i.name}</span></td>
                    <td className="mono tiny">{fmtOdds(i.odds)}</td>
                    <td className="mono tiny">{fmtPercent(i.p * 100)}</td>
                    <td className="mono tiny">{i.expected.toFixed(1)}</td>
                    <td className="mono">{fmtInt(i.observed)}</td>
                    <td className="mono tiny" style={{ color: Math.abs(i.z) > 3 ? "var(--warn)" : undefined }}>{i.z.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function BiomeTool({ boot }: { boot: Bootstrap }) {
  const [biome, setBiome] = useState("architects_domain");
  const [duration, setDuration] = useState("");
  const { run, busy } = useAction();
  const refreshHud = useGame((s) => s.refreshHud);
  const current = useGame((s) => s.hud?.biome);

  const go = async () => {
    const res = await run(() => post("/api/admin/biome", { biome_key: biome, duration: duration ? Number(duration) : null }),
      { success: "Biomeを変更しました" });
    if (res) refreshHud();
  };

  return (
    <div className="glass pad col">
      <h3>Admin Biome 移動</h3>
      <p className="tiny muted">管理者は Admin Biome を含む任意のBiomeへ自由に移動できます（監査ログに記録されます）。</p>
      <div className="row-wrap">
        <span className="chip">現在: {current?.name ?? "—"}</span>
        {current?.forced && <span className="chip" style={{ color: "var(--gold)" }}>FORCED</span>}
      </div>
      <div className="admin-form">
        <div>
          <label>移動先</label>
          <select value={biome} onChange={(e) => setBiome(e.target.value)}>
            <optgroup label="Admin">{boot.biomes.filter((b) => b.kind === "admin").map((b) => <option key={b.key} value={b.key}>{b.name}</option>)}</optgroup>
            <optgroup label="Natural">{boot.biomes.filter((b) => b.kind !== "admin").map((b) => <option key={b.key} value={b.key}>{b.name}</option>)}</optgroup>
          </select>
        </div>
        <div><label>継続秒数（空=既定）</label><input type="number" value={duration} onChange={(e) => setDuration(e.target.value)} /></div>
      </div>
      <button className="btn gold" disabled={busy} onClick={go}>移動する</button>
    </div>
  );
}

function Broadcast() {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [style, setStyle] = useState("info");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();

  const send = async () => {
    const ok = await confirm("全体通知の送信", `全プレイヤーに通知を送信します。\n\n「${title}」`, { danger: style === "warning" });
    if (!ok) return;
    if (await run(() => post("/api/admin/broadcast", { title, body, style }), { success: "送信しました" })) {
      setTitle("");
      setBody("");
    }
  };

  return (
    <div className="glass pad col">
      <h3>全体通知 / World Broadcast</h3>
      <div><label>タイトル *</label><input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} /></div>
      <div><label>本文</label><textarea value={body} onChange={(e) => setBody(e.target.value)} maxLength={1000} /></div>
      <div>
        <label>スタイル</label>
        <select value={style} onChange={(e) => setStyle(e.target.value)} style={{ width: "auto" }}>
          <option value="info">情報</option><option value="warning">警告</option><option value="cosmic">Cosmic（豪華）</option>
        </select>
      </div>
      <button className="btn primary" disabled={!title.trim() || busy} onClick={send}>送信</button>
      {node}
    </div>
  );
}

function Backup({ superAdmin }: { superAdmin: boolean }) {
  const { data, loading, reload } = useApi<{ backups: any[] }>("/api/admin/backups");
  const [note, setNote] = useState("");
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const toast = useGame((s) => s.toast);

  const create = async () => {
    const res = await run(() => post("/api/admin/backups", { note: note || null }), { success: "バックアップを作成しました" });
    if (res) {
      setNote("");
      reload();
    }
  };

  const restore = async (filename: string) => {
    const ok = await confirm("データベースの復元", (
      <div className="col">
        <p style={{ color: "var(--bad)" }}><strong>警告: この操作は現在のデータベースを完全に置き換えます。</strong></p>
        <p>復元後、全プレイヤーのセッションが無効になり、サーバーが再起動します。</p>
        <div className="mono tiny">{filename}</div>
      </div>
    ), { danger: true, phrase: `RESTORE ${filename}` });
    if (!ok) return;
    const res = await run(() => post("/api/admin/backups/restore", { filename, confirm: `RESTORE ${filename}`, reason: "管理者による復元" }));
    if (res) {
      toast("復元が完了しました。サーバーを再起動しています…", "warning", "数秒後にページを再読み込みします", 12000);
      window.setTimeout(() => location.reload(), 8000);
    }
  };

  const remove = async (filename: string) => {
    const ok = await confirm("バックアップの削除", `${filename} を削除します。`, { danger: true, phrase: `DELETE ${filename}` });
    if (!ok) return;
    if (await run(() => post("/api/admin/backups/delete", { filename, confirm: `DELETE ${filename}` }), { success: "削除しました" })) reload();
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad col">
        <h3>データベースバックアップ</h3>
        {!superAdmin && <div className="tiny" style={{ color: "var(--warn)" }}>作成・復元はスーパー管理者のみ実行できます。</div>}
        <div className="row-wrap">
          <input placeholder="メモ（任意）" value={note} onChange={(e) => setNote(e.target.value)} style={{ flex: "1 1 200px" }} disabled={!superAdmin} />
          <button className="btn primary" disabled={!superAdmin || busy} onClick={create}>今すぐバックアップ</button>
          <button className="btn ghost sm" onClick={() => reload()}>更新</button>
        </div>
        <div className="tiny faint">定期バックアップは systemd timer（cosmic-backup.timer）で自動実行されます。</div>
      </div>

      {loading && !data ? <Spinner /> : !data?.backups.length ? <Empty icon="💾">バックアップがありません</Empty> : (
        <div className="table-wrap glass">
          <table className="table">
            <thead><tr><th>ファイル</th><th>サイズ</th><th>種別</th><th>状態</th><th>作成日時</th><th /></tr></thead>
            <tbody>
              {data.backups.map((b) => (
                <tr key={b.filename}>
                  <td className="mono tiny">{b.filename}</td>
                  <td className="mono tiny">{b.size_bytes ? `${(b.size_bytes / 1048576).toFixed(1)} MB` : "—"}</td>
                  <td className="tiny">{b.kind}</td>
                  <td><span className="chip tiny" style={{ color: b.status === "done" ? "var(--good)" : b.status === "failed" ? "var(--bad)" : undefined }}>{b.status}</span></td>
                  <td className="tiny faint">{b.created_at ? fmtDate(b.created_at) : "—"}</td>
                  <td>
                    {b.file_exists && superAdmin && (
                      <div className="row" style={{ gap: 4 }}>
                        <button className="btn xs danger" disabled={busy} onClick={() => restore(b.filename)}>復元</button>
                        <button className="btn xs ghost" disabled={busy} onClick={() => remove(b.filename)}>削除</button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {node}
    </div>
  );
}
