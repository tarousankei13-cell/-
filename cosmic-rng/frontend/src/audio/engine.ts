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
  root: number;          // tonic (Hz)
  scale: number[];       // semitone offsets of the mode, 7 or 5 degrees
  bpm: number;
  /** Chord cycle as scale-degree indices (0-based). Four chords, two bars each. */
  progression: number[][];
  arp: "up" | "down" | "updown" | "walk" | "none";
  arpRate: 1 | 2 | 4;    // notes per beat
  pulse: number;         // 0..1 rhythmic density (kick / hat)
  padGain: number;
  bassGain: number;
  leadGain: number;
  filter: number;        // pad low-pass cutoff (Hz)
  detune: number;        // cents between pad voices
  waveform: OscillatorType;
  shimmer: number;       // filter LFO depth 0..1
  reverb: number;        // send level 0..1
}

// Each biome is a short piece: a mode, a four-chord cycle, a way of moving
// through it and a rhythm. Drift is the calm home key; the deep-space biomes
// (void, singularity, sanctum) sit low and slow with heavy reverb; the bright
// ones (bloom, starfall, genesis) move faster with an arpeggio on top.
const MAJ = [0, 2, 4, 5, 7, 9, 11];
const MIN = [0, 2, 3, 5, 7, 8, 10];
const DOR = [0, 2, 3, 5, 7, 9, 10];
const LYD = [0, 2, 4, 6, 7, 9, 11];
const PHR = [0, 1, 3, 5, 7, 8, 10];
const LOC = [0, 1, 3, 5, 6, 8, 10];
const WHOLE = [0, 2, 4, 6, 8, 10];
const T = (deg: number) => [deg, deg + 2, deg + 4];           // triad on a degree
const T7 = (deg: number) => [deg, deg + 2, deg + 4, deg + 6];  // seventh

