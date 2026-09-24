/**
 * Procedural audio engine (Web Audio API).
 *
 * All music and sound effects are synthesised at runtime — no asset downloads,
 * no licensing, and the BGM can morph continuously with the biome. Everything
 * is created lazily after the first user gesture (browser autoplay policy).
 */

type SfxName =
  | "click" | "hover" | "roll_start" | "roll_tick" | "reveal_common" | "reveal_rare" | "reveal_epic"
  | "reveal_legendary" | "reveal_secret" | "reveal_ultra" | "reveal_mythic" | "reveal_admin"
  | "charge" | "impact" | "shatter" | "sparkle" | "whoosh" | "heartbeat" | "error" | "success"
  | "coin" | "craft" | "equip" | "level_up" | "quest" | "world_notice" | "biome_shift" | "biome_end"
  | "first_discovery" | "glitch" | "void" | "time";

export interface BgmProfile {
  root: number;          // base frequency (Hz)
  scale: number[];       // semitone offsets
  tempo: number;         // seconds per step
  padGain: number;
  bellGain: number;
  bassGain: number;
  filter: number;        // low-pass cutoff
  detune: number;
  waveform: OscillatorType;
  shimmer: number;
}

const BGM_PROFILES: Record<string, BgmProfile> = {
  drift:       { root: 110.0, scale: [0, 3, 5, 7, 10], tempo: 3.4, padGain: 0.30, bellGain: 0.22, bassGain: 0.22, filter: 900,  detune: 6,  waveform: "sine",     shimmer: 0.4 },
  bloom:       { root: 130.8, scale: [0, 2, 4, 7, 9],  tempo: 2.6, padGain: 0.34, bellGain: 0.30, bassGain: 0.20, filter: 1400, detune: 9,  waveform: "triangle", shimmer: 0.7 },
  solar:       { root: 98.0,  scale: [0, 2, 3, 7, 8],  tempo: 1.9, padGain: 0.32, bellGain: 0.26, bassGain: 0.34, filter: 1800, detune: 14, waveform: "sawtooth", shimmer: 0.5 },
  storm:       { root: 87.3,  scale: [0, 1, 5, 7, 8],  tempo: 1.6, padGain: 0.30, bellGain: 0.22, bassGain: 0.36, filter: 1600, detune: 16, waveform: "sawtooth", shimmer: 0.3 },
  aurora:      { root: 146.8, scale: [0, 2, 4, 6, 9],  tempo: 3.0, padGain: 0.36, bellGain: 0.34, bassGain: 0.18, filter: 2200, detune: 8,  waveform: "sine",     shimmer: 0.9 },
  frost:       { root: 155.6, scale: [0, 2, 3, 7, 10], tempo: 3.6, padGain: 0.30, bellGain: 0.36, bassGain: 0.16, filter: 2600, detune: 5,  waveform: "sine",     shimmer: 1.0 },
  eclipse:     { root: 82.4,  scale: [0, 1, 4, 6, 8],  tempo: 2.2, padGain: 0.36, bellGain: 0.20, bassGain: 0.38, filter: 700,  detune: 18, waveform: "sawtooth", shimmer: 0.2 },
  quantum:     { root: 164.8, scale: [0, 1, 3, 6, 10], tempo: 1.4, padGain: 0.26, bellGain: 0.32, bassGain: 0.22, filter: 2800, detune: 22, waveform: "square",   shimmer: 0.8 },
  starfall:    { root: 174.6, scale: [0, 4, 7, 9, 11], tempo: 2.0, padGain: 0.34, bellGain: 0.40, bassGain: 0.22, filter: 2400, detune: 7,  waveform: "triangle", shimmer: 1.0 },
  void:        { root: 61.7,  scale: [0, 1, 6, 7, 11], tempo: 2.8, padGain: 0.40, bellGain: 0.18, bassGain: 0.44, filter: 480,  detune: 24, waveform: "sawtooth", shimmer: 0.2 },
  singularity: { root: 55.0,  scale: [0, 2, 6, 8, 11], tempo: 3.2, padGain: 0.44, bellGain: 0.24, bassGain: 0.48, filter: 380,  detune: 28, waveform: "sawtooth", shimmer: 0.3 },
  genesis:     { root: 196.0, scale: [0, 4, 7, 11, 14], tempo: 2.4, padGain: 0.40, bellGain: 0.44, bassGain: 0.20, filter: 3200, detune: 4, waveform: "sine",     shimmer: 1.0 },
  architect:   { root: 138.6, scale: [0, 4, 7, 9, 12], tempo: 2.2, padGain: 0.36, bellGain: 0.38, bassGain: 0.26, filter: 2600, detune: 3,  waveform: "triangle", shimmer: 0.8 },
  sanctum:     { root: 58.3,  scale: [0, 1, 5, 6, 11], tempo: 3.0, padGain: 0.44, bellGain: 0.22, bassGain: 0.46, filter: 420,  detune: 26, waveform: "sawtooth", shimmer: 0.4 },
  fracture:    { root: 123.5, scale: [0, 1, 2, 6, 7],  tempo: 1.2, padGain: 0.30, bellGain: 0.28, bassGain: 0.30, filter: 2000, detune: 30, waveform: "square",   shimmer: 0.6 },
  source:      { root: 65.4,  scale: [0, 3, 7, 10, 12], tempo: 1.8, padGain: 0.34, bellGain: 0.34, bassGain: 0.36, filter: 1800, detune: 12, waveform: "square",  shimmer: 0.7 },
  chrono:      { root: 116.5, scale: [0, 2, 5, 7, 9],  tempo: 2.0, padGain: 0.32, bellGain: 0.32, bassGain: 0.24, filter: 1900, detune: 6,  waveform: "triangle", shimmer: 0.6 },
};

