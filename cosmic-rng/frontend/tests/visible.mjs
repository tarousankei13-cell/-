/**
 * Paint-level smoke test.
 *
 * The login screen once rendered with everything in place — correct size,
 * correct colour, clickable — and painted entirely underneath the fixed
 * starfield canvas, so nobody could see it. Every DOM-level check passed: the
 * elements existed and accepted fill(). Asserting on the DOM cannot catch that.
 *
 * So this asks the renderer. For each element it hides it, photographs its box,
 * shows it, photographs again, and requires the two images to differ. If they
 * match, the element contributed no pixels and is invisible to a player.
 *
 *   node tests/visible.mjs [base-url] [username] [password]
 *
 * Needs a running server and playwright. Install it where you run the test
 * (`npm i -D playwright && npx playwright install chromium`); it is deliberately
 * not a dependency of the app. Set CHROMIUM_PATH to reuse a browser you have.
 */
import { chromium } from "playwright";

const BASE = (process.argv[2] || process.env.BASE_URL || "http://localhost:8000").replace(/\/?$/, "/");
const USER = process.argv[3] || process.env.TEST_USER || "admin";
const PASS = process.argv[4] || process.env.TEST_PASSWORD || "";

const ROUTES = ["roll", "inventory", "collection", "sets", "equipment", "biomes", "shop", "shards",
                "market", "trade", "friends", "guild", "events", "quests", "achievements", "ranking",
                "pass", "fortune", "profile", "settings"];
const SAMPLE = "h1,h2,h3,label,p,td,.chip,.btn,input,.kpi,.stat";
const MAX_PER_PAGE = Number(process.env.MAX_PER_PAGE || 8);

const pass = [], fail = [];

/** True when the element puts pixels on the screen, null when not testable. */
async function paints(page, handle) {
  const box = await handle.boundingBox();
  if (!box || box.width < 2 || box.height < 2) return null;
  // The bottom navigation is fixed over the page on a phone. An element parked
  // under it is not an invisible element — the player scrolls and there it is.
  // Comparing its box would only measure the nav, so skip those.
  const undernav = await page.evaluate((b) => {
    const nav = document.querySelector(".bottom-nav");
    if (!nav) return false;
    const n = nav.getBoundingClientRect();
    return n.top < innerHeight && b.y + b.height > n.top;
  }, box);
  if (undernav) return null;
  const clip = { x: Math.max(0, box.x), y: Math.max(0, box.y),
                 width: Math.min(box.width, 700), height: Math.min(box.height, 200) };
  // A paint is a frame: wait for the compositor to produce one after each
  // toggle rather than a fixed delay. The music scheduler and the starfield
  // both take main-thread time, and a fixed 70ms sometimes captured the
  // previous frame, which reported a visible chip as unpainted.
  const frame = () => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(true)))));
  const capture = async (hide) => {
    await handle.evaluate((e, h) => { e.style.visibility = h ? "hidden" : ""; }, hide);
    await frame();
    return page.screenshot({ clip });
  };
  for (let attempt = 0; attempt < 2; attempt++) {
    const shown = await capture(false);
    const hidden = await capture(true);
    await handle.evaluate((e) => { e.style.visibility = ""; });
    if (!(shown.length === hidden.length && shown.equals(hidden))) return true;
    await page.waitForTimeout(250); // once more, generously, before calling it invisible
  }
  return false;
}

/** Nothing can be compared against a moving background. */
const freezeBackdrop = (page, hide) =>
  page.evaluate((h) => { const c = document.getElementById("cosmos"); if (c) c.style.visibility = h ? "hidden" : ""; }, hide);