const BGM_PROFILES: Record<string, BgmProfile> = {
  drift:       { root: 110.0, scale: MIN, bpm: 62, progression: [T(0), T(5), T(2), T(6)],      arp: "walk",   arpRate: 1, pulse: 0.15, padGain: 0.42, bassGain: 0.30, leadGain: 0.16, filter: 1100, detune: 6,  waveform: "sine",     shimmer: 0.4, reverb: 0.55 },
  bloom:       { root: 130.8, scale: MAJ, bpm: 84, progression: [T(0), T(3), T(5), T(4)],      arp: "updown", arpRate: 2, pulse: 0.40, padGain: 0.40, bassGain: 0.28, leadGain: 0.18, filter: 1600, detune: 9,  waveform: "triangle", shimmer: 0.7, reverb: 0.5 },
  solar:       { root: 98.0,  scale: DOR, bpm: 96, progression: [T(0), T(3), T(0), T(4)],      arp: "up",     arpRate: 2, pulse: 0.65, padGain: 0.36, bassGain: 0.40, leadGain: 0.17, filter: 2000, detune: 14, waveform: "sawtooth", shimmer: 0.5, reverb: 0.35 },
  storm:       { root: 87.3,  scale: PHR, bpm: 112, progression: [T(0), T(1), T(0), T(5)],     arp: "down",   arpRate: 4, pulse: 0.85, padGain: 0.34, bassGain: 0.44, leadGain: 0.15, filter: 1800, detune: 16, waveform: "sawtooth", shimmer: 0.3, reverb: 0.3 },
  aurora:      { root: 146.8, scale: LYD, bpm: 70, progression: [T7(0), T7(3), T7(1), T7(4)],  arp: "updown", arpRate: 2, pulse: 0.20, padGain: 0.44, bassGain: 0.24, leadGain: 0.18, filter: 2400, detune: 8,  waveform: "sine",     shimmer: 0.9, reverb: 0.7 },
  frost:       { root: 155.6, scale: MIN, bpm: 58, progression: [T(0), T(2), T(5), T(3)],      arp: "walk",   arpRate: 1, pulse: 0.10, padGain: 0.40, bassGain: 0.22, leadGain: 0.20, filter: 2800, detune: 5,  waveform: "sine",     shimmer: 1.0, reverb: 0.8 },
  eclipse:     { root: 82.4,  scale: PHR, bpm: 66, progression: [T(0), T(1), T(3), T(0)],      arp: "none",   arpRate: 1, pulse: 0.30, padGain: 0.44, bassGain: 0.46, leadGain: 0.14, filter: 800,  detune: 18, waveform: "sawtooth", shimmer: 0.2, reverb: 0.6 },
  quantum:     { root: 164.8, scale: WHOLE, bpm: 128, progression: [[0, 2, 4], [1, 3, 5], [2, 4, 0], [3, 5, 1]], arp: "up", arpRate: 4, pulse: 0.7, padGain: 0.30, bassGain: 0.30, leadGain: 0.16, filter: 3000, detune: 22, waveform: "square", shimmer: 0.8, reverb: 0.3 },
  starfall:    { root: 174.6, scale: MAJ, bpm: 104, progression: [T(0), T(4), T(5), T(3)],     arp: "updown", arpRate: 4, pulse: 0.55, padGain: 0.40, bassGain: 0.30, leadGain: 0.20, filter: 2600, detune: 7,  waveform: "triangle", shimmer: 1.0, reverb: 0.45 },
  void:        { root: 61.7,  scale: LOC, bpm: 48, progression: [T(0), T(0), T(4), T(1)],      arp: "none",   arpRate: 1, pulse: 0.12, padGain: 0.50, bassGain: 0.52, leadGain: 0.10, filter: 520,  detune: 24, waveform: "sawtooth", shimmer: 0.2, reverb: 0.9 },
  singularity: { root: 55.0,  scale: LOC, bpm: 44, progression: [T(0), T(3), T(0), T(6)],      arp: "none",   arpRate: 1, pulse: 0.25, padGain: 0.54, bassGain: 0.56, leadGain: 0.10, filter: 420,  detune: 28, waveform: "sawtooth", shimmer: 0.3, reverb: 0.9 },
  genesis:     { root: 196.0, scale: MAJ, bpm: 76, progression: [T7(0), T7(3), T7(5), T7(4)],  arp: "updown", arpRate: 2, pulse: 0.30, padGain: 0.46, bassGain: 0.26, leadGain: 0.22, filter: 3400, detune: 4,  waveform: "sine",     shimmer: 1.0, reverb: 0.75 },
  architect:   { root: 138.6, scale: LYD, bpm: 90, progression: [T(0), T(1), T(4), T(0)],      arp: "up",     arpRate: 2, pulse: 0.5,  padGain: 0.40, bassGain: 0.32, leadGain: 0.20, filter: 2800, detune: 3,  waveform: "triangle", shimmer: 0.8, reverb: 0.5 },
  sanctum:     { root: 58.3,  scale: PHR, bpm: 46, progression: [T(0), T(5), T(1), T(0)],      arp: "none",   arpRate: 1, pulse: 0.2,  padGain: 0.54, bassGain: 0.52, leadGain: 0.10, filter: 460,  detune: 26, waveform: "sawtooth", shimmer: 0.4, reverb: 0.9 },
  fracture:    { root: 123.5, scale: LOC, bpm: 140, progression: [T(0), T(1), T(0), T(2)],     arp: "down",   arpRate: 4, pulse: 0.95, padGain: 0.34, bassGain: 0.38, leadGain: 0.15, filter: 2200, detune: 30, waveform: "square",   shimmer: 0.6, reverb: 0.25 },
  source:      { root: 65.4,  scale: DOR, bpm: 100, progression: [T(0), T(3), T(4), T(0)],     arp: "up",     arpRate: 4, pulse: 0.6,  padGain: 0.38, bassGain: 0.42, leadGain: 0.18, filter: 2000, detune: 12, waveform: "square",   shimmer: 0.7, reverb: 0.4 },
  chrono:      { root: 116.5, scale: DOR, bpm: 72, progression: [T(0), T(6), T(3), T(4)],      arp: "walk",   arpRate: 2, pulse: 0.35, padGain: 0.40, bassGain: 0.30, leadGain: 0.18, filter: 2100, detune: 6,  waveform: "triangle", shimmer: 0.6, reverb: 0.55 },
};

