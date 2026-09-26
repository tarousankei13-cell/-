/**
 * First-run tutorial: three steps, shown once.
 *
 * The flag lives in the player's server-side settings, so it follows the
 * account rather than the browser — a player who signs in on their phone
 * after starting on a laptop is not taught the game twice.
 */
import { useState } from "react";
import { useGame } from "../store/game";

const STEPS = [
  { icon: "✦", title: "まずは押すだけ", body: "中央の大きなボタンがRollです。押すたびに宇宙からひとつ、アイテムが出てきます。確率はすべてサーバー側で決まります。" },
  { icon: "🌌", title: "Biomeが確率を変える", body: "時間とともに宇宙の様子（Biome）が移り変わり、出やすいものが変わります。珍しいBiomeは狙い目です。" },
  { icon: "📖", title: "集めて、強くなる", body: "手に入れたものは図鑑に残り、装備やセット収集で次のRollのLuckが上がっていきます。下のタブから覗いてみてください。" },
];

export function Tutorial() {
  const me = useGame((s) => s.me);
  const saveSettings = useGame((s) => s.saveSettings);
  const [step, setStep] = useState(0);
  const [done, setDone] = useState(false);

  if (!me || done || me.settings.ui.tutorial_done) return null;

  const finish = () => {
    setDone(true);
    saveSettings({ ui: { ...me.settings.ui, tutorial_done: true } }).catch(() => undefined);
  };

  const s = STEPS[step];
  return (
    <div className="tutorial-backdrop" role="dialog" aria-modal="true" aria-label="はじめかた">
      <div className="tutorial-card glass pad">
        <div className="tutorial-icon" aria-hidden>{s.icon}</div>
        <h2>{s.title}</h2>
        <p className="muted">{s.body}</p>
        <div className="row" style={{ justifyContent: "center", gap: 6 }} aria-hidden>
          {STEPS.map((_, i) => <span key={i} className={`step-dot ${i === step ? "on" : ""}`} />)}
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn ghost sm" onClick={finish}>スキップ</button>
          <span className="spacer" />
          {step > 0 && <button className="btn ghost sm" onClick={() => setStep(step - 1)}>もどる</button>}
          <button className="btn primary" onClick={() => (step === STEPS.length - 1 ? finish() : setStep(step + 1))}>
            {step === STEPS.length - 1 ? "はじめる" : "つぎへ"}
          </button>
        </div>
      </div>
    </div>
  );
}
