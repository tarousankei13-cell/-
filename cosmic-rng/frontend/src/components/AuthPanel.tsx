import { useState } from "react";
import { post } from "../lib/api";
import { audio } from "../audio/engine";
import { useGame } from "../store/game";
import { withBase } from "../lib/base";

type Mode = "login" | "register";

/**
 * Account panel on the landing page.
 *
 * Registration and sign-in are one form with a tab switch, because the two
 * differ by a single field and a first-time visitor should not have to work out
 * which one they need.
 */
export function AuthPanel({ onDone }: { onDone: () => void }) {
  const config = useGame((s) => s.config);
  const [mode, setMode] = useState<Mode>("login");
  const [login, setLogin] = useState("");
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const registering = mode === "register";
  const canSubmit = registering
    ? email.trim().length > 2 && username.trim().length >= 3 && password.length >= 8
    : login.trim().length > 0 && password.length > 0;

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!canSubmit || busy) return;
    setBusy(true);
    setError(null);
    audio.sfx("click");
    try {
      if (registering) {
        await post("/api/auth/register", { email: email.trim(), username: username.trim(), password });
      } else {
        await post("/api/auth/login", { login: login.trim(), password });
      }
      setPassword("");
      onDone();
    } catch (err) {
      setError((err as Error).message || "ログインに失敗しました");
      audio.sfx("error");
    } finally {
      setBusy(false);
    }
  };

  const swap = (m: Mode) => {
    setMode(m);
    setError(null);
    audio.sfx("hover");
  };

  return (
    <div className="glass pad auth-panel">
      <div className="auth-tabs" role="tablist">
        <button role="tab" aria-selected={!registering} className={!registering ? "on" : ""}
                onClick={() => swap("login")} type="button">
          ログイン
        </button>
        <button role="tab" aria-selected={registering} className={registering ? "on" : ""}
                onClick={() => swap("register")} type="button"
                disabled={config ? !config.registration_open : false}>
          新規登録
        </button>
      </div>

      <form className="col" style={{ gap: 10, marginTop: 14 }} onSubmit={submit}>
        {registering ? (
          <>
            <label className="field">
              <span>メールアドレス</span>
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                     autoComplete="email" placeholder="you@example.com" required />
            </label>
            <label className="field">
              <span>ユーザー名</span>
              <input value={username} onChange={(e) => setUsername(e.target.value)}
                     autoComplete="username" placeholder="3〜32文字の英数字" required />
            </label>
          </>
        ) : (
          <label className="field">
            <span>メールアドレス または ユーザー名</span>
            <input value={login} onChange={(e) => setLogin(e.target.value)}
                   autoComplete="username" placeholder="you@example.com" required />
          </label>
        )}

        <label className="field">
          <span>パスワード{registering ? "（8文字以上）" : ""}</span>
          <div className="pw-row">
            <input type={show ? "text" : "password"} value={password} onChange={(e) => setPassword(e.target.value)}
                   autoComplete={registering ? "new-password" : "current-password"} required />
            <button type="button" className="btn ghost pw-toggle" onClick={() => setShow((v) => !v)}
                    aria-label={show ? "パスワードを隠す" : "パスワードを表示"}>
              {show ? "🙈" : "👁"}
            </button>
          </div>
        </label>

        {error && <div className="auth-error small">{error}</div>}

        <button className="btn primary block" type="submit" disabled={!canSubmit || busy}
                style={{ minHeight: 48, fontSize: "1rem", marginTop: 2 }}>
          {busy ? "処理中…" : registering ? "アカウントを作成して始める" : "ログイン"}
        </button>
      </form>

      {config && !config.registration_open && !registering && (
        <p className="muted tiny" style={{ marginTop: 10, marginBottom: 0 }}>
          現在、新規登録は停止しています。
        </p>
      )}

      {config?.discord_login && (
        <>
          <div className="auth-sep"><span>または</span></div>
          <a className="btn block" href={withBase("/api/auth/login?next=/roll")} onClick={() => audio.sfx("click")}>
            <svg width="20" height="15" viewBox="0 0 71 55" fill="currentColor" aria-hidden="true">
              <path d="M60.1 4.9A58.5 58.5 0 0 0 45.6.4a41 41 0 0 0-1.9 3.8 54.1 54.1 0 0 0-16.2 0A41 41 0 0 0 25.6.4a58.4 58.4 0 0 0-14.5 4.5C1.9 18.6-.6 32 .6 45.2a58.9 58.9 0 0 0 17.8 9 43.7 43.7 0 0 0 3.8-6.2 38.2 38.2 0 0 1-6-2.9l1.5-1.2a42 42 0 0 0 35.9 0l1.5 1.2a38.2 38.2 0 0 1-6 2.9 43.6 43.6 0 0 0 3.8 6.2 58.7 58.7 0 0 0 17.8-9c1.4-15.3-2.4-28.6-10.6-40.3ZM23.7 37.3c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.3 6.4-7.3 6.5 3.3 6.4 7.3c0 4-2.8 7.2-6.4 7.2Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.3 6.4-7.3 6.5 3.3 6.4 7.3c0 4-2.8 7.2-6.4 7.2Z" />
            </svg>
            Discordでログイン
          </a>
        </>
      )}

      <p className="muted tiny" style={{ marginTop: 12, marginBottom: 0 }}>
        メールアドレスはログインIDとして使います。確認メールは送信されません。
      </p>
    </div>
  );
}
