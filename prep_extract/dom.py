"""DOM reading for the extractor (addendum §3.2). Reads fields and controls; never types, uploads or submits.
The only DOM write is a data-px-idx attribute on visible controls so a later click can target one exactly."""
import re
import time
from urllib.parse import urlsplit

from . import clicks
from .sanitize import norm_label

FIELDS_JS = r"""
(hint) => {
  const txt = (el) => (el ? String(el.innerText || el.textContent || '').slice(0, 2000) : '').replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    if (!el || el.closest('[hidden], [aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const byIds = (ids) => String(ids || '').split(/\s+/).filter(Boolean)
    .map((id) => txt(document.getElementById(id))).filter(Boolean).join(' ');
  const ownLabel = (el) => {
    const a = byIds(el.getAttribute('aria-labelledby'));
    if (a) return a;
    const b = (el.getAttribute('aria-label') || '').trim();
    if (b) return b;
    if (el.id) {
      for (const l of document.querySelectorAll('label[for]')) {
        if (l.htmlFor === el.id && txt(l)) return txt(l);
      }
    }
    const w = el.closest('label');
    if (!w) return '';
    // A card wrapped in one <label> (Lever) also holds the upload button text: prefer the adapter's title inside it.
    const t = hint && w.querySelector(hint.title);
    return (t && txt(t)) || txt(w);
  };
  const hinted = (el) => {
    if (!hint) return '';
    const c = el.closest(hint.container);
    if (!c) return '';
    return txt(c.querySelector(hint.title) || c);
  };
  const groupLabel = (el) => {
    const fs = el.closest('fieldset');
    const lg = fs && fs.querySelector('legend');
    if (lg && txt(lg)) return txt(lg);
    const g = el.closest('[role="radiogroup"], [role="group"]');
    if (g) {
      const l = byIds(g.getAttribute('aria-labelledby')) || (g.getAttribute('aria-label') || '').trim();
      if (l) return l;
    }
    return hinted(el);
  };
  const helpOf = (el) => {
    const parts = [byIds(el.getAttribute('aria-describedby'))];
    const box = el.closest('fieldset, li, div');
    if (box && box.querySelectorAll('input, textarea, select').length <= 3) {
      box.querySelectorAll('[class*="help" i], [class*="hint" i], [class*="description" i], small')
        .forEach((h) => parts.push(txt(h)));
    }
    return [...new Set(parts.filter(Boolean))].join(' ');
  };
  // A question's description sits beside its options in the group (Ashby fieldset), not beside any one option.
  const groupHelp = (el) => {
    const g = el.closest('fieldset, [role="radiogroup"], [role="group"]');
    return g ? [...g.querySelectorAll('[class*="description" i]')].map((h) => txt(h)).filter(Boolean).join(' ') : '';
  };
  const kindOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select' || el.getAttribute('role') === 'combobox') return 'select';
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    return ['radio', 'checkbox', 'file', 'email', 'tel', 'url', 'number', 'date'].includes(t) ? t : 'text';
  };
  const SKIP = ['hidden', 'submit', 'button', 'image', 'reset', 'search'];
  const fields = [];
  const seen = new Set();
  let password = false;
  for (const el of document.querySelectorAll('input, textarea, select, [role="combobox"]')) {
    if (seen.has(el)) continue;
    seen.add(el);
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'password') { if (shown(el)) password = true; continue; }
    if (SKIP.includes(t) || el.disabled || el.getAttribute('role') === 'searchbox') continue;
    let kind = kindOf(el);
    const loose = kind === 'file' || kind === 'radio' || kind === 'checkbox';  // often hidden behind styling
    const box = loose ? (el.closest('label, li, div, fieldset') || el) : el;
    if (el.closest('[hidden]') || !shown(box)) continue;
    let label = '';
    let options = [];
    let help = helpOf(el);
    // Yes/No question drawn as a button pair over a hidden checkbox (Ashby): it is a choice, not a checkbox.
    const yn = kind === 'checkbox' && el.parentElement
      ? [...el.parentElement.querySelectorAll('button[data-option]')].map((b) => txt(b)).filter(Boolean) : [];
    if (yn.length >= 2) {
      kind = 'radio';
      options = yn;
      label = groupLabel(el) || ownLabel(el);
      help = [...new Set([help, groupHelp(el)].filter(Boolean))].join(' ');
    } else if ((kind === 'radio' || kind === 'checkbox') && el.name) {
      const members = [...document.querySelectorAll('input[type="' + kind + '"]')].filter((m) => m.name === el.name);
      members.forEach((m) => seen.add(m));
      options = members.map((m) => ownLabel(m) || m.value || '').filter(Boolean);
      label = groupLabel(el) || (members.length === 1 ? ownLabel(el) : '');
      help = [...new Set([help, groupHelp(el)].filter(Boolean))].join(' ');
    } else {
      label = ownLabel(el) || hinted(el) || (el.getAttribute('placeholder') || '').trim() || (el.getAttribute('name') || '');
      if (el.tagName.toLowerCase() === 'select') {
        options = [...el.options].filter((o) => o.value !== '').map((o) => txt(o)).filter(Boolean);
      }
    }
    const ml = parseInt(el.getAttribute('maxlength') || '', 10);
    fields.push({ label, type: kind, required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      maxlength: ml > 0 ? ml : null, options, help_text: help });
    if (fields.length >= 400) break;
  }
  return { fields, password };
}
"""