async function sweep(page, label) {
  await freezeBackdrop(page, true);
  await page.waitForTimeout(120);
  const handles = await page.$$(SAMPLE);
  let checked = 0;
  // Two screenshots per element, so sample rather than sweep: a page that has
  // fallen out of the painted layer loses all of it at once, not one label.
  for (const h of handles.slice(0, MAX_PER_PAGE)) {
    const visible = await h.evaluate((e) => {
      const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
      if (!(b.width >= 8 && b.height >= 8 && b.top >= 0 && b.bottom <= innerHeight &&
            b.left >= 0 && b.right <= innerWidth && cs.visibility !== "hidden" &&
            cs.display !== "none" && (!!e.textContent?.trim() || e.tagName === "INPUT"))) return false;
      // opacity does not inherit, so a transparent ancestor (a fading wrapper, a
      // hidden duplicate header) leaves the element with a box and opacity 1
      // while painting nothing — that is not a page bug, it is the ancestor
      for (let p = e; p && p !== document.documentElement; p = p.parentElement) {
        if (parseFloat(getComputedStyle(p).opacity) === 0) return false;
      }
      // and something else may simply be on top of it (a toast, a fixed bar)
      const top = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
      return !!top && (top === e || e.contains(top) || top.contains(e));
    });
    if (!visible) continue;
    // React re-renders live values (the balance, rarity counts) between the
    // moment the handles were collected and the moment this one is tested. A
    // handle to a node React has since replaced toggles nothing on screen —
    // that says nothing about the page, so it is skipped rather than failed.
    if (!(await h.evaluate((e) => e.isConnected))) continue;
    const painted = await paints(page, h);
    if (painted === null) continue;
    if (!painted && !(await h.evaluate((e) => e.isConnected))) continue;
    checked += 1;
    if (!painted) {
      const what = await h.evaluate((e) => { const b = e.getBoundingClientRect();
        return `<${e.tagName.toLowerCase()} class="${String(e.className).slice(0, 40)}"> ${(e.textContent || "").trim().slice(0, 30)} @${Math.round(b.x)},${Math.round(b.y)} ${Math.round(b.width)}x${Math.round(b.height)}`; });
      fail.push(`${label}: 描画されていない ${what}`);
    }
  }
  await freezeBackdrop(page, false);
  if (checked) pass.push(`${label}: ${checked} 要素が描画されている`);
}

// CHROMIUM_PATH lets a CI image point at a browser it already has, instead of
// making `playwright install` a prerequisite.
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
);
try {
  for (const vp of [{ n: "desktop", width: 1400, height: 1000 }, { n: "mobile", width: 390, height: 844 }]) {
    const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
    const page = await ctx.newPage();

    // Signed out: the login screen is the one page every visitor must see.
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForTimeout(1200);
    await sweep(page, `ログイン画面 [${vp.n}]`);

    const layered = await page.evaluate(() => {
      const c = document.getElementById("cosmos"), p = document.querySelector(".page");
      if (!c || !p) return null;
      const cz = getComputedStyle(c), pz = getComputedStyle(p);
      return pz.position !== "static" && pz.zIndex !== "auto" && Number(pz.zIndex) > Number(cz.zIndex || 0);
    });
    (layered ? pass : fail).push(`背景より上の層 [${vp.n}]${layered ? "" : " — .page が星空の下にいます"}`);

    if (PASS) {
      await page.fill('input[autocomplete="username"]', USER);
      await page.fill('input[autocomplete="current-password"]', PASS);
      await page.click('button[type="submit"]');
      await page.waitForSelector(".roll-button", { timeout: 25000 });
      // A brand-new account meets the tutorial first; it covers the roll screen.
      const skip = page.getByRole("button", { name: "スキップ" });
      if (await skip.count()) { await skip.first().click(); await page.waitForTimeout(400); }
      for (const r of ROUTES) {
        await page.goto(BASE + r, { waitUntil: "networkidle" }).catch(() => {});
        await page.waitForTimeout(900);
        await sweep(page, `${r} [${vp.n}]`);
      }
    }
    await ctx.close();
  }
} finally {
  await browser.close();
}

for (const p of pass) console.log("  ✓", p);
for (const f of fail) console.log("  ✗", f);
console.log(`\n${pass.length} passed, ${fail.length} failed`);
process.exit(fail.length ? 1 : 0);
