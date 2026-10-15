from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_avoids_rebuilding_unchanged_rows_and_details():
    js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    assert "S.accountListSnapshot===renderKey" in js
    assert "snapshot===S.detailSnapshot" in js
    assert "},10000);setInterval(tick,1000);" in js


def test_overlay_scroll_is_restored_and_mobile_drawer_is_touch_scrollable():
    js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/css/ui-v14.css").read_text(encoding="utf-8")
    assert "function syncPageScrollLock()" in js
    assert "window.scrollTo(0,restoreY)" in js
    assert "overscroll-behavior:contain" in css
    assert "-webkit-overflow-scrolling:touch" in css
    assert "height:100dvh" in css
    assert "@media(hover:none) and (pointer:coarse)" in css
    assert "backdrop-filter:none!important" in css
