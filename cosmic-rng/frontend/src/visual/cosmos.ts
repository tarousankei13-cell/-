/**
 * Cosmic background renderer (Canvas 2D).
 *
 * One persistent full-screen canvas renders the whole universe: a deep-space
 * gradient, drifting nebulae, a parallax star field and a per-biome particle
 * system. Biome changes cross-fade the palette rather than cutting, so the
 * world feels continuous. Quality settings scale particle counts and effects
 * so low-end phones stay smooth.
 */
import { hexToRgb } from "../lib/format";
import type { BiomeTheme } from "../lib/types";

export type Quality = "high" | "medium" | "low" | "minimal";

interface Star {
  x: number;
  y: number;
  z: number;
  r: number;
  tw: number;
  phase: number;
}

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  maxLife: number;
  size: number;
  rot: number;
  vr: number;
  hueShift: number;
}

interface Nebula {
  x: number;
  y: number;
  r: number;
  color: [number, number, number];
  drift: number;
  phase: number;
}

const DEFAULT_THEME: Required<Pick<BiomeTheme, "bg" | "nebula" | "accent" | "accent2" | "particles" | "particle_color" | "intensity" | "fx" | "vignette">> & { ceiling: number } = {
  bg: ["#03040c", "#070b24", "#140f3a"],
  nebula: ["#1d2a6b", "#4b2d8a", "#0f4a7a"],
  accent: "#8ab4ff",
  accent2: "#b58cff",
  particles: "stars",
  particle_color: "#ffffff",
  intensity: 0.35,
  fx: "none",
  vignette: 0.4,
  ceiling: 0,
};

type Theme = typeof DEFAULT_THEME;