/**
 * How far to move the sound effects so they agree with the music.
 *
 * Every effect below is written in C. A profile in, say, E♭ would make each
 * reveal chord clash with the pad underneath it, which is worse than silence
 * at the moment a player is staring at a 1-in-a-million drop. The shift is
 * taken to the nearest key within a tritone, so nothing ever jumps an octave.
 */
const C0 = 16.3516;
function keyRatioFor(root: number): number {
  let semis = Math.round(12 * Math.log2(root / C0)) % 12;
  if (semis < 0) semis += 12;
  if (semis > 6) semis -= 12;
  return Math.pow(2, semis / 12);
}

const degreeToFreq = (root: number, scale: number[], degree: number, octave = 0): number => {
  const n = scale.length;
  const oct = Math.floor(degree / n) + octave;
  const semi = scale[((degree % n) + n) % n];
  return root * Math.pow(2, oct + semi / 12);
};

interface Voices {
  pads: { osc: OscillatorNode; gain: GainNode }[];
  filter: BiquadFilterNode;
  lfo: OscillatorNode;
  lfoGain: GainNode;
  layers: { arp: GainNode; bass: GainNode; lead: GainNode; pulse: GainNode };
}

class AudioEngine {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private limiter: DynamicsCompressorNode | null = null;
  private bgmBus: GainNode | null = null;
  private sfxBus: GainNode | null = null;
  private reverb: ConvolverNode | null = null;
  private reverbSend: GainNode | null = null;
  private voices: Voices | null = null;
  private profileKey = "";
  private profile: BgmProfile = BGM_PROFILES.drift;
  /** Pitch ratio applied to every musical sound effect, so they land in the
   *  same key as whatever is playing underneath. */
  private keyRatio = 1;
  private noiseBuffer: AudioBuffer | null = null;
  private enabled = false;
  private duckUntil = 0;

  // sequencer state (all in AudioContext seconds)
  private schedTimer: number | undefined;
  private nextBeat = 0;
  private beat = 0;
  private chordIdx = 0;
  private motif: number[] = [];
  private motifPos = 0;
  private arpDir = 1;
  private arpPos = 0;
  private intensity = 0.45;
  private intensityTarget = 0.45;

  // A little louder than before across the board; the limiter on the master
  // keeps the raised gain staging from clipping when a reveal chord lands on
  // top of the music.
  volumes = { master: 0.9, bgm: 0.7, sfx: 0.9, muted: false };

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
      const ctx = this.ctx;
      this.limiter = ctx.createDynamicsCompressor();
      this.limiter.threshold.value = -14;
      this.limiter.knee.value = 18;
      this.limiter.ratio.value = 5;
      this.limiter.attack.value = 0.004;
      this.limiter.release.value = 0.18;
      this.limiter.connect(ctx.destination);
      this.master = ctx.createGain();
      this.master.gain.value = this.volumes.muted ? 0 : this.volumes.master;
      this.master.connect(this.limiter);
      this.bgmBus = ctx.createGain();
      this.bgmBus.gain.value = this.volumes.bgm;
      this.bgmBus.connect(this.master);
      this.sfxBus = ctx.createGain();
      this.sfxBus.gain.value = this.volumes.sfx;
      this.sfxBus.connect(this.master);
      this.makeNoise();
      this.makeReverb();
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

  /** What the music is doing right now — shown in Settings, handy in tests. */
  get nowPlaying(): { profile: string; bpm: number; beat: number; bar: number; chord: number; intensity: number; running: boolean } | null {
    if (!this.voices || !this.ctx) return null;
    return {
      profile: this.profileKey, bpm: this.profile.bpm, beat: this.beat, bar: Math.floor(this.beat / 4),
      chord: this.chordIdx, intensity: Math.round(this.intensity * 100) / 100, running: this.ctx.state === "running",
    };
  }

  /** How much is going on: 0 idle, 1 full. Layers fade in and out with it. */
  setIntensity(level: number) {
    this.intensityTarget = Math.min(1, Math.max(0, level));
  }