_CONTROL_HELPERS_JS = r"""
  const txt = (el) => String(el.innerText || el.textContent || '').slice(0, 2000).replace(/\s+/g, ' ').trim();
  const shown = (el) => {
    if (el.closest('[hidden], [aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const BIDI = /[\u202A-\u202E\u2066-\u2069\u200E\u200F\u061C]/;
  // Every string a person or a screen reader could read as this control's name, not just the one we show. The
  // deny rule runs over all of them, so a control cannot say "Next" in one place and "Submit" in another.
  const candidates = (el) => {
    const raw = [];
    const push = (s) => { if (s) raw.push(String(s).slice(0, 2000)); };
    const pseudo = (e) => ['::before', '::after'].forEach((p) => {
      const c = getComputedStyle(e, p).content;
      if (c && c !== 'none' && c !== 'normal') push(c.replace(/^["']|["']$/g, ''));
    });
    push(el.getAttribute('aria-label'));
    String(el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .forEach((id) => { const t = document.getElementById(id); if (t) push(t.innerText || t.textContent); });
    push(el.innerText);
    push(el.textContent);
    push(el.value);
    push(el.getAttribute('title'));
    pseudo(el);
    const kids = el.querySelectorAll('*');
    for (let i = 0; i < kids.length && i < 40; i++) {
      const k = kids[i];
      push(k.getAttribute('alt'));
      push(k.getAttribute('aria-label'));
      push(k.getAttribute('title'));
      pseudo(k);
    }
    const bidi = raw.some((r) => BIDI.test(r));
    const labels = [...new Set(raw.map((r) => r.replace(/\s+/g, ' ').trim()).filter(Boolean))].slice(0, 40);
    return { labels, bidi };
  };
  const describe = (el, idx) => {
    const tag = el.tagName.toLowerCase();
    const form = el.form || el.closest('form');
    const explicit = (el.getAttribute('type') || '').toLowerCase();
    const type = tag === 'input' ? explicit : tag === 'button' ? (explicit || (form ? 'submit' : 'button')) : '';
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : 'button');
    const label = (el.getAttribute('aria-label') || '').trim() || txt(el)
      || (tag === 'input' ? String(el.value || '') : '') || (el.getAttribute('title') || '');
    const c = candidates(el);
    return { idx, tag, role, type, label, labels: c.labels, bidi: c.bidi,
      href: tag === 'a' && el.hasAttribute('href') ? el.href : null,
      target: el.getAttribute('target') || '',
      in_form: !!form,
      in_chrome: !!el.closest('header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]'),
      form_attrs: ['form', 'formaction', 'formmethod', 'formtarget', 'formenctype'].some((a) => el.hasAttribute(a)),
      tab_in_list: role === 'tab' && !!el.closest('[role="tablist"]'),
      selected: el.getAttribute('aria-selected') === 'true',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' };
  };
"""