class AudioEngine {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private bgmBus: GainNode | null = null;
  private sfxBus: GainNode | null = null;
  private bgmNodes: { osc: OscillatorNode[]; gains: GainNode[]; filter: BiquadFilterNode; lfo: OscillatorNode } | null = null;
  private stepTimer: number | undefined;
  private profileKey = "";
  private profile: BgmProfile = BGM_PROFILES.drift;
  private noiseBuffer: AudioBuffer | null = null;
  private enabled = false;
  private duckUntil = 0;

  volumes = { master: 0.8, bgm: 0.5, sfx: 0.8, muted: false };

  get ready() {
    return this.ctx !== null && this.ctx.state === "running";
  }

  /** Must be called from a user gesture. */
  async unlock(): Promise<boolean> {
    if (!this.ctx) {
      const Ctx = window.AudioContext || (window as any).webkitAudioContext;
      if (!Ctx) return false;
      try {
        this.ctx = new Ctx();
      } catch {
        return false;
      }
      this.master = this.ctx.createGain();
      this.master.gain.value = this.volumes.muted ? 0 : this.volumes.master;
      this.master.connect(this.ctx.destination);
      this.bgmBus = this.ctx.createGain();
      this.bgmBus.gain.value = this.volumes.bgm;
      this.bgmBus.connect(this.master);
      this.sfxBus = this.ctx.createGain();
      this.sfxBus.gain.value = this.volumes.sfx;
      this.sfxBus.connect(this.master);
      this.makeNoise();
    }
    if (this.ctx.state === "suspended") await this.ctx.resume().catch(() => undefined);
    this.enabled = this.ctx.state === "running";
    return this.enabled;
  }

  setVolumes(v: Partial<typeof this.volumes>) {
    Object.assign(this.volumes, v);
    if (!this.ctx || !this.master || !this.bgmBus || !this.sfxBus) return;
    const t = this.ctx.currentTime;
    this.master.gain.setTargetAtTime(this.volumes.muted ? 0 : this.volumes.master, t, 0.08);
    this.bgmBus.gain.setTargetAtTime(this.volumes.bgm, t, 0.08);
    this.sfxBus.gain.setTargetAtTime(this.volumes.sfx, t, 0.08);
  }

