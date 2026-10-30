// Runs in every frame of a tab the author switched JournalFit on for.
// JF_RUN applies the steps the app decided (type, choose, tick, attach). It never presses a
// button: Next, Add author and Submit are always the author's own clicks.
// JF_WATCH tells the extension when the frame shows a different form page.

(() => {
  if (self.JF_RUN) return;  // already set up in this frame

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

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
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
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
  const CODES = {Enter: 13, Tab: 9, ',': 188};
  const press = (el, key) => {
    el.focus();
    for (const type of ['keydown', 'keypress', 'keyup']) {
      // keyCode/which given when the event is made, so the page's own code sees them too.
      el.dispatchEvent(new KeyboardEvent(type, {key, code: key, keyCode: CODES[key] || 0, which: CODES[key] || 0,
                                                bubbles: true, cancelable: true}));
    }
  };

  self.JF_RUN = async (ops) => {
    const failed = [];
    let focused = null;
    for (const op of ops) {
      try {
        if (op.op === 'wait') { await sleep(op.ms); continue; }
        if (op.op === 'insert') {
          (focused || document.activeElement || document.body).focus();
          document.execCommand('insertText', false, op.value);
          continue;
        }
        const el = find(op.sel);
        if (!el) throw new Error('box no longer on the page');
        if (op.op === 'fill') setValue(el, op.value);
        else if (op.op === 'select') choose(el, op.label);
        else if (op.op === 'check') {
          if (!el.checked) el.click();
          if (!el.checked) { el.checked = true; fire(el, 'input', 'change'); }
        } else if (op.op === 'click') { el.focus(); el.click(); focused = el; }  // only boxes, never buttons
        else if (op.op === 'files') attach(el, op.files);
        else if (op.op === 'press') press(el, op.key);
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