  /** A short surge that settles back — a roll, a purchase, a reveal ending. */
  nudge(amount = 0.25) {
    this.intensity = Math.min(1, this.intensity + amount);
  }

  private makeNoise() {
    if (!this.ctx) return;
    const len = this.ctx.sampleRate * 2;
    const buf = this.ctx.createBuffer(1, len, this.ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    this.noiseBuffer = buf;
  }

  /** A synthesised hall: exponentially decaying stereo noise as the impulse.
   *  This one node is most of the difference between "oscillators" and "music". */
  private makeReverb() {
    if (!this.ctx || !this.bgmBus) return;
    const ctx = this.ctx;
    const seconds = 3.2;
    const len = Math.floor(ctx.sampleRate * seconds);
    const ir = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let ch = 0; ch < 2; ch++) {
      const d = ir.getChannelData(ch);
      for (let i = 0; i < len; i++) {
        const t = i / len;
        // early reflections a little denser, then a smooth tail
        const env = Math.pow(1 - t, 2.6) * (i < ctx.sampleRate * 0.08 ? 0.6 : 1);
        d[i] = (Math.random() * 2 - 1) * env;
      }
    }
    this.reverb = ctx.createConvolver();
    this.reverb.buffer = ir;
    this.reverbSend = ctx.createGain();
    this.reverbSend.gain.value = 0.5;
    this.reverbSend.connect(this.reverb).connect(this.bgmBus);
  }

  // ----------------------------------------------------------------- BGM
  startBgm(key: string) {
    if (!this.enabled || !this.ctx || !this.bgmBus) return;
    const profile = BGM_PROFILES[key] ?? BGM_PROFILES.drift;
    if (this.voices && this.profileKey === key) return;
    this.profileKey = key;
    this.profile = profile;
    this.keyRatio = keyRatioFor(profile.root);
    if (this.reverbSend) this.reverbSend.gain.setTargetAtTime(profile.reverb, this.ctx.currentTime, 1.5);
    if (this.voices) {
      // Changing biome mid-piece: glide the pads to the new key on the next
      // chord and let the sequencer pick up the new tempo and cycle.
      this.chordIdx = 0;
      this.motif = [];
      const t = this.ctx.currentTime;
      this.voices.filter.frequency.setTargetAtTime(profile.filter, t, 1.4);
      this.voices.lfo.frequency.setTargetAtTime(0.05 + profile.shimmer * 0.06, t, 1.4);
      this.voices.lfoGain.gain.setTargetAtTime(profile.filter * 0.3 * profile.shimmer, t, 1.4);
      this.voices.pads.forEach((v) => { v.osc.type = profile.waveform; });
      this.glidePads(this.profile.progression[0], 1.6);
      return;
    }
    const ctx = this.ctx;
    const filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.value = profile.filter;
    filter.Q.value = 0.7;
    filter.connect(this.bgmBus);
    if (this.reverbSend) filter.connect(this.reverbSend);

    // Three pad voices (root, third, fifth) plus a sub. Each voice is two
    // oscillators detuned against each other, which is what gives a pad width.
    const pads: Voices["pads"] = [];
    for (let i = 0; i < 4; i++) {
      const o = ctx.createOscillator();
      o.type = profile.waveform;
      o.detune.value = i === 1 ? profile.detune : i === 2 ? -profile.detune : 0;
      const g = ctx.createGain();
      g.gain.value = 0;
      o.connect(g).connect(filter);
      o.start();
      pads.push({ osc: o, gain: g });
    }
    const lfo = ctx.createOscillator();
    lfo.frequency.value = 0.05 + profile.shimmer * 0.06;
    const lfoGain = ctx.createGain();
    lfoGain.gain.value = profile.filter * 0.3 * profile.shimmer;
    lfo.connect(lfoGain).connect(filter.frequency);
    lfo.start();

    const layer = () => { const g = ctx.createGain(); g.gain.value = 0; g.connect(this.bgmBus!); if (this.reverbSend) g.connect(this.reverbSend); return g; };
    this.voices = { pads, filter, lfo, lfoGain, layers: { arp: layer(), bass: layer(), lead: layer(), pulse: layer() } };
    this.glidePads(profile.progression[0], 2.4);
    this.nextBeat = ctx.currentTime + 0.4;
    this.beat = 0;
    this.chordIdx = 0;
    this.motif = [];
    this.startScheduler();
  }