/** Relative luminance of a hex colour (WCAG), 0..1. */
function luminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex).map((c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/**
 * How much to darken a theme so the interface stays readable on top of it.
 *
 * Every colour in the UI — text, panels, chips — is chosen for a dark stage.
 * A biome is free to paint the sky as bright as it likes (Genesis is nearly
 * white), so the sky gets its colour and the interface gets its stage: the
 * frame is pulled down until the brightest tone of the palette sits at about
 * the luminance of the darkest panel. Dark themes are left exactly as they are.
 */
const STAGE_LUMINANCE = 0.06;
function ceilingFor(bg: string[], nebula: string[], intensity: number): number {
  const brightest = Math.max(...bg.map(luminance), ...nebula.map((c) => luminance(c) * (0.25 + 0.45 * intensity)));
  if (brightest <= STAGE_LUMINANCE) return 0;
  // fraction of black to lay over the frame so brightest * (1 - k) ≈ target
  return Math.min(0.86, 1 - STAGE_LUMINANCE / brightest);
}

function normalize(t: BiomeTheme | null | undefined): Theme {
  const bg = (t?.bg && t.bg.length >= 3 ? t.bg : DEFAULT_THEME.bg).slice(0, 3);
  const nebula = (t?.nebula && t.nebula.length >= 3 ? t.nebula : DEFAULT_THEME.nebula).slice(0, 3);
  const intensity = t?.intensity ?? DEFAULT_THEME.intensity;
  return {
    bg,
    nebula,
    accent: t?.accent ?? DEFAULT_THEME.accent,
    accent2: t?.accent2 ?? DEFAULT_THEME.accent2,
    particles: t?.particles ?? DEFAULT_THEME.particles,
    particle_color: t?.particle_color ?? DEFAULT_THEME.particle_color,
    intensity,
    fx: t?.fx ?? DEFAULT_THEME.fx,
    vignette: t?.vignette ?? DEFAULT_THEME.vignette,
    ceiling: ceilingFor(bg, nebula, intensity),
  };
}

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
function lerpColor(a: string, b: string, t: number): [number, number, number] {
  const [r1, g1, b1] = hexToRgb(a);
  const [r2, g2, b2] = hexToRgb(b);
  return [lerp(r1, r2, t), lerp(g1, g2, t), lerp(b1, b2, t)];
}
const css = (c: [number, number, number], a = 1) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

const PARTICLE_BUDGET: Record<Quality, number> = { high: 240, medium: 130, low: 60, minimal: 0 };
const STAR_BUDGET: Record<Quality, number> = { high: 420, medium: 240, low: 120, minimal: 60 };

export class CosmosRenderer {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private raf = 0;
  private w = 0;
  private h = 0;
  private dpr = 1;
  private stars: Star[] = [];
  private particles: Particle[] = [];
  private nebulae: Nebula[] = [];
  private from: Theme = DEFAULT_THEME;
  private to: Theme = DEFAULT_THEME;
  private mix = 1;
  private t = 0;
  private last = 0;
  private quality: Quality = "high";
  private particleScale = 1;
  private reduced = false;
  private shake = 0;
  private flash = 0;
  private flashColor = "#ffffff";
  private warp = 0;
  private pointer = { x: 0.5, y: 0.5 };
  private running = false;
  private lowFrames = 0;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) throw new Error("canvas 2d unavailable");
    this.ctx = ctx;
    this.resize();
  }

  setQuality(q: Quality, particleScale = 1, reduced = false) {
    const changed = q !== this.quality;
    this.quality = q;
    this.particleScale = particleScale;
    this.reduced = reduced;
    if (changed) {
      this.buildStars();
      this.particles.length = 0;
    }
  }

  setTheme(theme: BiomeTheme | null | undefined, instant = false) {
    const next = normalize(theme);
    if (JSON.stringify(next) === JSON.stringify(this.to) && this.mix >= 1) return;
    this.from = this.current();
    this.to = next;
    this.mix = instant || this.reduced ? 1 : 0;
    this.buildNebulae();
  }

  /** Snapshot of the currently displayed (interpolated) theme. */
  private current(): Theme {
    if (this.mix >= 1) return this.to;
    const m = this.mix;
    const blend = (a: string, b: string) => css(lerpColor(a, b, m));
    return {
      bg: [blend(this.from.bg[0], this.to.bg[0]), blend(this.from.bg[1], this.to.bg[1]), blend(this.from.bg[2], this.to.bg[2])],
      nebula: [blend(this.from.nebula[0], this.to.nebula[0]), blend(this.from.nebula[1], this.to.nebula[1]), blend(this.from.nebula[2], this.to.nebula[2])],
      accent: blend(this.from.accent, this.to.accent),
      accent2: blend(this.from.accent2, this.to.accent2),
      particles: m > 0.5 ? this.to.particles : this.from.particles,
      particle_color: blend(this.from.particle_color, this.to.particle_color),
      intensity: lerp(this.from.intensity, this.to.intensity, m),
      fx: m > 0.5 ? this.to.fx : this.from.fx,
      vignette: lerp(this.from.vignette, this.to.vignette, m),
      ceiling: lerp(this.from.ceiling, this.to.ceiling, m),
    };
  }

  pulse(color = "#ffffff", strength = 1) {
    this.flash = Math.min(1, strength);
    this.flashColor = color;
  }

  shakeScreen(strength = 8) {
    if (this.reduced) return;
    this.shake = Math.max(this.shake, strength);
  }

  setWarp(v: number) {
    this.warp = v;
  }

  setPointer(x: number, y: number) {
    this.pointer.x = x;
    this.pointer.y = y;
  }

  resize = () => {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, this.quality === "high" ? 2 : 1.5);
    this.w = Math.max(1, Math.floor(rect.width || window.innerWidth));
    this.h = Math.max(1, Math.floor(rect.height || window.innerHeight));
    this.canvas.width = Math.floor(this.w * this.dpr);
    this.canvas.height = Math.floor(this.h * this.dpr);
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this.buildStars();
    this.buildNebulae();
  };

  private buildStars() {
    const n = Math.floor(STAR_BUDGET[this.quality] * Math.min(1.4, Math.max(0.2, (this.w * this.h) / (1440 * 900))));
    this.stars = Array.from({ length: n }, () => ({
      x: Math.random() * this.w,
      y: Math.random() * this.h,
      z: 0.2 + Math.random() * 0.8,
      r: 0.4 + Math.random() * 1.5,
      tw: 0.3 + Math.random() * 1.6,
      phase: Math.random() * Math.PI * 2,
    }));
  }

  private buildNebulae() {
    const count = this.quality === "high" ? 5 : this.quality === "medium" ? 3 : 2;
    const th = this.to;
    this.nebulae = Array.from({ length: count }, (_, i) => ({
      x: Math.random() * this.w,
      y: Math.random() * this.h,
      r: (0.25 + Math.random() * 0.45) * Math.max(this.w, this.h),
      color: hexToRgb(th.nebula[i % th.nebula.length]),
      drift: 0.004 + Math.random() * 0.012,
      phase: Math.random() * Math.PI * 2,
    }));
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    const loop = (now: number) => {
      if (!this.running) return;
      const dt = Math.min(0.05, (now - this.last) / 1000);
      this.last = now;
      this.step(dt);
      this.draw();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  private step(dt: number) {
    this.t += dt;
    if (this.mix < 1) this.mix = Math.min(1, this.mix + dt / 1.6);
    if (this.flash > 0) this.flash = Math.max(0, this.flash - dt * 1.6);
    if (this.shake > 0) this.shake = Math.max(0, this.shake - dt * 26);
    // Adaptive degradation: if frames are consistently slow, drop quality once.
    if (dt > 0.045 && this.quality !== "minimal") {
      this.lowFrames++;
      if (this.lowFrames > 120) {
        this.lowFrames = 0;
        this.setQuality(this.quality === "high" ? "medium" : this.quality === "medium" ? "low" : "minimal", this.particleScale, this.reduced);
      }
    } else if (this.lowFrames > 0) this.lowFrames--;

    const th = this.current();
    const budget = Math.floor(PARTICLE_BUDGET[this.quality] * this.particleScale * (0.4 + th.intensity));
    if (budget > 0 && !this.reduced) {
      const spawn = Math.min(budget - this.particles.length, Math.ceil(budget * dt * 1.4));
      for (let i = 0; i < spawn; i++) this.particles.push(this.spawn(th.particles));
    }
    const drag = 1 - dt * 0.35;
    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.life += dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.rot += p.vr * dt;
      if (th.particles === "bubbles" || th.particles === "petals") {
        p.vx *= drag;
        p.vy *= drag;
      }
      if (th.particles === "vortex") {
        const cx = this.w / 2;
        const cy = this.h / 2;
        const dx = cx - p.x;
        const dy = cy - p.y;
        const d = Math.hypot(dx, dy) || 1;
        p.vx += (dx / d) * 140 * dt - (dy / d) * 120 * dt;
        p.vy += (dy / d) * 140 * dt + (dx / d) * 120 * dt;
      }
      if (p.life > p.maxLife || p.x < -80 || p.x > this.w + 80 || p.y < -100 || p.y > this.h + 100) this.particles.splice(i, 1);
    }
    if (this.particles.length > budget) this.particles.splice(0, this.particles.length - budget);
  }

  private spawn(kind: string): Particle {
    const W = this.w;
    const H = this.h;
    const base = { rot: Math.random() * Math.PI * 2, vr: (Math.random() - 0.5) * 2, hueShift: Math.random() };
    switch (kind) {
      case "petals":
        return { ...base, x: Math.random() * W, y: -20, vx: (Math.random() - 0.5) * 30, vy: 24 + Math.random() * 40, life: 0, maxLife: 14, size: 4 + Math.random() * 7 };
      case "embers":
        return { ...base, x: Math.random() * W, y: H + 20, vx: (Math.random() - 0.5) * 40, vy: -(50 + Math.random() * 110), life: 0, maxLife: 6, size: 1.5 + Math.random() * 3.5 };
      case "meteors":
        return { ...base, x: Math.random() * W * 1.4 - W * 0.2, y: -40, vx: -(180 + Math.random() * 260), vy: 320 + Math.random() * 380, life: 0, maxLife: 3.4, size: 1.4 + Math.random() * 2.6 };
      case "snow":
        return { ...base, x: Math.random() * W, y: -20, vx: (Math.random() - 0.5) * 24, vy: 18 + Math.random() * 34, life: 0, maxLife: 16, size: 1.4 + Math.random() * 3 };
      case "aurora":
        return { ...base, x: Math.random() * W, y: H * (0.15 + Math.random() * 0.6), vx: (Math.random() - 0.5) * 26, vy: -(6 + Math.random() * 16), life: 0, maxLife: 9, size: 20 + Math.random() * 60 };
      case "ash":
        return { ...base, x: Math.random() * W, y: -20, vx: (Math.random() - 0.5) * 18, vy: 16 + Math.random() * 26, life: 0, maxLife: 14, size: 1 + Math.random() * 2.6 };
      case "bubbles":
        return { ...base, x: Math.random() * W, y: H + 10, vx: (Math.random() - 0.5) * 60, vy: -(20 + Math.random() * 60), life: 0, maxLife: 8, size: 3 + Math.random() * 9 };
      case "starfall":
        return { ...base, x: Math.random() * W, y: -20, vx: (Math.random() - 0.5) * 60, vy: 150 + Math.random() * 260, life: 0, maxLife: 5, size: 1.6 + Math.random() * 3.4 };
      case "glitch":
        return { ...base, x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - 0.5) * 320, vy: (Math.random() - 0.5) * 90, life: 0, maxLife: 0.9, size: 3 + Math.random() * 26 };
      case "vortex":
        return { ...base, x: Math.random() * W, y: Math.random() * H, vx: 0, vy: 0, life: 0, maxLife: 4.5, size: 1.2 + Math.random() * 3 };
      case "genesis":
        return { ...base, x: W / 2 + (Math.random() - 0.5) * 120, y: H / 2 + (Math.random() - 0.5) * 120, vx: (Math.random() - 0.5) * 420, vy: (Math.random() - 0.5) * 420, life: 0, maxLife: 3.4, size: 1.6 + Math.random() * 4 };
      case "geometry":
        return { ...base, x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - 0.5) * 20, vy: (Math.random() - 0.5) * 20, life: 0, maxLife: 8, size: 8 + Math.random() * 26 };
      case "code":
        return { ...base, x: Math.random() * W, y: -20, vx: 0, vy: 120 + Math.random() * 220, life: 0, maxLife: 7, size: 6 + Math.random() * 10 };
      case "clock":
        return { ...base, x: Math.random() * W, y: Math.random() * H, vx: 0, vy: 0, life: 0, maxLife: 6, size: 10 + Math.random() * 30 };
      default: // "stars" — slow drifting motes
        return { ...base, x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - 0.5) * 14, vy: (Math.random() - 0.5) * 14, life: 0, maxLife: 10, size: 0.8 + Math.random() * 2 };
    }
  }

  private draw() {
    const ctx = this.ctx;
    const th = this.current();
    const W = this.w;
    const H = this.h;
    ctx.save();
    if (this.shake > 0.2) ctx.translate((Math.random() - 0.5) * this.shake, (Math.random() - 0.5) * this.shake);

    // background gradient
    const g = ctx.createLinearGradient(0, 0, W * 0.4, H);
    g.addColorStop(0, th.bg[0]);
    g.addColorStop(0.55, th.bg[1]);
    g.addColorStop(1, th.bg[2]);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    // nebulae
    if (this.quality !== "minimal") {
      ctx.globalCompositeOperation = "screen";
      for (const n of this.nebulae) {
        const px = n.x + Math.sin(this.t * n.drift + n.phase) * 60 + (this.pointer.x - 0.5) * 24;
        const py = n.y + Math.cos(this.t * n.drift * 0.8 + n.phase) * 44 + (this.pointer.y - 0.5) * 18;
        const rad = ctx.createRadialGradient(px, py, 0, px, py, n.r);
        const alpha = 0.16 + th.intensity * 0.2;
        rad.addColorStop(0, css(n.color, alpha));
        rad.addColorStop(0.5, css(n.color, alpha * 0.35));
        rad.addColorStop(1, css(n.color, 0));
        ctx.fillStyle = rad;
        ctx.fillRect(px - n.r, py - n.r, n.r * 2, n.r * 2);
      }
      ctx.globalCompositeOperation = "source-over";
    }

    // stars (parallax + twinkle)
    ctx.globalCompositeOperation = "lighter";
    for (const s of this.stars) {
      const tw = this.reduced ? 0.8 : 0.55 + 0.45 * Math.sin(this.t * s.tw + s.phase);
      const px = s.x + (this.pointer.x - 0.5) * 30 * s.z + (this.warp ? Math.sin(this.t * 2 + s.phase) * this.warp * 10 * s.z : 0);
      const py = s.y + (this.pointer.y - 0.5) * 22 * s.z;
      ctx.globalAlpha = 0.25 + 0.65 * tw * s.z;
      ctx.fillStyle = tw > 0.85 && s.r > 1.2 ? th.accent : "#ffffff";
      ctx.beginPath();
      ctx.arc(px, py, s.r * (0.7 + s.z * 0.6), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;

    // particles
    const pc = hexToRgb(th.particle_color);
    const accent = hexToRgb(th.accent2);
    for (const p of this.particles) {
      const lifeT = p.life / p.maxLife;
      const fade = lifeT < 0.15 ? lifeT / 0.15 : lifeT > 0.75 ? (1 - lifeT) / 0.25 : 1;
      const col = p.hueShift > 0.7 ? accent : pc;
      ctx.globalAlpha = Math.max(0, fade) * (0.5 + th.intensity * 0.5);
      ctx.fillStyle = css(col, 1);
      switch (th.particles) {
        case "meteors":
        case "starfall": {
          ctx.strokeStyle = css(col, 0.9);
          ctx.lineWidth = p.size * 0.7;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(p.x - p.vx * 0.055, p.y - p.vy * 0.055);
          ctx.stroke();
          break;
        }
        case "aurora": {
          const rad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size);
          rad.addColorStop(0, css(col, 0.4));
          rad.addColorStop(1, css(col, 0));
          ctx.fillStyle = rad;
          ctx.fillRect(p.x - p.size, p.y - p.size, p.size * 2, p.size * 2);
          break;
        }
        case "petals": {
          ctx.save();
          ctx.translate(p.x, p.y);
          ctx.rotate(p.rot + Math.sin(this.t + p.hueShift * 6) * 0.6);
          ctx.beginPath();
          ctx.ellipse(0, 0, p.size, p.size * 0.5, 0, 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
          break;
        }
        case "glitch": {
          ctx.fillRect(p.x, p.y, p.size, 2 + p.hueShift * 3);
          break;
        }
        case "geometry": {
          ctx.save();
          ctx.translate(p.x, p.y);
          ctx.rotate(p.rot);
          ctx.strokeStyle = css(col, 0.55);
          ctx.lineWidth = 1;
          ctx.beginPath();
          const sides = 3 + Math.floor(p.hueShift * 4);
          for (let i = 0; i <= sides; i++) {
            const a = (i / sides) * Math.PI * 2;
            const x = Math.cos(a) * p.size;
            const y = Math.sin(a) * p.size;
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
          }
          ctx.stroke();
          ctx.restore();
          break;
        }
        case "code": {
          ctx.font = `${Math.round(p.size)}px ui-monospace, monospace`;
          ctx.fillText(p.hueShift > 0.5 ? "1" : "0", p.x, p.y);
          break;
        }
        case "clock": {
          ctx.strokeStyle = css(col, 0.35);
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
          ctx.moveTo(p.x, p.y);
          const ang = this.t * (0.4 + p.hueShift) + p.rot;
          ctx.lineTo(p.x + Math.cos(ang) * p.size, p.y + Math.sin(ang) * p.size);
          ctx.stroke();
          break;
        }
        case "bubbles": {
          ctx.strokeStyle = css(col, 0.5);
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
          ctx.stroke();
          break;
        }
        default: {
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";

    // warp rings
    if (this.warp > 0.01) {
      ctx.strokeStyle = css(hexToRgb(th.accent), 0.22 * this.warp);
      ctx.lineWidth = 2;
      for (let i = 0; i < 4; i++) {
        const r = ((this.t * 220 + i * 160) % Math.max(W, H)) * 0.9;
        ctx.beginPath();
        ctx.arc(W / 2, H / 2, r, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    // legibility ceiling: the sky keeps its hue, the interface keeps its stage
    if (th.ceiling > 0.01) {
      ctx.fillStyle = `rgba(3,4,12,${th.ceiling.toFixed(3)})`;
      ctx.fillRect(0, 0, W, H);
    }

    // vignette
    if (th.vignette > 0.02) {
      const vg = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.max(W, H) * 0.78);
      vg.addColorStop(0, "rgba(0,0,0,0)");
      vg.addColorStop(1, `rgba(0,0,0,${th.vignette})`);
      ctx.fillStyle = vg;
      ctx.fillRect(0, 0, W, H);
    }

    // flash
    if (this.flash > 0.01) {
      ctx.fillStyle = css(hexToRgb(this.flashColor), this.flash * 0.65);
      ctx.fillRect(0, 0, W, H);
    }
    ctx.restore();
  }
}
