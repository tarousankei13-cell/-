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

const ROUTES = ["roll", "inventory", "collection", "equipment", "biomes", "shop", "market",
                "trade", "quests", "achievements", "ranking", "profile", "settings"];
const SAMPLE = "h1,h2,h3,label,p,td,.chip,.btn,input,.kpi,.stat";
const MAX_PER_PAGE = Number(process.env.MAX_PER_PAGE || 8);

const pass = [], fail = [];

/** True when the element puts pixels on the screen. */
async function paints(page, handle) {
  const box = await handle.boundingBox();
  if (!box || box.width < 2 || box.height < 2) return null;
  const clip = { x: Math.max(0, box.x), y: Math.max(0, box.y),
                 width: Math.min(box.width, 700), height: Math.min(box.height, 200) };
  const shown = await page.screenshot({ clip });
  await handle.evaluate((e) => { e.style.visibility = "hidden"; });
  await page.waitForTimeout(70);
  const hidden = await page.screenshot({ clip });
  await handle.evaluate((e) => { e.style.visibility = ""; });
  return !(shown.length === hidden.length && shown.equals(hidden));
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
      return b.width >= 8 && b.height >= 8 && b.top >= 0 && b.bottom <= innerHeight &&
             b.left >= 0 && b.right <= innerWidth && cs.visibility !== "hidden" &&
             cs.display !== "none" && cs.opacity !== "0" &&
             (!!e.textContent?.trim() || e.tagName === "INPUT");
    });
    if (!visible) continue;
    const painted = await paints(page, h);
    if (painted === null) continue;
    checked += 1;
    if (!painted) {
      const what = await h.evaluate((e) => `<${e.tagName.toLowerCase()} class="${String(e.className).slice(0, 40)}"> ${(e.textContent || "").trim().slice(0, 30)}`);
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