  private beatSeconds() {
    return 60 / this.profile.bpm;
  }

  private chordTones(chord: number[], octave = 0): number[] {
    const p = this.profile;
    return chord.map((deg) => degreeToFreq(p.root, p.scale, deg, octave));
  }

  /** Move the pad voices to a chord. */
  private glidePads(chord: number[], seconds: number) {
    if (!this.ctx || !this.voices) return;
    const t = this.ctx.currentTime;
    const p = this.profile;
    const tones = this.chordTones(chord, 0);
    const targets = [tones[0], tones[1 % tones.length], tones[2 % tones.length], tones[0] / 2];
    const levels = [p.padGain * 0.42, p.padGain * 0.30, p.padGain * 0.24, p.bassGain * 0.34];
    this.voices.pads.forEach((v, i) => {
      v.osc.frequency.setTargetAtTime(targets[i], t, seconds * 0.45);
      v.gain.gain.setTargetAtTime(levels[i], t, seconds * 0.6);
    });
  }

  /** Look-ahead scheduler: everything is placed on the AudioContext clock,
   *  so a busy main thread cannot make the music stutter. */
  private startScheduler() {
    window.clearInterval(this.schedTimer);
    const LOOKAHEAD = 0.28;
    const tick = () => {
      if (!this.enabled || !this.ctx || !this.voices) return;
      // intensity eases towards its target and decays after a nudge
      this.intensity += (this.intensityTarget - this.intensity) * 0.06;
      this.applyIntensity();
      while (this.nextBeat < this.ctx.currentTime + LOOKAHEAD) {
        this.scheduleBeat(this.nextBeat, this.beat);
        this.nextBeat += this.beatSeconds();
        this.beat += 1;
      }
    };
    this.schedTimer = window.setInterval(tick, 90);
  }

  private applyIntensity() {
    if (!this.ctx || !this.voices) return;
    const p = this.profile;
    const x = this.intensity;
    const t = this.ctx.currentTime;
    const L = this.voices.layers;
    L.arp.gain.setTargetAtTime(p.arp === "none" ? 0 : 0.35 + 0.65 * x, t, 0.4);
    L.bass.gain.setTargetAtTime(0.6 + 0.4 * x, t, 0.4);
    L.lead.gain.setTargetAtTime(0.5 + 0.5 * x, t, 0.4);
    L.pulse.gain.setTargetAtTime(p.pulse * (0.3 + 0.7 * x), t, 0.4);
    this.voices.filter.frequency.setTargetAtTime(p.filter * (0.75 + 0.5 * x), t, 0.6);
  }