CONTROLS_JS = "() => {" + _CONTROL_HELPERS_JS + r"""
  document.querySelectorAll('[data-px-idx]').forEach((e) => e.removeAttribute('data-px-idx'));
  const sel = 'button, input[type="submit"], input[type="button"], input[type="image"], input[type="reset"], '
    + 'a, [role="button"], [role="link"], [role="tab"]';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    if (!shown(el)) continue;
    const idx = out.length;
    el.setAttribute('data-px-idx', String(idx));
    out.push(describe(el, idx));
    if (out.length >= 300) break;
  }
  return out;
}
"""

# Re-describes one element (by handle) just before it is clicked, with the same code that found it.
ONE_CONTROL_JS = "(el) => {" + _CONTROL_HELPERS_JS + "  return describe(el, -1);\n}"

GATE_RE = re.compile(r"\b(?:sign ?in|log ?in|create (?:an )?account|sign ?up|register)\b")
_PRIMARY_RE = re.compile(r"^(?:apply(?: now| for (?:this )?(?:job|role|position))?|continue|next|start application|begin)$")
_ACCOUNTISH_RE = re.compile(r"e-?mail|user ?name")


def settle(page) -> None:
    """Wait for the page to finish loading; never raises (slow pages are read as they are)."""
    for state, ms in (("load", 10000), ("networkidle", 3000)):
        try:
            page.wait_for_load_state(state, timeout=ms)
        except Exception:
            pass


def read_page(page, hint: dict | None = None) -> dict:
    """Fields from every frame (embedded ATS iframes included), controls from the main frame only."""
    fields, password, errors = [], False, []
    for frame in page.frames:
        try:
            got = frame.evaluate(FIELDS_JS, hint)
        except Exception as exc:
            errors.append(f"frame read failed: {type(exc).__name__}: {str(exc)[:150]}")
            continue
        fields.extend(got.get("fields") or [])
        password = password or bool(got.get("password"))
    try:
        controls = page.main_frame.evaluate(CONTROLS_JS) or []
    except Exception as exc:
        controls = []
        errors.append(f"controls read failed: {type(exc).__name__}: {str(exc)[:150]}")
    return {"fields": fields, "password": password, "controls": controls, "url": page.url, "errors": errors}


def is_gate(snap: dict) -> bool:
    """Sign-in wall: a visible password field, or a Sign in / Log in / Create account control (outside the
    header/nav/footer) in front of no real questions (no fields, or only an email/username box)."""
    if snap.get("password"):
        return True
    # No questions on the page but a plain Apply / Continue action: a posting or a step, not a wall, however it is
    # laid out (div headers/footers carry no landmarks, and "Already applied? Log in" is only a hint).
    if not snap.get("fields") and any(_PRIMARY_RE.match(norm_label(c.get("label"))) for c in snap.get("controls") or []):
        return False
    # a header/nav/footer "Sign in" or "Register for alerts" link is page chrome, not a wall in front of the form
    if not any(GATE_RE.search(norm_label(c.get("label"))) and not c.get("in_chrome")
               for c in snap.get("controls") or []):
        return False
    return all(f.get("type") == "email" or _ACCOUNTISH_RE.search(norm_label(f.get("label")))
               for f in snap.get("fields") or [])


MAX_PAGES = 5


def fingerprint(snap: dict) -> tuple:
    return (snap.get("url"), tuple((f.get("label"), f.get("type")) for f in snap.get("fields") or []))


