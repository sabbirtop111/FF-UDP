"""Real-browser mobile audit: opens the live console at phone size and measures breakage.

Run: python3 tests/browser_mobile_audit.py [--shots DIR]
Exits non-zero when a surface overflows the viewport (the "squeezed / cut off" bug).
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8000"
ADMIN_USER = "admin"
ADMIN_PASS = "GcV17-NeonGlass-Demo-2026!"
SHOTS = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else Path("/tmp/shots")
DEVICES = [("phone", 360, 780, 3), ("small-phone", 320, 690, 2), ("tablet", 768, 1024, 2)]

MEASURE = """
() => {
  const vw = window.innerWidth;
  const offenders = [];
  const seen = new Set();
  const add = (label, r) => offenders.push({ label, left: Math.round(r.left), right: Math.round(r.right),
                                             width: Math.round(r.width), vw, overflowRight: Math.round(r.right - vw) });
  const probe = (label, el) => {
    if (!el || seen.has(el)) return; const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    if (r.right - vw > 2 || r.left < -2) add(label, r);
  };
  const sels = { modal: '#modal', modal_body: '#modal-body', drawer: '#detail-drawer', shell: '#admin-shell',
                 rail: '.rail', topbar: '.workspace-top', dock: '#live-dock', log: '#log-stream',
                 chart: '.chart-table', safety: '#safety-grid', daily: '#daily-watch',
                 accounts: '#account-list', settings: '#view-settings', activity: '#activity-list',
                 toolbar: '.directory-toolbar', stats: '#overview-stats', toast: '.toast' };
  for (const [name, sel] of Object.entries(sels)) document.querySelectorAll(sel).forEach(el => probe(name, el));
  document.querySelectorAll('body *').forEach(el => {
    if (el.closest('.table-scroll')) return;   // wide tables scroll on purpose
    const r = el.getBoundingClientRect();
    if (r.width > vw + 4 && r.height > 0 && el.children.length === 0)
      add('wide:' + el.tagName.toLowerCase() + '.' + String(el.className || '').slice(0, 32), r);
  });
  return { vw, offenders: offenders.slice(0, 30) };
}
"""


async def click_action(page, action):
    """Click the visible control for an action; fall back to a scripted click."""
    locator = page.locator(f'[data-action="{action}"]:visible').first
    try:
        await locator.click(timeout=4000)
        return True
    except Exception:
        pass
    return await page.evaluate("""(action) => {
        const els = Array.from(document.querySelectorAll(`[data-action="${action}"]`));
        const el = els.find(e => e.offsetParent !== null) || els[0];
        if (!el) return false;
        el.click();
        return true;
    }""", action)


async def goto_view(page, view):
    await page.evaluate("""(view) => {
        const el = Array.from(document.querySelectorAll(`[data-view="${view}"]`)).find(e => e.offsetParent !== null)
                || document.querySelector(`[data-view="${view}"]`);
        if (el) el.click();
    }""", view)
    await page.wait_for_timeout(450)


async def run_device(name, width, height, scale, problems):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(
            viewport={"width": width, "height": height}, device_scale_factor=scale,
            is_mobile=True, has_touch=True,
            user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Mobile Safari/537.36")
        page = await context.new_page()
        page.on("pageerror", lambda e: problems.append((name, "js-error", str(e))))
        await page.goto(f"{BASE}/admin", wait_until="domcontentloaded")
        await page.wait_for_timeout(900)
        await page.fill('[name=username]', ADMIN_USER)
        await page.fill('[name=password]', ADMIN_PASS)
        await page.evaluate("document.querySelector('#login-form').requestSubmit()")
        await page.wait_for_timeout(1500)

        async def snap(tag):
            await page.screenshot(path=str(SHOTS / f"{name}-{tag}.png"), full_page=False)
            res = await page.evaluate(MEASURE)
            problems.append((name, tag, res))

        await snap("01-overview")

        # --- Add Bot (user complaint #1) ---
        await goto_view(page, "accounts")
        await snap("02-accounts")
        await click_action(page, "open-add")
        await page.wait_for_timeout(500)
        await snap("03-add-bot")
        boxes = await page.evaluate("""() => {
          const m = document.querySelector('#modal'), r = m.getBoundingClientRect();
          const f = document.querySelector('#account-form');
          return { viewport: innerWidth, modal: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
                   modalScrollW: m.scrollWidth, modalClientW: m.clientWidth,
                   form: f ? Math.round(f.getBoundingClientRect().width) : null,
                   fields: Array.from(document.querySelectorAll('#account-form [name]')).map(n => ({name: n.name, w: Math.round(n.getBoundingClientRect().width)})),
                   modalVisible: !m.classList.contains('is-hidden') };
        }""")
        problems.append((name, "add-bot-metrics", boxes))
        await click_action(page, "close-modal")
        await page.wait_for_timeout(300)

        # --- EXP reference / chart (user complaint #2) ---
        await goto_view(page, "settings")
        await snap("04-settings")
        await click_action(page, "open-chart")
        await page.wait_for_timeout(600)
        await snap("05-chart")
        chart = await page.evaluate("""() => {
          const t = document.querySelector('.chart-table');
          const m = document.querySelector('#modal');
          if (!t) return null;
          const r = t.getBoundingClientRect();
          return { tableW: Math.round(r.width), tableX: Math.round(r.x), tableScrollW: t.scrollWidth, vw: innerWidth,
                   modalW: m ? Math.round(m.getBoundingClientRect().width) : null,
                   modalScrollW: m ? m.scrollWidth : null,
                   bodyScrollW: document.querySelector('#modal-body') ? document.querySelector('#modal-body').scrollWidth : null };
        }""")
        problems.append((name, "chart-metrics", chart))
        await click_action(page, "close-modal")
        await page.wait_for_timeout(250)

        # --- other surfaces ---
        await goto_view(page, "activity")
        await snap("06-activity")
        await goto_view(page, "overview")
        await snap("07-overview-final")

        await browser.close()


async def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list = []
    for name, w, h, s in DEVICES:
        await run_device(name, w, h, s, problems)

    print("\n=== MOBILE AUDIT ===")
    overflow = 0
    for name, tag, info in problems:
        if isinstance(info, dict) and "offenders" in info:
            if info["offenders"]:
                for off in info["offenders"]:
                    overflow += 1
                    print(f"  ✗ {name}/{tag}: {off['label']} left={off['left']} right={off['right']} w={off['width']} vw={off['vw']} overflow={off['overflowRight']}")
            else:
                print(f"  ✓ {name}/{tag}: nothing overflows")
        else:
            print(f"  · {name}/{tag}: {info}")
    print(f"total overflow problems: {overflow}")
    sys.exit(1 if overflow else 0)


asyncio.run(main())