  private scheduleBeat(t: number, beat: number) {
    if (!this.ctx || !this.voices) return;
    const p = this.profile;
    const ducked = performance.now() < this.duckUntil;
    const inBar = beat % 4;
    const bar = Math.floor(beat / 4);

    // chord changes every two bars
    if (beat % 8 === 0) {
      this.chordIdx = (bar / 2) % p.progression.length;
      this.glidePads(p.progression[this.chordIdx], this.beatSeconds() * 1.5);
    }
    const chord = p.progression[this.chordIdx];
    const tones = this.chordTones(chord, 0);

    // bass: root on 1, fifth (or root) on 3
    if (inBar === 0 || inBar === 2) {
      const f = inBar === 0 ? tones[0] / 2 : (tones[2 % tones.length] ?? tones[0]) / 2;
      this.pluck(f, this.beatSeconds() * 1.6, p.bassGain * 0.5, "triangle", this.voices.layers.bass, t, 0.02);
    }

    // pulse: kick on 1 (and 3 when driven), hat on the off-beats
    if (!ducked && p.pulse > 0.05) {
      if (inBar === 0 || (inBar === 2 && p.pulse > 0.6)) this.kick(t, 0.5 * p.pulse);
      if (p.pulse > 0.3) {
        for (let k = 0; k < 2; k++) {
          const at = t + (k + 0.5) * (this.beatSeconds() / 2);
          if (k === 1 || p.pulse > 0.75) this.hat(at, 0.09 * p.pulse);
        }
      }
    }

    // arpeggio on chord tones across two octaves
    if (!ducked && p.arp !== "none") {
      const pool = [...tones, ...tones.map((f) => f * 2)];
      const step = this.beatSeconds() / p.arpRate;
      for (let k = 0; k < p.arpRate; k++) {
        let idx: number;
        if (p.arp === "up") idx = this.arpPos++ % pool.length;
        else if (p.arp === "down") idx = pool.length - 1 - (this.arpPos++ % pool.length);
        else if (p.arp === "updown") {
          idx = this.arpPos;
          this.arpPos += this.arpDir;
          if (this.arpPos >= pool.length - 1 || this.arpPos <= 0) this.arpDir *= -1;
          this.arpPos = Math.max(0, Math.min(pool.length - 1, this.arpPos));
        } else {
          // walk: mostly steps, sometimes a leap, stays on chord tones
          this.arpPos = Math.max(0, Math.min(pool.length - 1, this.arpPos + (Math.random() < 0.7 ? (Math.random() < 0.5 ? -1 : 1) : (Math.random() < 0.5 ? -2 : 2))));
          idx = this.arpPos;
          if (Math.random() < 0.35) continue; // rests keep a walk from sounding mechanical
        }
        const accent = k === 0 ? 1 : 0.7;
        this.pluck(pool[idx], step * 1.8, p.leadGain * 0.45 * accent, p.waveform === "sine" ? "triangle" : "sine", this.voices.layers.arp, t + k * step, 0.004);
      }
    }

    // melody: a motif of scale steps, regenerated every four bars, placed on
    // beats 1 and 3 (plus 2 when busy), transposed to sit on the current chord
    if (!ducked) {
      if (beat % 16 === 0 || this.motif.length === 0) this.newMotif(chord);
      const play = inBar === 0 || inBar === 2 || (inBar === 1 && this.intensity > 0.7 && Math.random() < 0.5);
      if (play && Math.random() < 0.85) {
        const deg = this.motif[this.motifPos++ % this.motif.length];
        // land the motif on the chord: snap non-chord tones towards the nearest chord tone half the time
        const snapped = chord.includes(((deg % p.scale.length) + p.scale.length) % p.scale.length) || Math.random() < 0.5
          ? deg : chord[Math.floor(Math.random() * chord.length)] + Math.floor(deg / p.scale.length) * p.scale.length;
        const f = degreeToFreq(p.root, p.scale, snapped, 2);
        this.bell(f, this.beatSeconds() * 2.6, p.leadGain, this.voices.layers.lead, t + (Math.random() < 0.2 ? this.beatSeconds() * 0.5 : 0));
      }
    }
  }

  private newMotif(chord: number[]) {
    const n = this.profile.scale.length;
    const len = 4 + Math.floor(Math.random() * 4);
    let deg = chord[0] + n; // start an octave up on the chord root
    const out: number[] = [];
    for (let i = 0; i < len; i++) {
      out.push(deg);
      const r = Math.random();
      deg += r < 0.45 ? 1 : r < 0.8 ? -1 : r < 0.9 ? 2 : -2;
      deg = Math.max(chord[0] + n - 3, Math.min(chord[0] + n + 6, deg));
    }
    this.motif = out;
    this.motifPos = 0;
  }

  // -------------------------------------------------------------- BGM voices
  private pluck(freq: number, dur: number, gain: number, type: OscillatorType, dest: GainNode, t: number, attack: number) {
    if (!this.ctx) return;
    const ctx = this.ctx;
    const o = ctx.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(dest);
    o.start(t);
    o.stop(t + dur + 0.05);
  }

