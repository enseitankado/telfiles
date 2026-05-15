#!/usr/bin/env python3
"""Generate TelFiles README banner via headless Chromium."""

from pathlib import Path
from playwright.sync_api import sync_playwright

DOCS_DIR = Path(__file__).parent
OUT = DOCS_DIR / "banner.png"

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    width: 1280px;
    height: 500px;
    background: #0d1b2a;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    display: flex;
    align-items: stretch;
    overflow: hidden;
    position: relative;
  }

  /* ── gradient blobs ── */
  body::before {
    content: '';
    position: absolute;
    width: 500px; height: 500px;
    background: radial-gradient(circle, rgba(37,99,235,.18) 0%, transparent 70%);
    top: -80px; left: -80px;
    pointer-events: none;
  }
  body::after {
    content: '';
    position: absolute;
    width: 400px; height: 400px;
    background: radial-gradient(circle, rgba(99,102,241,.10) 0%, transparent 70%);
    bottom: -60px; left: 180px;
    pointer-events: none;
  }

  /* ── left column ── */
  .left {
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 52px 56px;
    flex: 0 0 580px;
    position: relative;
    z-index: 1;
  }

  .brand-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 28px;
  }
  .brand-dot {
    width: 11px; height: 11px;
    border-radius: 50%;
    background: #3b82f6;
    box-shadow: 0 0 10px rgba(59,130,246,.6);
  }
  .brand-label {
    font-size: 13px;
    font-weight: 600;
    letter-spacing: .12em;
    color: #64748b;
    text-transform: uppercase;
  }

  .title {
    font-size: 96px;
    font-weight: 800;
    color: #f1f5f9;
    line-height: 1;
    letter-spacing: -.03em;
  }
  .title span { color: #3b82f6; }

  .divider {
    width: 52px; height: 4px;
    background: linear-gradient(90deg, #3b82f6, #6366f1);
    border-radius: 2px;
    margin: 24px 0;
  }

  .meta {
    font-size: 13px;
    color: #475569;
    letter-spacing: .06em;
    margin-bottom: 32px;
  }

  /* ── feature badges ── */
  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 8px 16px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,.08);
    background: rgba(255,255,255,.04);
    font-size: 14px;
    font-weight: 600;
    color: #cbd5e1;
    backdrop-filter: blur(4px);
  }
  .badge .ico { font-size: 15px; line-height: 1; }

  /* ── right column — file-list mockup ── */
  .right {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 40px 48px 40px 24px;
    position: relative;
    z-index: 1;
  }

  .panel {
    background: rgba(255,255,255,.04);
    border: 1px solid rgba(255,255,255,.07);
    border-radius: 14px;
    padding: 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 13px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .row-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .bar-wrap {
    flex: 1;
    height: 9px;
    background: rgba(255,255,255,.06);
    border-radius: 5px;
    overflow: hidden;
  }
  .bar {
    height: 100%;
    border-radius: 5px;
    opacity: .55;
  }
  .bar-sm {
    height: 9px;
    width: 70px;
    background: rgba(255,255,255,.08);
    border-radius: 5px;
    flex-shrink: 0;
  }

  /* ── github url — top right ── */
  .github {
    position: absolute;
    top: 28px; right: 40px;
    font-size: 13px;
    color: #3b82f6;
    text-decoration: none;
    font-weight: 500;
    letter-spacing: .02em;
    border-bottom: 1px solid rgba(59,130,246,.35);
    padding-bottom: 1px;
    z-index: 2;
  }
</style>
</head>
<body>
  <a class="github">github.com/enseitankado/telfiles</a>

  <div class="left">
    <div class="brand-row">
      <div class="brand-dot"></div>
      <span class="brand-label">telfiles.io</span>
    </div>

    <div class="title">Tel<span>Files</span></div>
    <div class="divider"></div>

    <div class="meta">self-hosted &nbsp;·&nbsp; open-source &nbsp;·&nbsp; anonymous</div>

    <div class="badges">
      <div class="badge"><span class="ico">📁</span> Files</div>
      <div class="badge"><span class="ico">📡</span> Hunter</div>
      <div class="badge"><span class="ico">🔗</span> Links</div>
      <div class="badge"><span class="ico">🔔</span> Alerts</div>
      <div class="badge"><span class="ico">🐳</span> Docker</div>
    </div>
  </div>

  <div class="right">
    <div class="panel">
      <!-- simulated file rows with colour-coded type dots -->
      <div class="row">
        <div class="row-dot" style="background:#2563eb"></div>
        <div class="bar-wrap"><div class="bar" style="width:82%;background:#2563eb"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#f59e0b"></div>
        <div class="bar-wrap"><div class="bar" style="width:65%;background:#f59e0b"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#059669"></div>
        <div class="bar-wrap"><div class="bar" style="width:48%;background:#059669"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#ec4899"></div>
        <div class="bar-wrap"><div class="bar" style="width:73%;background:#ec4899"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#7c3aed"></div>
        <div class="bar-wrap"><div class="bar" style="width:55%;background:#7c3aed"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#f59e0b"></div>
        <div class="bar-wrap"><div class="bar" style="width:38%;background:#f59e0b"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#2563eb"></div>
        <div class="bar-wrap"><div class="bar" style="width:91%;background:#2563eb"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#ef4444"></div>
        <div class="bar-wrap"><div class="bar" style="width:60%;background:#ef4444"></div></div>
        <div class="bar-sm"></div>
      </div>
      <div class="row">
        <div class="row-dot" style="background:#059669"></div>
        <div class="bar-wrap"><div class="bar" style="width:77%;background:#059669"></div></div>
        <div class="bar-sm"></div>
      </div>
    </div>
  </div>
</body>
</html>"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
        )
        page = browser.new_page(viewport={"width": 1280, "height": 500})
        page.set_content(HTML, wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT), full_page=False)
        browser.close()
    print(f"Banner saved → {OUT}")


if __name__ == "__main__":
    main()
