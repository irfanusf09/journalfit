// Generated from journalfit/submit/assistant.py by journalfit.submit.remote.write_generated_js().
// Do not edit: change COLLECT_JS / PAGE_SIGNATURE_JS there and regenerate.
self.JF_COLLECT = (startIdx) => {
  const out = [];
  let idx = startIdx;
  const txt = (n) => ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const rootOf = (el) => el.getRootNode();
  const byId = (el, id) => {
    const r = rootOf(el);
    return (r.getElementById && r.getElementById(id)) || document.getElementById(id);
  };
  // Label texts, nearest first: explicit labels and attributes, then text just before the
  // field, walking up through the page structure (modern forms put labels in nearby blocks).
  const labelsFor = (el) => {
    const bits = [];
    const add = (t) => { t = (t || '').replace(/\s+/g, ' ').trim(); if (t) bits.push(t.slice(0, 200)); };
    if (el.id) {
      const r = rootOf(el);
      const l = r.querySelector && r.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) add(txt(l));
    }
    const wrap = el.closest('label'); if (wrap) add(txt(wrap));
    for (const a of ['aria-label', 'placeholder', 'data-placeholder', 'title', 'name', 'id', 'data-testid']) add(el.getAttribute(a));
    for (const a of ['aria-labelledby', 'aria-describedby']) {
      const v = el.getAttribute(a);
      if (v) v.split(/\s+/).forEach(id => { const n = byId(el, id); if (n) add(txt(n)); });
    }
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) add(txt(lg)); }
    let node = el;
    for (let depth = 0; depth < 6 && node; depth++) {
      let sib = node.previousElementSibling, hops = 0;
      while (sib && hops < 3) {
        if (!sib.matches('input, textarea, select, style, script, link, meta, head, noscript') && txt(sib)) {
          add(txt(sib).slice(0, 160)); break;
        }
        sib = sib.previousElementSibling; hops++;
      }
      if (node.tagName === 'TD') { const row = node.closest('tr'); if (row) add(txt(row)); }
      node = node.parentElement || (rootOf(node) && rootOf(node).host) || null;
    }
    return bits;
  };
  const all = [];
  const walk = (r) => {
    r.querySelectorAll('input, textarea, select, [contenteditable="true"], [contenteditable=""]').forEach(e => all.push(e));
    r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  const groups = new Map();  // radio buttons are one question per name
  for (const el of all) {
    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && (itype === 'checkbox' || itype === 'radio')) {
      if (el.disabled || !visible(el.closest('label') || el.parentElement || el)) continue;
      // A tick box is described by its own label and the nearest sentence before it, never by neighbours.
      const own = el.closest('label') ? txt(el.closest('label')) :
        (el.id && rootOf(el).querySelector && rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`)
          ? txt(rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`)) : (el.getAttribute('aria-label') || ''));
      let near = '', node = el.closest('label') || el;
      for (let depth = 0; depth < 4 && node && !near; depth++) {
        let sib = node.previousElementSibling, hops = 0;
        while (sib && hops < 2 && !near) {
          if (!sib.querySelector('input') && !sib.matches('input, style, script') && txt(sib)) near = txt(sib).slice(0, 200);
          sib = sib.previousElementSibling; hops++;
        }
        node = node.parentElement;
      }
      const bits = [own, near].filter(Boolean).map(t => t.slice(0, 200));
      if (itype === 'checkbox') {
        el.setAttribute('data-jf-idx', String(idx));
        out.push({idx: idx++, type: 'checkbox', tag, bits, label: bits.join(' | '), value: el.checked ? 'checked' : '',
                  required: !!el.required || el.getAttribute('aria-required') === 'true' || /\*|required/i.test(bits.slice(0, 2).join(' ')),
                  visible: true, options: [], maxlength: 0, multiple: false});
      } else {
        const name = el.name || ('anon' + idx);
        let g = groups.get(name);
        if (!g) {
          // The question is the text before the group, not each button's own label.
          const box = el.closest('fieldset, tr, li, .form-group, .question, div') || el.parentElement;
          g = {idx: idx++, type: 'radio', tag, bits: [], label: '', value: '', required: false, visible: true,
               options: [], maxlength: 0, multiple: false, _box: box};
          groups.set(name, g);
          out.push(g);
        }
        el.setAttribute('data-jf-idx', String(g.idx));
        el.setAttribute('data-jf-opt', String(g.options.length));
        g.options.push(bits[0] || el.value || '');
        if (el.checked) g.value = bits[0] || el.value || 'chosen';
        g.required = g.required || !!el.required;
        if (!g.bits.length) {
          let q = [], node = el;
          for (let depth = 0; depth < 6 && node && q.length < 2; depth++) {
            let sib = node.previousElementSibling, hops = 0;
            while (sib && hops < 3) {
              if (!sib.querySelector('input') && txt(sib)) { q.push(txt(sib).slice(0, 200)); break; }
              sib = sib.previousElementSibling; hops++;
            }
            node = node.parentElement;
          }
          const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) q.unshift(txt(fs.querySelector('legend')));
          g.bits = q.length ? q : [el.name || 'question'];
          g.label = g.bits.join(' | ');
          g.required = g.required || /\*|required/i.test(g.label);
        }
      }
      continue;
    }
    const rich = el.isContentEditable && tag !== 'input' && tag !== 'textarea';
    if (rich && el.parentElement && el.parentElement.closest('[contenteditable="true"]')) continue;
    const type = rich ? 'richtext' : tag === 'select' ? 'select' : (el.getAttribute('type') || tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'checkbox', 'radio', 'password', 'search'].includes(type)) continue;
    if (el.disabled || el.readOnly) continue;
    const shown = visible(el);
    // Lists are kept even when hidden behind a custom-styled drop-down (the site reads the real list).
    if (type !== 'file' && type !== 'select' && !shown) continue;
    el.setAttribute('data-jf-idx', String(idx));
    let value = el.value || '';
    if (rich) value = txt(el);
    if (tag === 'select') value = el.selectedIndex > 0 ? txt(el.options[el.selectedIndex]) : '';
    const bits = labelsFor(el);
    const required = !!el.required || el.getAttribute('aria-required') === 'true' ||
      /\*|required/i.test(bits.slice(0, 2).join(' '));
    out.push({idx: idx++, type, tag, bits, label: bits.join(' | '), value, multiple: !!el.multiple,
              maxlength: el.maxLength > 0 ? el.maxLength : 0, required, visible: shown,
              options: tag === 'select' ? [...el.options].map(o => txt(o)) : []});
  }
  out.forEach(f => { delete f._box; });
  return out;
};
self.JF_SIGNATURE = () => {
  let n = 0, names = '';
  const walk = (r) => { r.querySelectorAll('input, textarea, select, [contenteditable]').forEach(e => {
                          n++; names += (e.name || e.id || '') + ','; });
                        r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  const heads = [...document.querySelectorAll('h1, h2, h3, legend, .step.active, [aria-current="step"]')]
    .map(h => (h.innerText || '').trim()).join('/').slice(0, 300);
  let h = 0;
  for (const c of names + heads) h = (h * 31 + c.charCodeAt(0)) | 0;
  return location.href.split('#')[0] + ' ' + n + ' ' + h;
};
