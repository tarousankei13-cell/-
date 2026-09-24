import { useState } from "react";
import { useGame } from "../store/game";
import { post } from "../lib/api";
import { audio } from "../audio/engine";

const ERRORS: Record<string, string> = {
  banned: "このアカウントは現在利用できません。",
  oauth_state: "ログインの有効期限が切れました。もう一度お試しください。",
  oauth_failed: "Discord認証に失敗しました。",
  oauth_denied: "Discordログインがキャンセルされました。",
  registration_closed: "現在、新規登録を停止しています。",
  session: "セッションが終了しました。再度ログインしてください。",
};

const FEATURES = [
  { icon: "🎲", title: "サーバー権威のRNG", body: "確率判定はすべてサーバー側。Luck・Biome・装備・Boostが一つの確率テーブルに合成されます。" },
  { icon: "🌌", title: "個人Biome", body: "毎秒抽選される17種のBiome。Void RiftやSingularityでは世界が別物になります。" },
  { icon: "✦", title: "超レア演出", body: "レア度が上がるほど演出は長く豪華に。最高レアは30秒級のクライマックス。" },
  { icon: "🏆", title: "世界初発見", body: "誰も引いたことのないアイテムを最初に引くと、世界中に通知されます。" },
  { icon: "🤝", title: "Market / Trade", body: "プレイヤー同士で売買・交換・贈与。相場も履歴も残ります。" },
  { icon: "🌙", title: "オフラインRoll", body: "ブラウザを閉じている間もAuto Rollが進行。復帰時にまとめて結果を確認。" },
];

export function Landing() {
  const config = useGame((s) => s.config);
  const bootstrap = useGame((s) => s.bootstrap);
  const [devId, setDevId] = useState("1324938326741876758");
  const [busy, setBusy] = useState(false);
  const params = new URLSearchParams(location.search);
  const error = params.get("error");

  const devLogin = async () => {
    setBusy(true);
    try {
      // Discord IDs are 64-bit snowflakes: they must travel as strings or JS number precision truncates them.
      await post("/api/auth/dev-login", { discord_id: devId, username: `dev_${devId.slice(-5)}` });
      await bootstrap();
      history.replaceState(null, "", "/roll");
    } catch (e) {
      useGame.getState().toast("ログインに失敗しました", "error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 940, paddingTop: "min(9vh, 70px)" }}>
      <div className="center col" style={{ gap: 18, alignItems: "center" }}>
        <div className="fx-float" style={{ fontSize: "clamp(3rem, 12vw, 5.6rem)", lineHeight: 1, filter: "drop-shadow(0 0 40px rgba(150,120,255,0.6))" }}>✦</div>
        <h1 style={{
          fontSize: "clamp(2rem, 9vw, 4.2rem)", letterSpacing: "0.16em", fontWeight: 900,
          background: "linear-gradient(90deg, #ffffff, #b58cff 45%, #56c8ff)", WebkitBackgroundClip: "text",
          backgroundClip: "text", color: "transparent",
        }}>
          COSMIC RNG
        </h1>
        <p className="muted" style={{ fontSize: "1.02rem", maxWidth: 560 }}>
          巨大な宇宙の片隅で、あなただけのBiomeが渦を巻く。<br />
          次の1回で、世界にまだ存在しないアイテムが生まれるかもしれない。
        </p>

        {error && (
          <div className="glass pad" style={{ borderColor: "rgba(255,92,122,0.5)", maxWidth: 480 }}>
            {ERRORS[error] ?? "エラーが発生しました。"}
          </div>
        )}

        {config?.maintenance.enabled && (
          <div className="glass pad" style={{ borderColor: "rgba(255,193,77,0.5)", maxWidth: 480 }}>
            🛠 {config.maintenance.message}
          </div>
        )}

        <div className="col" style={{ gap: 10, width: "min(360px, 100%)" }}>
          {config?.discord_login ? (
            <a className="btn primary block" href="/api/auth/login?next=/roll" style={{ minHeight: 52, fontSize: "1.02rem" }}
               onClick={() => audio.sfx("click")}>
              <svg width="22" height="17" viewBox="0 0 71 55" fill="currentColor" aria-hidden="true">
                <path d="M60.1 4.9A58.5 58.5 0 0 0 45.6.4a41 41 0 0 0-1.9 3.8 54.1 54.1 0 0 0-16.2 0A41 41 0 0 0 25.6.4a58.4 58.4 0 0 0-14.5 4.5C1.9 18.6-.6 32 .6 45.2a58.9 58.9 0 0 0 17.8 9 43.7 43.7 0 0 0 3.8-6.2 38.2 38.2 0 0 1-6-2.9l1.5-1.2a42 42 0 0 0 35.9 0l1.5 1.2a38.2 38.2 0 0 1-6 2.9 43.6 43.6 0 0 0 3.8 6.2 58.7 58.7 0 0 0 17.8-9c1.4-15.3-2.4-28.6-10.6-40.3ZM23.7 37.3c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.3 6.4-7.3 6.5 3.3 6.4 7.3c0 4-2.8 7.2-6.4 7.2Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.2s2.8-7.3 6.4-7.3 6.5 3.3 6.4 7.3c0 4-2.8 7.2-6.4 7.2Z" />
              </svg>
              Discordでログイン
            </a>
          ) : (
            <div className="glass pad small muted">Discord OAuthが未設定です。<code className="mono">.env</code> を確認してください。</div>
          )}

          {config?.dev_login && (
            <details className="glass pad-sm" style={{ textAlign: "left" }}>
              <summary className="small muted" style={{ cursor: "pointer" }}>開発用ログイン（本番では無効）</summary>
              <div className="col" style={{ marginTop: 8 }}>
                <input value={devId} onChange={(e) => setDevId(e.target.value.replace(/\D/g, ""))} placeholder="Discord ID" inputMode="numeric" />
                <button className="btn" onClick={devLogin} disabled={busy || !devId}>{busy ? "ログイン中…" : "ログイン"}</button>
              </div>
            </details>
          )}
        </div>
      </div>

      <div className="grid grid-auto" style={{ marginTop: 44 }}>
        {FEATURES.map((f) => (
          <div key={f.title} className="glass pad">
            <div style={{ fontSize: "1.6rem", marginBottom: 6 }}>{f.icon}</div>
            <h3 style={{ marginBottom: 4 }}>{f.title}</h3>
            <p className="muted small" style={{ margin: 0 }}>{f.body}</p>
          </div>
        ))}
      </div>

      {config && (
        <div className="center muted tiny" style={{ marginTop: 30 }}>
          RNG v{config.rng_version} · Content v{config.content_version} · アイテム、Biome、実績は運営により随時追加されます
        </div>
      )}
    </div>
  );
}