  private makeNoise() {
    if (!this.ctx) return;
    const len = this.ctx.sampleRate * 2;
    const buf = this.ctx.createBuffer(1, len, this.ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    this.noiseBuffer = buf;
  }

  // ----------------------------------------------------------------- BGM
  startBgm(key: string) {
    if (!this.enabled || !this.ctx || !this.bgmBus) return;
    const profile = BGM_PROFILES[key] ?? BGM_PROFILES.drift;
    if (this.bgmNodes && this.profileKey === key) return;
    this.profileKey = key;
    this.profile = profile;
    if (this.bgmNodes) {
      this.morphTo(profile);
      return;
    }
    const ctx = this.ctx;
    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.value = profile.filter;
    filter.Q.value = 0.8;
    filter.connect(this.bgmBus);

    const osc: OscillatorNode[] = [];
    const gains: GainNode[] = [];
    // 3 detuned pad voices + sub bass
    const voices = [
      { f: profile.root, g: profile.padGain, d: 0 },
      { f: profile.root * 1.5, g: profile.padGain * 0.55, d: profile.detune },
      { f: profile.root * 2, g: profile.padGain * 0.35, d: -profile.detune },
      { f: profile.root / 2, g: profile.bassGain, d: 0 },
    ];
    for (const v of voices) {
      const o = ctx.createOscillator();
      o.type = profile.waveform;
      o.frequency.value = v.f;
      o.detune.value = v.d;
      const g = ctx.createGain();
      g.gain.value = 0;
      o.connect(g).connect(filter);
      o.start();
      g.gain.setTargetAtTime(v.g * 0.25, ctx.currentTime, 1.6);
      osc.push(o);
      gains.push(g);
    }
    // slow shimmer LFO on the filter
    const lfo = ctx.createOscillator();
    lfo.frequency.value = 0.05 + profile.shimmer * 0.06;
    const lfoGain = ctx.createGain();
    lfoGain.gain.value = profile.filter * 0.35 * profile.shimmer;
    lfo.connect(lfoGain).connect(filter.frequency);
    lfo.start();

    this.bgmNodes = { osc, gains, filter, lfo };
    this.scheduleSteps();
  }

  private morphTo(profile: BgmProfile) {
    if (!this.ctx || !this.bgmNodes) return;
    const t = this.ctx.currentTime;
    const { osc, gains, filter, lfo } = this.bgmNodes;
    const targets = [profile.root, profile.root * 1.5, profile.root * 2, profile.root / 2];
    const levels = [profile.padGain * 0.25, profile.padGain * 0.14, profile.padGain * 0.09, profile.bassGain * 0.25];
    osc.forEach((o, i) => {
      o.frequency.setTargetAtTime(targets[i], t, 1.2);
      o.detune.setTargetAtTime(i === 1 ? profile.detune : i === 2 ? -profile.detune : 0, t, 1.2);
      o.type = profile.waveform;
      gains[i].gain.setTargetAtTime(levels[i], t, 1.2);
    });
    filter.frequency.setTargetAtTime(profile.filter, t, 1.4);
    lfo.frequency.setTargetAtTime(0.05 + profile.shimmer * 0.06, t, 1.4);
  }

  private scheduleSteps() {
    window.clearTimeout(this.stepTimer);
    const tick = () => {
      if (!this.enabled || !this.bgmNodes) return;
      if (performance.now() > this.duckUntil) this.bell();
      this.stepTimer = window.setTimeout(tick, this.profile.tempo * 1000 * (0.85 + Math.random() * 0.4));
    };
    this.stepTimer = window.setTimeout(tick, 900);
  }

  /** One melodic bell from the current scale. */
  private bell() {
    if (!this.ctx || !this.bgmBus) return;
    const ctx = this.ctx;
    const p = this.profile;
    const semi = p.scale[Math.floor(Math.random() * p.scale.length)] + (Math.random() < 0.25 ? 12 : 0);
    const freq = p.root * 4 * Math.pow(2, semi / 12);
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.value = freq;
    const o2 = ctx.createOscillator();
    o2.type = "sine";
    o2.frequency.value = freq * 2.01;
    const g = ctx.createGain();
    const peak = p.bellGain * 0.10;
    g.gain.setValueAtTime(0.0001, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(peak, ctx.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 2.8);
    const g2 = ctx.createGain();
    g2.gain.value = 0.3;
    o.connect(g);
    o2.connect(g2).connect(g);
    g.connect(this.bgmBus);
    o.start();
    o2.start();
    o.stop(ctx.currentTime + 3);
    o2.stop(ctx.currentTime + 3);
  }

  /** Temporarily lower the BGM (used by cutscenes). */
  duck(seconds: number, level = 0.12) {
    if (!this.ctx || !this.bgmBus) return;
    this.duckUntil = performance.now() + seconds * 1000;
    const t = this.ctx.currentTime;
    this.bgmBus.gain.cancelScheduledValues(t);
    this.bgmBus.gain.setTargetAtTime(this.volumes.bgm * level, t, 0.25);
    this.bgmBus.gain.setTargetAtTime(this.volumes.bgm, t + seconds, 0.8);
  }

  silence(seconds: number) {
    this.duck(seconds, 0.0001);
  }

  stopBgm() {
    window.clearTimeout(this.stepTimer);
    if (!this.ctx || !this.bgmNodes) return;
    const t = this.ctx.currentTime;
    this.bgmNodes.gains.forEach((g) => g.gain.setTargetAtTime(0, t, 0.3));
    const nodes = this.bgmNodes;
    this.bgmNodes = null;
    this.profileKey = "";
    window.setTimeout(() => {
      nodes.osc.forEach((o) => {
        try {
          o.stop();
        } catch {
          /* already stopped */
        }
      });
      try {
        nodes.lfo.stop();
      } catch {
        /* already stopped */
      }
    }, 1200);
  }

  // ----------------------------------------------------------------- SFX
  private tone(freq: number, dur: number, opts: { type?: OscillatorType; gain?: number; slideTo?: number; delay?: number; attack?: number; q?: number } = {}) {
    if (!this.ctx || !this.sfxBus) return;
    const ctx = this.ctx;
    const t0 = ctx.currentTime + (opts.delay ?? 0);
    const o = ctx.createOscillator();
    o.type = opts.type ?? "sine";
    o.frequency.setValueAtTime(freq, t0);
    if (opts.slideTo) o.frequency.exponentialRampToValueAtTime(Math.max(20, opts.slideTo), t0 + dur);
    const g = ctx.createGain();
    const peak = Math.max(0.0002, opts.gain ?? 0.2);
    const atk = opts.attack ?? 0.006;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + atk);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.connect(g).connect(this.sfxBus);
    o.start(t0);
    o.stop(t0 + dur + 0.05);
  }

  private noise(dur: number, opts: { gain?: number; filter?: number; type?: BiquadFilterType; delay?: number; sweepTo?: number } = {}) {
    if (!this.ctx || !this.sfxBus || !this.noiseBuffer) return;
    const ctx = this.ctx;
    const t0 = ctx.currentTime + (opts.delay ?? 0);
    const src = ctx.createBufferSource();
    src.buffer = this.noiseBuffer;
    src.loop = true;
    const f = ctx.createBiquadFilter();
    f.type = opts.type ?? "bandpass";
    f.frequency.setValueAtTime(opts.filter ?? 1200, t0);
    if (opts.sweepTo) f.frequency.exponentialRampToValueAtTime(Math.max(40, opts.sweepTo), t0 + dur);
    f.Q.value = 1.2;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, opts.gain ?? 0.12), t0 + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    src.connect(f).connect(g).connect(this.sfxBus);
    src.start(t0);
    src.stop(t0 + dur + 0.05);
  }

