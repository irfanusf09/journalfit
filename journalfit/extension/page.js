// Runs in every frame of a tab the author switched JournalFit on for.
// JF_RUN applies the steps the app decided (type, choose, tick, attach, and in autopilot mode
// safe Next / Add author / Add reviewer clicks). Submit is never clicked.
// JF_WATCH tells the extension when the frame shows a different form page.

(() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const FORBIDDEN = /submit|approv|build\s*(the\s*)?pdf|\bpdf\b|finish|complete|finali[sz]e|\bsend\b|\bpay|payment|checkout|purchase|order|delete|remove|withdraw|cancel|discard|reset|clear|log\s*-?\s*(out|off)|sign\s*-?\s*(out|off)|\bagree|\baccept|\bconsent|\bsign\b|transfer|decline|reject|\bclose\b|\bexit\b|\bquit\b|unsubscribe|upload|browse|attach|choose\s+file|select\s+file|print|download|preview|back|previous|\bprev\b|search|find|lookup|look\s+up|verify|validate|confirm|\bok\b|\byes\b|\bno\b|edit|change|start\s+new|new\s+submission|create|register|save\s+(as\s+)?draft|save\s+and\s+(close|exit)|help|contact|feedback|faq|instructions|copy|duplicate|revise|resubmit|proof/i;

  // Boxes inside shadow roots are found too (the page reader looks there as well).
  const find = (sel) => {
    const look = (root) => {
      const hit = root.querySelector(sel);
      if (hit) return hit;
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) { const h = look(el.shadowRoot); if (h) return h; }
      }
      return null;
    };
    return look(document);
  };

  const fire = (el, ...types) => types.forEach((t) => el.dispatchEvent(new Event(t, {bubbles: true})));

  // Sites built with React/Angular/Vue only notice a value set through the element's own setter.
  const setValue = (el, value) => {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, value);
    } else {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
      fire(el, 'input', 'change');
    }
  };

  const choose = (el, label) => {
    const opt = [...el.options].find((o) => norm(o.text) === norm(label));
    if (!opt) throw new Error(`option '${label}' not in the list`);
    el.value = opt.value;
    opt.selected = true;
    fire(el, 'input', 'change');
  };

  const attach = (el, files) => {
    const dt = new DataTransfer();
    for (const f of files) {
      const bytes = Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0));
      dt.items.add(new File([bytes], f.name, {type: f.type}));
    }
    el.files = dt.files;
    fire(el, 'input', 'change');
  };

  // A key press the way the site's own code reads it (Enter turns a typed keyword into a chip).
  const CODES = {Enter: 13, Tab: 9, ',': 188, ArrowDown: 40};
  const press = (el, key) => {
    el.focus();
    for (const type of ['keydown', 'keypress', 'keyup']) {
      el.dispatchEvent(new KeyboardEvent(type, {key, code: key, keyCode: CODES[key] || 0, which: CODES[key] || 0,
                                                bubbles: true, cancelable: true}));
    }
  };

  const BAD_ARTICLE = /review|letter|editorial|comment|short|brief|erratum|correction|book|perspective|case/i;
  const tryComboOption = async (el, value) => {
    const isCombo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list' ||
      /^id_[0-9a-f-]{20,}$/i.test(el.id || '') || (value === 'Research Article' || value === 'Review Article');
    if (!isCombo) return false;
    el.focus();
    if (el.parentElement) el.parentElement.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
    el.click();
    press(el, 'ArrowDown');
    await sleep(220);
    const opts = [...document.querySelectorAll('[role="option"], mat-option, [id*="-option-"], [class*="-option"]')]
      .filter((o) => { const r = o.getBoundingClientRect(); return r.width > 0 && r.height > 0 && norm(o.innerText || o.textContent); });
    if (!opts.length) return false;
    let hit = opts.find((o) => norm(o.innerText || o.textContent).toLowerCase() === norm(value).toLowerCase());
    if (!hit && value === 'Research Article') {
      hit = opts.find((o) => { const t = norm(o.innerText || o.textContent); return /original|research/i.test(t) && !BAD_ARTICLE.test(t); }) ||
            opts.find((o) => /^\s*(full[\s-]*length\s+)?article\s*$/i.test(norm(o.innerText || o.textContent)));
    } else if (!hit && value === 'Review Article') {
      hit = opts.find((o) => { const t = norm(o.innerText || o.textContent); return /review/i.test(t) && !/book/i.test(t); });
    }
    if (!hit) return false;
    hit.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
    hit.click();
    return true;
  };

  self.JF_RUN = async (ops) => {
    const failed = [];
    let focused = null;
    let comboPicked = false;
    for (const op of ops) {
      try {
        if (op.op === 'wait') { await sleep(op.ms); continue; }
        if (op.op === 'insert') {
          const target = focused || document.activeElement || document.body;
          target.focus();
          try {
            const sel = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(target);
            sel.removeAllRanges();
            sel.addRange(range);
          } catch (e) {}
          document.execCommand('insertText', false, op.value);
          if (!norm(target.innerText || target.textContent)) {
            target.textContent = op.value;
            fire(target, 'input', 'change', 'blur');
          }
          continue;
        }
        if (op.op === 'click_btn' && failed.length > 0) continue;  // never advance if a field failed
        const el = find(op.sel);
        if (!el) throw new Error('box no longer on the page');
        if (op.op === 'fill') {
          comboPicked = await tryComboOption(el, op.value);
          if (!comboPicked) setValue(el, op.value);
        } else if (op.op === 'select') choose(el, op.label);
        else if (op.op === 'check') {
          if (!el.checked) el.click();
          if (!el.checked) { el.checked = true; fire(el, 'input', 'change'); }
        } else if (op.op === 'click') { el.focus(); el.click(); focused = el; comboPicked = false; }  // only boxes, never buttons
        else if (op.op === 'click_btn') {
          if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
          await sleep(150);
          const words = [el.innerText || el.textContent || '', el.value || '', el.getAttribute('title') || '',
                         el.getAttribute('aria-label') || '', el.getAttribute('alt') || ''].join(' ');
          if (FORBIDDEN.test(words)) throw new Error('forbidden button wording; not clicked');
          el.click();
        } else if (op.op === 'files') attach(el, op.files);
        else if (op.op === 'press') {
          if (!(comboPicked && op.key === 'Enter')) press(el, op.key);
          comboPicked = false;
        }
        await sleep(60);  // let the site react, as it would to typing
      } catch (e) {
        failed.push({op: op.op, sel: op.sel || '', paths: op.paths || [], error: String(e && e.message || e)});
      }
    }
    // Leave the last box, as a person would: sites check a box's value when you move on.
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    return failed;
  };

  self.JF_WATCH = () => {
    if (self.JF_WATCHING) return;
    self.JF_WATCHING = true;
    let last = '', seen = '';
    setInterval(() => {
      let sig = '';
      try { sig = self.JF_SIGNATURE(); } catch (e) { return; }
      // A new page counts once it has stopped changing for one look.
      if (sig === seen && sig !== last) {
        last = sig;
        chrome.runtime.sendMessage({type: 'page-changed'}).catch(() => {});
      }
      seen = sig;
    }, 1200);
  };
})();