def _host(url) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def crawl(page, url: str, guard, deadline: float, hint: dict | None = None, max_pages: int = MAX_PAGES,
          clock=time.monotonic) -> dict:
    """Read up to max_pages pages, clicking only what clicks.decide() allows (addendum §3.3). Never types, picks,
    uploads or submits; a step blocked by empty required fields ends the crawl with unseen_pages=True.
    guard.check_page runs after the first load and after every click; an off-host page is never read.
    Returns raw page data: the caller sanitizes everything through schema."""
    out = {"pages": [], "clicks": [], "unseen_pages": False, "sign_in_required": False, "errors": []}
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        out["errors"].append(f"page load failed: {type(exc).__name__}: {str(exc)[:150]}")
        out["unseen_pages"] = True
        return out
    settle(page)
    if not guard.check_page(page):
        out["errors"].append("the page is off the allowed hosts; stopped")
        out["unseen_pages"] = True
        return out
    snap = read_page(page, hint)
    out["errors"].extend(snap.get("errors") or [])
    guard.lock_writes()  # the first page is rendered; from here on nothing may send data, only GETs
    tried_tabs: set[str] = set()
    for n in range(1, max_pages + 1):
        if is_gate(snap):
            out["sign_in_required"] = True
            out["unseen_pages"] = out["unseen_pages"] or n > 1
            break
        out["pages"].append((n, snap["fields"]))
        if hint and hint.get("single_page"):
            break  # the adapter knows this is one page; its tabs link to the posting overview, not to a next step
        host = _host(page.url)
        for c in snap["controls"]:
            if c.get("role") == "tab" and c.get("selected"):
                tried_tabs.add(norm_label(c.get("label")))
        choice, why = None, ""
        refused_nav = False
        for c in snap["controls"]:
            verdict = clicks.decide(c, host)
            if verdict is None:
                continue
            decision, reason = verdict
            label = c.get("label") or ""
            if decision == "deny":
                out["clicks"].append((n, label, "deny", reason))
                if ((c.get("role") == "tab" and c.get("tab_in_list"))
                        or clicks.ALLOW_RE.search(norm_label(label))):
                    refused_nav = True  # a way forward exists that we refused to take
            elif c.get("role") == "tab" and norm_label(label) in tried_tabs:
                continue  # a step we already read
            elif choice is None:
                choice, why = c, reason
            else:
                out["clicks"].append((n, label, "skipped", "another navigation control was clicked"))
        if choice is None:
            out["unseen_pages"] = out["unseen_pages"] or refused_nav
            break
        label = choice.get("label") or ""
        if n == max_pages:
            out["clicks"].append((n, label, "not_clicked", f"page limit ({max_pages}) reached"))
            out["unseen_pages"] = True
            break
        if clock() > deadline:
            out["clicks"].append((n, label, "not_clicked", "time limit reached"))
            out["errors"].append("time limit reached; later pages not read")
            out["unseen_pages"] = True
            break
        if choice.get("role") == "tab":
            tried_tabs.add(norm_label(label))
        before = fingerprint(snap)
        try:
            handle = page.query_selector(f'[data-px-idx="{int(choice["idx"])}"]')
            now = handle.evaluate(ONE_CONTROL_JS) if handle else None
        except Exception:
            handle, now = None, None
        again = clicks.decide(now, host) if now else None
        if (again is None or again[0] != "allow" or norm_label(now.get("label")) != norm_label(label)
                or sorted(now.get("labels") or []) != sorted(choice.get("labels") or [])):
            out["clicks"].append((n, label, "deny", "control changed before click"))
            out["unseen_pages"] = True
            break
        out["clicks"].append((n, label, "clicked", why))
        try:
            handle.click(timeout=5000)
        except Exception as e:
            out["errors"].append(f"click on '{label}' failed: {type(e).__name__}")
            out["unseen_pages"] = True
            break
        settle(page)
        if not guard.check_page(page):
            out["errors"].append("the page left the allowed hosts; stopped")
            out["unseen_pages"] = True
            break
        snap = read_page(page, hint)
        out["errors"].extend(snap.get("errors") or [])
        # A navigation that was still in flight (or blocked into an error page) while we read: not a page to keep.
        if snap.get("errors") or not guard.check_page(page):
            out["errors"].append("the page changed while it was being read; stopped")
            out["unseen_pages"] = True
            break
        if fingerprint(snap) == before:
            out["clicks"].append((n, label, "no_change",
                                  "page did not change (required fields are empty or the step was blocked)"))
            out["unseen_pages"] = True
            break
    return out