  /** Bell: a sine with a detuned octave partial, long decay. */
  private bell(freq: number, dur: number, gain: number, dest: GainNode, t: number) {
    if (!this.ctx) return;
    const ctx = this.ctx;
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.setValueAtTime(freq, t);
    const o2 = ctx.createOscillator();
    o2.type = "sine";
    o2.frequency.setValueAtTime(freq * 2.005, t);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    const g2 = ctx.createGain();
    g2.gain.value = 0.28;
    o.connect(g);
    o2.connect(g2).connect(g);
    g.connect(dest);
    o.start(t);
    o2.start(t);
    o.stop(t + dur + 0.05);
    o2.stop(t + dur + 0.05);
  }

  private kick(t: number, gain: number) {
    if (!this.ctx || !this.voices) return;
    const ctx = this.ctx;
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.setValueAtTime(120, t);
    o.frequency.exponentialRampToValueAtTime(38, t + 0.16);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.32);
    o.connect(g).connect(this.voices.layers.pulse);
    o.start(t);
    o.stop(t + 0.36);
  }

  private hat(t: number, gain: number) {
    if (!this.ctx || !this.voices || !this.noiseBuffer) return;
    const ctx = this.ctx;
    const src = ctx.createBufferSource();
    src.buffer = this.noiseBuffer;
    const f = ctx.createBiquadFilter();
    f.type = "highpass";
    f.frequency.value = 6000;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t + 0.003);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.07);
    src.connect(f).connect(g).connect(this.voices.layers.pulse);
    src.start(t);
    src.stop(t + 0.1);
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
    window.clearInterval(this.schedTimer);
    if (!this.ctx || !this.voices) return;
    const t = this.ctx.currentTime;
    const v = this.voices;
    v.pads.forEach((p) => p.gain.gain.setTargetAtTime(0, t, 0.3));
    Object.values(v.layers).forEach((g) => g.gain.setTargetAtTime(0, t, 0.3));
    this.voices = null;
    this.profileKey = "";
    window.setTimeout(() => {
      v.pads.forEach((p) => { try { p.osc.stop(); } catch { /* already stopped */ } });
      try { v.lfo.stop(); } catch { /* already stopped */ }
    }, 1200);
  }

  // ----------------------------------------------------------------- SFX
  private tone(freq: number, dur: number, opts: { type?: OscillatorType; gain?: number; slideTo?: number; delay?: number; attack?: number; q?: number; atonal?: boolean } = {}) {
    if (!this.ctx || !this.sfxBus) return;
    const ctx = this.ctx;
    const t0 = ctx.currentTime + (opts.delay ?? 0);
    const o = ctx.createOscillator();
    o.type = opts.type ?? "sine";
    // Effects are written in C and transposed to the current key here. Noise
    // and pure sweeps opt out: there is no pitch in them to move.
    const r = opts.atonal ? 1 : this.keyRatio;
    o.frequency.setValueAtTime(freq * r, t0);
    if (opts.slideTo) o.frequency.exponentialRampToValueAtTime(Math.max(20, opts.slideTo * r), t0 + dur);
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
        this.tone(1400 + Math.random() * 200, 0.03, { type: "square", gain: 0.03 * G, atonal: true });
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
        this.tone(80, 1.8, { type: "sawtooth", gain: 0.10 * G, slideTo: 900, attack: 0.5, atonal: true });
        this.noise(1.8, { gain: 0.05 * G, filter: 200, sweepTo: 4000 });
        break;
      case "impact":
        this.tone(140, 0.6, { type: "sine", gain: 0.3 * G, slideTo: 40, atonal: true });
        this.noise(0.4, { gain: 0.18 * G, filter: 500, type: "lowpass", sweepTo: 80 });
        break;
      case "shatter":
        for (let i = 0; i < 10; i++) this.tone(1800 + Math.random() * 2400, 0.22, { type: "triangle", gain: 0.05 * G, delay: i * 0.03, slideTo: 400, atonal: true });
        this.noise(0.6, { gain: 0.1 * G, filter: 5000, sweepTo: 800 });
        break;
      case "sparkle":
        for (let i = 0; i < 7; i++) this.tone(1600 + Math.random() * 2600, 0.16, { type: "sine", gain: 0.035 * G, delay: i * 0.055, atonal: true });
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
