import { withBase } from "../lib/base";
import { useEffect, useState } from "react";
import { post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";
import { Section, useConfirm } from "../components/ui";
import { FEATURE_LABEL, RARITY_LABEL, fmtInt } from "../lib/format";
import type { PlayerSettings, RarityKey } from "../lib/types";

const TIERS: RarityKey[] = ["common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic"];
const AUTO_SKIP_LEVELS: { unlock: string; value: number; label: string }[] = [
  { unlock: "auto_skip_100", value: 100, label: "1/100 未満をスキップ" },
  { unlock: "auto_skip_1000", value: 1000, label: "1/1,000 未満をスキップ" },
  { unlock: "auto_skip_10000", value: 10000, label: "1/10,000 未満をスキップ" },
];

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 12, flexWrap: "wrap", padding: "6px 0", borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
      <div style={{ flex: "1 1 220px", minWidth: 0 }}>
        <div>{label}</div>
        {hint && <div className="tiny faint">{hint}</div>}
      </div>
      <div style={{ flex: "0 1 260px", display: "flex", justifyContent: "flex-end", gap: 8, alignItems: "center" }}>{children}</div>
    </div>
  );
}

export function Settings() {
  const me = useGame((s) => s.me);
  const saveSettings = useGame((s) => s.saveSettings);
  const hud = useGame((s) => s.hud);
  const { data: unlockData } = useApi<{ settings: PlayerSettings; unlocks: string[] }>("/api/settings");
  const [local, setLocal] = useState<PlayerSettings | null>(me?.settings ?? null);
  const [saving, setSaving] = useState(false);
  const { run } = useAction();
  const { confirm, node } = useConfirm();

  useEffect(() => {
    if (me?.settings) setLocal(me.settings);
  }, [me?.settings]);

  const unlocks = unlockData?.unlocks ?? [];
  const has = (k: string) => unlocks.includes(k);

  const patch = async (p: any) => {
    setLocal((s) => (s ? deepMerge(s, p) : s));
    setSaving(true);
    try {
      await saveSettings(p);
      if (p.audio) audio.setVolumes({ ...audio.volumes, ...p.audio });
    } catch (e) {
      useGame.getState().toast("設定の保存に失敗しました", "error");
    } finally {
      setSaving(false);
    }
  };

  const logout = async () => {
    const ok = await confirm("ログアウト", "ログアウトしますか？");
    if (!ok) return;
    await run(() => post("/api/auth/logout"));
    location.href = withBase("/");
  };

  if (!local) return null;

  return (
    <div className="page" style={{ maxWidth: 880 }}>
      <div className="page-head">
        <div>
          <h1>設定<span className="h1-en">Settings</span></h1>
          <div className="sub">変更は即座に保存されます{saving ? " · 保存中…" : ""}</div>
        </div>
      </div>

      <div className="col" style={{ gap: 14 }}>
        <Section title="🔊 オーディオ">
          <Row label="ミュート">
            <label className="switch"><input type="checkbox" checked={local.audio.muted} onChange={(e) => patch({ audio: { muted: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="マスター音量">
            <input type="range" min={0} max={1} step={0.05} value={local.audio.master} onChange={(e) => patch({ audio: { master: Number(e.target.value) } })} />
            <span className="mono tiny" style={{ width: 34 }}>{Math.round(local.audio.master * 100)}%</span>
          </Row>
          <Row label="BGM">
            <input type="range" min={0} max={1} step={0.05} value={local.audio.bgm} onChange={(e) => patch({ audio: { bgm: Number(e.target.value) } })} />
            <span className="mono tiny" style={{ width: 34 }}>{Math.round(local.audio.bgm * 100)}%</span>
          </Row>
          <Row label="効果音">
            <input type="range" min={0} max={1} step={0.05} value={local.audio.sfx} onChange={(e) => patch({ audio: { sfx: Number(e.target.value) } })} />
            <span className="mono tiny" style={{ width: 34 }}>{Math.round(local.audio.sfx * 100)}%</span>
          </Row>
          <Row label="テスト再生"><button className="btn sm ghost" onClick={() => audio.sfx("reveal_legendary")}>▶ 再生</button></Row>
        </Section>

        <Section title="🎨 グラフィック">
          <Row label="演出品質" hint="低スペック端末では Low / Minimal を推奨">
            <select value={local.graphics.quality} onChange={(e) => patch({ graphics: { quality: e.target.value } })} style={{ width: "auto" }}>
              <option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option><option value="minimal">Minimal</option>
            </select>
          </Row>
          <Row label="パーティクル量">
            <input type="range" min={0} max={1.5} step={0.1} value={local.graphics.particles} onChange={(e) => patch({ graphics: { particles: Number(e.target.value) } })} />
            <span className="mono tiny" style={{ width: 34 }}>{Math.round(local.graphics.particles * 100)}%</span>
          </Row>
          <Row label="アニメーション軽減" hint="動きを最小限にします">
            <label className="switch"><input type="checkbox" checked={local.graphics.reduced_motion} onChange={(e) => patch({ graphics: { reduced_motion: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="画面シェイク">
            <label className="switch"><input type="checkbox" checked={local.graphics.screen_shake} onChange={(e) => patch({ graphics: { screen_shake: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="背景エフェクト">
            <label className="switch"><input type="checkbox" checked={local.graphics.background_fx} onChange={(e) => patch({ graphics: { background_fx: e.target.checked } })} /><span className="track" /></label>
          </Row>
        </Section>

        <Section title="✦ Roll と演出">
          <Row label="Roll速度" hint={!has("fast_mode") ? "Fast Mode Module をショップで購入すると解放" : undefined}>
            <select value={local.roll.speed} onChange={(e) => patch({ roll: { speed: e.target.value } })} style={{ width: "auto" }}>
              <option value="normal">Normal</option>
              <option value="fast" disabled={!has("fast_mode")}>Fast</option>
              <option value="ultra" disabled={!has("ultra_fast")}>Ultra Fast</option>
            </select>
          </Row>
          <Row label="演出を再生する">
            <label className="switch"><input type="checkbox" checked={local.roll.cutscenes} onChange={(e) => patch({ roll: { cutscenes: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="フル演出の最低レア度" hint="これ以上のレア度は速度設定に関わらずフル演出">
            <select value={local.roll.full_cutscene_min_tier} onChange={(e) => patch({ roll: { full_cutscene_min_tier: e.target.value } })} style={{ width: "auto" }}>
              {TIERS.map((t) => <option key={t} value={t}>{RARITY_LABEL[t]}</option>)}
            </select>
          </Row>
          <Row label="スキップ確認の最低レア度" hint="これ以上では誤スキップ防止の確認が入ります">
            <select value={local.roll.skip_confirm_min_tier} onChange={(e) => patch({ roll: { skip_confirm_min_tier: e.target.value } })} style={{ width: "auto" }}>
              {TIERS.map((t) => <option key={t} value={t}>{RARITY_LABEL[t]}</option>)}
            </select>
          </Row>
        </Section>

        <Section title="⏭ Auto Skip（ショップ商品）">
          <Row label="Auto Skipを有効化" hint={unlocks.some((u) => u.startsWith("auto_skip")) ? undefined : "Filter Labで購入すると解放されます"}>
            <label className="switch">
              <input type="checkbox" disabled={!unlocks.some((u) => u.startsWith("auto_skip"))} checked={local.auto_skip.enabled}
                onChange={(e) => patch({ auto_skip: { enabled: e.target.checked } })} /><span className="track" />
            </label>
          </Row>
          <Row label="スキップ閾値">
            {has("auto_skip_custom") ? (
              <input type="number" min={2} value={local.auto_skip.threshold} style={{ width: 130 }}
                onChange={(e) => patch({ auto_skip: { threshold: Math.max(2, Number(e.target.value) || 2) } })} />
            ) : (
              <select value={local.auto_skip.threshold} disabled={!local.auto_skip.enabled}
                onChange={(e) => patch({ auto_skip: { threshold: Number(e.target.value) } })} style={{ width: "auto" }}>
                {AUTO_SKIP_LEVELS.filter((l) => has(l.unlock)).map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
                {!AUTO_SKIP_LEVELS.some((l) => has(l.unlock)) && <option value={100}>未解放</option>}
              </select>
            )}
          </Row>
        </Section>

        <Section title="🗑 自動削除フィルター" right={hud && !hud.unlocked.includes("auto_delete") ? <span className="chip tiny">Lv.2で解放</span> : undefined}>
          <div className="tiny muted">Auto Skip（表示スキップ）とは別機能です。削除されたアイテムはインベントリに入りませんが、Roll数にはカウントされます。</div>
          <Row label="自動削除を有効化">
            <label className="switch">
              <input type="checkbox" disabled={!hud?.unlocked.includes("auto_delete")} checked={local.auto_delete.enabled}
                onChange={(e) => patch({ auto_delete: { enabled: e.target.checked } })} /><span className="track" />
            </label>
          </Row>
          <Row label="処理方法">
            <select value={local.auto_delete.mode} onChange={(e) => patch({ auto_delete: { mode: e.target.value } })} style={{ width: "auto" }}>
              <option value="sell">自動売却（Stardustを獲得）</option>
              <option value="delete">完全に削除</option>
            </select>
          </Row>
          <Row label="この確率以下を削除" hint={`現在: 1/${fmtInt(local.auto_delete.max_odds)} 以下`}>
            <select value={local.auto_delete.max_odds} onChange={(e) => patch({ auto_delete: { max_odds: Number(e.target.value) } })} style={{ width: "auto" }}>
              {[1, 10, 100, 1000, 10000, 100000].map((v) => <option key={v} value={v}>1 / {fmtInt(v)} 以下</option>)}
            </select>
          </Row>
          <Row label="レア度でも削除" hint="指定したレア度は確率に関わらず削除">
            <div className="row-wrap" style={{ gap: 4, justifyContent: "flex-end" }}>
              {TIERS.slice(0, 4).map((t) => (
                <button key={t} className={`chip tiny ${local.auto_delete.tiers.includes(t) ? "on" : ""} r-${t}`}
                  onClick={() => patch({ auto_delete: { tiers: local.auto_delete.tiers.includes(t) ? local.auto_delete.tiers.filter((x) => x !== t) : [...local.auto_delete.tiers, t] } })}>
                  {t}
                </button>
              ))}
            </div>
          </Row>
          <Row label="未発見アイテムを保護" hint="初めて引いたアイテムは削除しない">
            <label className="switch"><input type="checkbox" checked={local.auto_delete.protect_new} onChange={(e) => patch({ auto_delete: { protect_new: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="削除されたアイテムを結果に表示">
            <label className="switch"><input type="checkbox" checked={local.auto_delete.show_deleted} onChange={(e) => patch({ auto_delete: { show_deleted: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <div className="tiny" style={{ color: "var(--good)" }}>⭐ お気に入り登録したアイテムは常に保護されます。</div>
        </Section>

        <Section title="🔔 通知">
          <Row label="World Feedを表示">
            <label className="switch"><input type="checkbox" checked={local.notifications.world_feed} onChange={(e) => patch({ notifications: { world_feed: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="トースト通知">
            <label className="switch"><input type="checkbox" checked={local.notifications.toasts} onChange={(e) => patch({ notifications: { toasts: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="トレード通知">
            <label className="switch"><input type="checkbox" checked={local.notifications.trades} onChange={(e) => patch({ notifications: { trades: e.target.checked } })} /><span className="track" /></label>
          </Row>
        </Section>

        <Section title="🔐 プライバシー">
          <Row label="プロフィールを公開">
            <label className="switch"><input type="checkbox" checked={local.privacy.public_profile} onChange={(e) => patch({ privacy: { public_profile: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="獲得情報をWorld Feedに公開" hint="OFFにすると匿名で表示されます">
            <label className="switch"><input type="checkbox" checked={local.privacy.public_drops} onChange={(e) => patch({ privacy: { public_drops: e.target.checked } })} /><span className="track" /></label>
          </Row>
          <Row label="トレード相手にインベントリを公開">
            <label className="switch"><input type="checkbox" checked={local.privacy.show_inventory} onChange={(e) => patch({ privacy: { show_inventory: e.target.checked } })} /><span className="track" /></label>
          </Row>
        </Section>

        <Section title="♿ アクセシビリティ">
          <Row label="文字サイズ">
            <input type="range" min={0.8} max={1.4} step={0.05} value={local.ui.font_scale} onChange={(e) => patch({ ui: { font_scale: Number(e.target.value) } })} />
            <span className="mono tiny" style={{ width: 40 }}>{Math.round(local.ui.font_scale * 100)}%</span>
          </Row>
          <Row label="ハイコントラスト">
            <label className="switch"><input type="checkbox" checked={local.ui.high_contrast} onChange={(e) => patch({ ui: { high_contrast: e.target.checked } })} /><span className="track" /></label>
          </Row>
        </Section>

        <Section title="🔓 解放済み機能">
          <div className="row-wrap">
            {(hud?.unlocked ?? []).map((f) => <span key={f} className="chip tiny">{FEATURE_LABEL[f] ?? f}</span>)}
          </div>
          {unlocks.length > 0 && (
            <>
              <div className="tiny faint" style={{ marginTop: 8 }}>購入済みアップグレード</div>
              <div className="row-wrap">{unlocks.map((u) => <span key={u} className="chip tiny" style={{ color: "var(--gold)" }}>{u}</span>)}</div>
            </>
          )}
        </Section>

        <Section title="アカウント">
          <Row label="ユーザー名"><span className="mono tiny">{me?.user.username}</span></Row>
          {me?.user.email && <Row label="メールアドレス"><span className="mono tiny">{me.user.email}</span></Row>}
          {me?.user.discord_id && <Row label="Discord ID"><span className="mono tiny">{me.user.discord_id}</span></Row>}
          {me?.user.email && <PasswordChange />}
          <Row label="ログアウト"><button className="btn sm danger" onClick={logout}>ログアウト</button></Row>
        </Section>
      </div>
      {node}
    </div>
  );
}

function PasswordChange() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useGame((s) => s.toast);

  const mismatch = again.length > 0 && next !== again;
  const ready = current.length > 0 && next.length >= 8 && next === again && !busy;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/password", { current_password: current, new_password: next });
      setCurrent(""); setNext(""); setAgain(""); setOpen(false);
      toast("パスワードを変更しました", "success", "他の端末のログインは無効になりました");
      audio.sfx("success");
    } catch (e) {
      setError((e as Error).message);
      audio.sfx("error");
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <Row label="パスワード">
        <button className="btn sm" onClick={() => setOpen(true)}>変更する</button>
      </Row>
    );
  }
  return (
    <div style={{ padding: "10px 0", borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
      <div className="col" style={{ gap: 8, maxWidth: 340 }}>
        <label className="field">
          <span>現在のパスワード</span>
          <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
        </label>
        <label className="field">
          <span>新しいパスワード（8文字以上）</span>
          <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
        </label>
        <label className="field">
          <span>新しいパスワード（確認）</span>
          <input type="password" value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="new-password" />
        </label>
        {mismatch && <div className="auth-error small">確認用のパスワードが一致しません</div>}
        {error && <div className="auth-error small">{error}</div>}
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm primary" onClick={submit} disabled={!ready}>{busy ? "変更中…" : "変更する"}</button>
          <button className="btn sm ghost" onClick={() => { setOpen(false); setError(null); }}>キャンセル</button>
        </div>
        <p className="muted tiny" style={{ margin: 0 }}>変更すると、この端末以外のログインはすべて無効になります。</p>
      </div>
    </div>
  );
}

function deepMerge<T extends Record<string, any>>(base: T, patch: Record<string, any>): T {
  const out: any = { ...base };
  for (const [k, v] of Object.entries(patch)) {
    out[k] = v && typeof v === "object" && !Array.isArray(v) && typeof out[k] === "object" ? deepMerge(out[k], v) : v;
  }
  return out;
}
