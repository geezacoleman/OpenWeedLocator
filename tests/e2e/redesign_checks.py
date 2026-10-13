"""Shared helpers for the redesign browser checks (standalone + networked).

CHECK_JS: page scroll size, visible buttons under 44 px, and any ' · '
joining items in the visible text. INSIDE_JS: is an element fully on
screen. fake_frame_jpeg(): a field-looking picture for screenshots.
Not a test module (no test_ prefix).
"""

# Every visible button / link-button: >= 44 px; text without ' · '
CHECK_JS = """
() => {
  const out = {small: [], dots: [], scroll: null};
  const de = document.scrollingElement;
  out.scroll = {h: de.scrollHeight, w: de.scrollWidth, ih: innerHeight, iw: innerWidth};
  const visible = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && el.offsetParent !== null; };
  document.querySelectorAll('button, a.ou-btn, a.ou-row, select, .ou-cls, .ou-lab.add, .leaflet-bar a').forEach((b) => {
    if (!visible(b)) return;
    if (b.closest('.notification-panel, .agent-panel, .config-sections, .numpad-overlay, .kb-overlay')) return;
    const r = b.getBoundingClientRect();
    if (r.height < 44 || r.width < 44) out.small.push((b.id || b.className || b.tagName) + ' ' + (b.textContent || '').trim().slice(0, 30) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
  });
  if (document.body.innerText.indexOf(' · ') !== -1) out.dots.push(document.body.innerText.split('\\n').filter((l) => l.indexOf(' · ') !== -1).slice(0, 5));
  return out;
}
"""

INSIDE_JS = """
(sel) => { const el = document.querySelector(sel); if (!el) return 'missing';
  const r = el.getBoundingClientRect();
  return (r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight + 0.5 && r.right <= innerWidth + 0.5) ? 'ok'
         : `outside ${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.right)},${Math.round(r.bottom)}`; }
"""



def fake_frame_jpeg(width=728, height=544):
    """Soil stripes with three green weeds, as JPEG bytes."""
    import cv2
    import numpy as np
    img = np.zeros((height, width, 3), np.uint8)
    for x in range(-height, width, 30):
        cv2.line(img, (x, height), (x + height // 5, 0), (56, 80, 107), 14)
        cv2.line(img, (x + 15, height), (x + 15 + height // 5, 0), (76, 106, 138), 14)
    for (cx, cy, r) in ((210, 330, 48), (470, 180, 30), (600, 420, 20)):
        cv2.ellipse(img, (cx, cy), (r + 12, r), 0, 0, 360, (65, 142, 62), -1)
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes()
