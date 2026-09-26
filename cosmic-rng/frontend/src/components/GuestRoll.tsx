/**
 * Try before you sign up.
 *
 * The rolls are real: the server draws them from the same table a level-1
 * account rolls on, keeps the tally in a signed cookie, and hands the results
 * over to whatever account the visitor creates next.
 */
import { useEffect, useState } from "react";
import { get, post, ApiError } from "../lib/api";
import { ItemIcon } from "./ItemIcon";
import { fmtOdds } from "../lib/format";

type GuestState = { used: number; limit: number; remaining: number; enabled: boolean };
type GuestRoll = { item: any; odds: number; final_odds: number | null; remaining: number; fortune: { label_ja?: string; label: string } };

export function GuestRoll() {
  const [state, setState] = useState<GuestState | null>(null);
  const [last, setLast] = useState<GuestRoll | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    get<GuestState>("/api/guest/state").then(setState).catch(() => setState(null));
  }, []);

  if (!state?.enabled) return null;

  const roll = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await post<GuestRoll>("/api/guest/roll");
      setLast(res);
      setState((s) => (s ? { ...s, used: s.used + 1, remaining: res.remaining } : s));
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const done = state.remaining <= 0;
  return (
    <div className="glass pad guest-panel">
      <div className="tiny faint" style={{ letterSpacing: "0.18em" }}>登録なしで試す</div>
      {last ? (
        <div className="guest-result">
          <ItemIcon visual={last.item.visual} tier={last.item.tier} size={52} />
          <div className="col" style={{ gap: 1, flex: 1, minWidth: 0, textAlign: "left" }}>
            <span className={`r-${last.item.rarity}`} style={{ fontWeight: 700 }}>{last.item.name_ja || last.item.name}</span>
            <span className="tiny mono faint">{fmtOdds(last.odds)}</span>
          </div>
          <span className="tiny" style={{ color: "var(--gold)" }}>{last.fortune?.label_ja || last.fortune?.label}</span>
        </div>
      ) : (
        <p className="muted small" style={{ margin: 0 }}>
          アカウントを作る前に {state.limit} 回だけ引けます。引いたものは、そのまま登録後のアカウントに引き継がれます。
        </p>
      )}
      {error && <div className="small" style={{ color: "var(--bad)" }}>{error}</div>}
      <button className="btn primary block" disabled={busy || done} onClick={roll}>
        {done ? "お試しはここまで — 登録して続きから" : busy ? "…" : `お試しRoll（あと ${state.remaining} 回）`}
      </button>
      {state.used > 0 && (
        <span className="tiny faint">引いた {state.used} 個は、下のフォームで登録すると受け取れます。</span>
      )}
    </div>
  );
}
