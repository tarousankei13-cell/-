import { type ReactNode, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ItemIcon } from "./ItemIcon";
import { FEATURE_LABEL, RARITY_LABEL, fmtInt, fmtOdds, timeAgo } from "../lib/format";
import type { ItemInfo, RarityKey, UserBrief } from "../lib/types";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";

// --------------------------------------------------------------------- Modal
export function Modal({
  open, onClose, title, children, footer, wide, closeOnBackdrop = true,
}: { open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean; closeOnBackdrop?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={(e) => closeOnBackdrop && e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" style={wide ? { width: "min(1060px, 100%)" } : undefined} ref={ref} tabIndex={-1}>
        {title && (
          <div className="modal-head">
            <h3 style={{ flex: 1 }}>{title}</h3>
            <button className="icon-btn" onClick={onClose} aria-label="閉じる">✕</button>
          </div>
        )}
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- Confirm
export function useConfirm() {
  const [state, setState] = useState<{ open: boolean; title: string; body: ReactNode; danger?: boolean; phrase?: string; resolve?: (v: boolean) => void }>({
    open: false, title: "", body: null,
  });
  const [typed, setTyped] = useState("");
  const confirm = (title: string, body: ReactNode, opts: { danger?: boolean; phrase?: string } = {}) =>
    new Promise<boolean>((resolve) => {
      setTyped("");
      setState({ open: true, title, body, danger: opts.danger, phrase: opts.phrase, resolve });
    });
  const close = (v: boolean) => {
    state.resolve?.(v);
    setState((s) => ({ ...s, open: false, resolve: undefined }));
  };
  const node = (
    <Modal
      open={state.open}
      onClose={() => close(false)}
      title={state.title}
      footer={
        <>
          <button className="btn ghost" onClick={() => close(false)}>キャンセル</button>
          <button
            className={`btn ${state.danger ? "danger" : "primary"}`}
            disabled={!!state.phrase && typed !== state.phrase}
            onClick={() => close(true)}
          >
            実行する
          </button>
        </>
      }
    >
      <div className="col">
        <div>{state.body}</div>
        {state.phrase && (
          <div>
            <label>確認のため <code className="mono" style={{ color: "var(--warn)" }}>{state.phrase}</code> と入力してください</label>
            <input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus spellCheck={false} />
          </div>
        )}
      </div>
    </Modal>
  );
  return { confirm, node };
}

// -------------------------------------------------------------------- Badges
export function RarityBadge({ rarity, tier }: { rarity: RarityKey; tier?: number }) {
  const t = tier ?? 1;
  return (
    <span className={`badge r-${rarity}`} style={t >= 6 ? { boxShadow: "0 0 14px currentColor" } : undefined}>
      {RARITY_LABEL[rarity] ?? rarity}
    </span>
  );
}

export function Stat({ k, v, color }: { k: string; v: ReactNode; color?: string }) {
  return (
    <div className="stat">
      <span className="k">{k}</span>
      <span className="v" style={color ? { color } : undefined}>{v}</span>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="center" style={{ padding: 40 }}>
      <div style={{
        width: 42, height: 42, margin: "0 auto 12px", borderRadius: "50%",
        border: "3px solid rgba(255,255,255,0.12)", borderTopColor: "var(--accent)", animation: "spin 0.9s linear infinite",
      }} />
      {label && <div className="muted small">{label}</div>}
    </div>
  );
}

export function Empty({ children, icon = "✦" }: { children: ReactNode; icon?: string }) {
  return (
    <div className="empty">
      <div style={{ fontSize: "2rem", marginBottom: 8, opacity: 0.5 }}>{icon}</div>
      {children}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="glass pad" style={{ borderColor: "rgba(255,92,122,0.45)" }}>
      <div className="row">
        <span style={{ fontSize: "1.2rem" }}>⚠</span>
        <div style={{ flex: 1 }}>{error}</div>
        {onRetry && <button className="btn sm" onClick={onRetry}>再試行</button>}
      </div>
    </div>
  );
}

// -------------------------------------------------------------- Feature lock
export function LockedFeature({ feature }: { feature: string }) {
  const unlocks = useGame((s) => s.config?.unlocks);
  const level = unlocks?.[feature] ?? 1;
  return (
    <div className="glass locked-note">
      <div style={{ fontSize: "2.4rem" }}>🔒</div>
      <h2>{FEATURE_LABEL[feature] ?? feature} はまだ解放されていません</h2>
      <div className="lv">Lv.{level}</div>
      <p className="muted">Rollを重ねてレベルを上げると解放されます。</p>
      <Link className="btn primary" to="/roll">Rollへ戻る</Link>
    </div>
  );
}

// ------------------------------------------------------------------ ItemCard
export function ItemCard({
  item, count, onClick, footer, corner, selected, compact,
}: { item: ItemInfo; count?: number; onClick?: () => void; footer?: ReactNode; corner?: ReactNode; selected?: boolean; compact?: boolean }) {
  return (
    <button
      type="button"
      className={`item-card t${item.tier}`}
      style={selected ? { borderColor: "var(--accent)", boxShadow: "0 0 0 2px var(--accent-glow)" } : undefined}
      onClick={() => {
        audio.sfx("click");
        onClick?.();
      }}
    >
      {corner && <div className="corner">{corner}</div>}
      <div className="row" style={{ gap: 10 }}>
        <ItemIcon visual={item.visual} tier={item.tier} size={compact ? 38 : 50} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className={`name r-${item.rarity}`}>{item.name}</div>
          <div className="odds mono">{fmtOdds(item.odds, item.display_odds)}</div>
        </div>
        {count !== undefined && count > 1 && (
          <div className="mono" style={{ fontWeight: 700, fontSize: "0.95rem" }}>×{fmtInt(count)}</div>
        )}
      </div>
      {footer}
    </button>
  );
}

// ------------------------------------------------------------------ UserChip
export function UserChip({ user, size = 22 }: { user: UserBrief | null | undefined; size?: number }) {
  if (!user) return <span className="muted">—</span>;
  return (
    <Link to={`/profile/${user.id}`} className="row" style={{ gap: 6, color: "inherit", textDecoration: "none" }}>
      <Avatar user={user} size={size} />
      <span className="ellipsis" style={{ maxWidth: 160 }}>{user.name}</span>
    </Link>
  );
}

export function Avatar({ user, size = 32 }: { user: { avatar?: string | null; name: string } | null; size?: number }) {
  if (!user) return null;
  if (user.avatar) {
    return <img src={user.avatar} alt="" width={size} height={size} style={{ borderRadius: "50%", flex: "none", border: "1px solid var(--line)" }} loading="lazy" />;
  }
  return (
    <div
      aria-hidden
      style={{
        width: size, height: size, borderRadius: "50%", flex: "none", display: "grid", placeItems: "center",
        background: "linear-gradient(135deg, rgba(120,150,255,0.5), rgba(160,90,220,0.5))",
        fontSize: size * 0.45, fontWeight: 700, border: "1px solid var(--line)",
      }}
    >
      {(user.name || "?").slice(0, 1).toUpperCase()}
    </div>
  );
}

// ---------------------------------------------------------------- Countdown
export function Countdown({ to, onDone, prefix = "" }: { to: string | null | undefined; onDone?: () => void; prefix?: string }) {
  const [, force] = useState(0);
  const done = useRef(false);
  useEffect(() => {
    done.current = false;
    const id = window.setInterval(() => force((n) => n + 1), 500);
    return () => window.clearInterval(id);
  }, [to]);
  if (!to) return null;
  const ms = new Date(to).getTime() - Date.now();
  if (ms <= 0) {
    if (!done.current) {
      done.current = true;
      onDone?.();
    }
    return null;
  }
  const s = Math.ceil(ms / 1000);
  const m = Math.floor(s / 60);
  return <span className="mono">{prefix}{m > 0 ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`}</span>;
}

// ------------------------------------------------------------------- Tabs
export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { key: T; label: string; badge?: number }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="row-wrap" style={{ gap: 6, overflowX: "auto", paddingBottom: 2 }}>
      {tabs.map((t) => (
        <button
          key={t.key}
          className={`btn sm ${value === t.key ? "active" : "ghost"}`}
          onClick={() => {
            audio.sfx("click");
            onChange(t.key);
          }}
        >
          {t.label}
          {t.badge ? <span className="badge" style={{ borderColor: "var(--bad)", color: "var(--bad)" }}>{t.badge}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function Section({ title, right, children }: { title: ReactNode; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="glass pad" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="row">
        <h3 style={{ flex: 1 }}>{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

export function TimeAgo({ iso }: { iso: string | null | undefined }) {
  return <span className="faint tiny">{timeAgo(iso)}</span>;
}