  private chord(freqs: number[], dur: number, gain = 0.14, type: OscillatorType = "sine", spread = 0) {
    freqs.forEach((f, i) => this.tone(f, dur, { type, gain, delay: i * spread }));
  }

  sfx(name: SfxName, intensity = 1) {
    if (!this.enabled || !this.ctx) return;
    const G = Math.min(1.4, Math.max(0, intensity));
    switch (name) {
      case "click":
        this.tone(660, 0.06, { type: "triangle", gain: 0.09 * G });
        break;
      case "hover":
        this.tone(880, 0.04, { type: "sine", gain: 0.035 * G });
        break;
      case "roll_start":
        this.tone(220, 0.22, { type: "triangle", gain: 0.12 * G, slideTo: 520 });
        this.noise(0.18, { gain: 0.05 * G, filter: 900, sweepTo: 2600 });
        break;
      case "roll_tick":
        this.tone(1400 + Math.random() * 200, 0.03, { type: "square", gain: 0.03 * G });
        break;
      case "reveal_common":
        this.tone(520, 0.14, { type: "sine", gain: 0.09 * G });
        break;
      case "reveal_rare":
        this.chord([523, 659], 0.5, 0.10 * G, "sine", 0.05);
        this.sfx("sparkle", 0.6);
        break;
      case "reveal_epic":
        this.chord([523, 659, 784], 0.8, 0.11 * G, "triangle", 0.06);
        this.noise(0.5, { gain: 0.05 * G, filter: 2600, sweepTo: 600 });
        break;
      case "reveal_legendary":
        this.chord([392, 523, 659, 784], 1.6, 0.12 * G, "sine", 0.09);
        this.tone(98, 1.8, { type: "sine", gain: 0.16 * G });
        this.sfx("sparkle", 1);
        break;
      case "reveal_secret":
        this.chord([330, 440, 554, 659, 880], 2.6, 0.11 * G, "sine", 0.12);
        this.tone(55, 3, { type: "sine", gain: 0.2 * G });
        this.noise(1.6, { gain: 0.06 * G, filter: 4000, sweepTo: 300 });
        break;
      case "reveal_ultra":
        this.chord([261, 329, 392, 493, 587, 784], 3.6, 0.10 * G, "sine", 0.14);
        this.tone(41, 4, { type: "sine", gain: 0.24 * G });
        this.noise(2.4, { gain: 0.07 * G, filter: 6000, sweepTo: 200 });
        break;
      case "reveal_mythic":
        this.chord([196, 261, 329, 392, 523, 659, 784, 1046], 5, 0.09 * G, "sine", 0.16);
        this.tone(32.7, 6, { type: "sine", gain: 0.26 * G });
        this.noise(3.4, { gain: 0.08 * G, filter: 8000, sweepTo: 120 });
        break;
      case "reveal_admin":
        this.chord([110, 138, 164, 220, 277, 330], 4, 0.10 * G, "sawtooth", 0.1);
        this.tone(27.5, 5, { type: "sine", gain: 0.3 * G });
        this.noise(2.6, { gain: 0.1 * G, filter: 120, type: "lowpass", sweepTo: 5000 });
        break;
      case "charge":
        this.tone(80, 1.8, { type: "sawtooth", gain: 0.10 * G, slideTo: 900, attack: 0.5 });
        this.noise(1.8, { gain: 0.05 * G, filter: 200, sweepTo: 4000 });
        break;
      case "impact":
        this.tone(140, 0.6, { type: "sine", gain: 0.3 * G, slideTo: 40 });
        this.noise(0.4, { gain: 0.18 * G, filter: 500, type: "lowpass", sweepTo: 80 });
        break;
      case "shatter":
        for (let i = 0; i < 10; i++) this.tone(1800 + Math.random() * 2400, 0.22, { type: "triangle", gain: 0.05 * G, delay: i * 0.03, slideTo: 400 });
        this.noise(0.6, { gain: 0.1 * G, filter: 5000, sweepTo: 800 });
        break;
      case "sparkle":
        for (let i = 0; i < 7; i++) this.tone(1600 + Math.random() * 2600, 0.16, { type: "sine", gain: 0.035 * G, delay: i * 0.055 });
        break;
      case "whoosh":
        this.noise(0.7, { gain: 0.11 * G, filter: 180, sweepTo: 3600, type: "bandpass" });
        break;
      case "heartbeat":
        this.tone(52, 0.24, { type: "sine", gain: 0.26 * G, slideTo: 32 });
        this.tone(48, 0.22, { type: "sine", gain: 0.18 * G, slideTo: 30, delay: 0.3 });
        break;
      case "error":
        this.tone(180, 0.18, { type: "square", gain: 0.09 * G, slideTo: 120 });
        break;
      case "success":
        this.chord([523, 784], 0.3, 0.09 * G, "triangle", 0.07);
        break;
      case "coin":
        this.tone(1180, 0.09, { type: "square", gain: 0.06 * G });
        this.tone(1560, 0.13, { type: "square", gain: 0.05 * G, delay: 0.06 });
        break;
      case "craft":
        this.tone(320, 0.14, { type: "square", gain: 0.1 * G });
        this.noise(0.3, { gain: 0.08 * G, filter: 1800, sweepTo: 400, delay: 0.05 });
        this.chord([659, 880], 0.5, 0.07 * G, "sine", 0.08);
        break;
      case "equip":
        this.tone(440, 0.1, { type: "triangle", gain: 0.09 * G });
        this.tone(660, 0.16, { type: "triangle", gain: 0.07 * G, delay: 0.07 });
        break;
      case "level_up":
        this.chord([392, 523, 659, 880], 1.1, 0.10 * G, "triangle", 0.1);
        this.sfx("sparkle", 1.1);
        break;
      case "quest":
        this.chord([523, 698], 0.5, 0.09 * G, "sine", 0.1);
        break;
      case "world_notice":
        this.chord([440, 587, 880], 1.2, 0.07 * G, "sine", 0.13);
        break;
      case "biome_shift":
        this.noise(1.4, { gain: 0.09 * G, filter: 260, sweepTo: 3400 });
        this.chord([196, 294, 392], 1.6, 0.08 * G, "sine", 0.14);
        break;
      case "biome_end":
        this.noise(0.9, { gain: 0.06 * G, filter: 2600, sweepTo: 200 });
        break;
      case "first_discovery":
        this.chord([261, 392, 523, 784, 1046], 3.4, 0.10 * G, "sine", 0.17);
        this.tone(65, 3.6, { type: "sine", gain: 0.2 * G });
        this.sfx("sparkle", 1.3);
        break;
      case "glitch":
        for (let i = 0; i < 9; i++) this.noise(0.05, { gain: 0.1 * G, filter: 400 + Math.random() * 5000, delay: i * 0.045 });
        break;
      case "void":
        this.tone(28, 3.2, { type: "sine", gain: 0.28 * G });
        this.noise(2.6, { gain: 0.07 * G, filter: 90, type: "lowpass" });
        break;
      case "time":
        for (let i = 0; i < 8; i++) this.tone(1320, 0.05, { type: "square", gain: 0.04 * G, delay: i * 0.22 });
        break;
      default:
        break;
    }
  }
}

export const audio = new AudioEngine();

export function bgmForBiome(theme: { bgm?: string } | undefined | null): string {
  return theme?.bgm && BGM_PROFILES[theme.bgm] ? theme.bgm : "drift";
}
