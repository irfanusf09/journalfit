// JournalFit extension: fills journal submission forms in the author's own Chrome.
//
// The author switches it on for one tab and picks the prepared submission. On each new form
// page, the boxes of every frame are read (generated.js), sent to the JournalFit app running
// on this Mac, and the app's answer (what to type, choose, tick and attach) is applied
// (page.js). Buttons are never pressed.

const PORTS = Array.from({length: 20}, (_, i) => 8765 + i);
let app = null;  // {base, token}

// Loaded from the JournalFit folder (Developer mode): when JournalFit has been updated, load the
// new files instead of running the old copy until the author reloads it by hand.
async function updateIfNewer(version) {
  if (!version || version === chrome.runtime.getManifest().version) return;
  const self = await chrome.management.getSelf();
  const {tried} = await chrome.storage.session.get('tried');
  if (self.installType === 'development' && tried !== version) {
    await chrome.storage.session.set({tried: version});
    chrome.runtime.reload();
  }
}

async function connect() {
  if (app) return app;
  for (const port of PORTS) {
    const base = `http://127.0.0.1:${port}`;
    try {
      const r = await fetch(`${base}/api/ext/hello`, {signal: AbortSignal.timeout(1500)});
      const j = await r.json();
      if (j.app === 'JournalFit' && j.token) {
        await updateIfNewer(j.extension_version);
        return (app = {base, token: j.token});
      }
    } catch (e) { /* not on this port */ }
  }
  throw new Error('JournalFit is not running. Double-click the JournalFit icon on your Desktop.');
}

async function api(path, body) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const {base, token} = await connect();
    let r;
    try {
      r = await fetch(base + path, body === undefined ? {headers: {'X-Token': token}} :
        {method: 'POST', headers: {'X-Token': token, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    } catch (e) { app = null; continue; }  // the app was restarted: find it again
    if (r.status === 403) { app = null; continue; }  // new pairing code after a restart
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    return j;
  }
  app = null;
  throw new Error('JournalFit is not running. Double-click the JournalFit icon on your Desktop.');
}

// Tabs switched on: {tabId: {package, journal, log, score, needed, lastSig, pending}}, kept while Chrome is open.
async function tabs() { return (await chrome.storage.session.get('tabs')).tabs || {}; }
async function saveTab(tabId, st) {
  const all = await tabs();
  if (st) all[tabId] = st; else delete all[tabId];
  await chrome.storage.session.set({tabs: all});
}

async function inject(tabId, frameIds) {
  const target = frameIds ? {tabId, frameIds} : {tabId, allFrames: true};
  try {
    await chrome.scripting.executeScript({target, files: ['generated.js', 'page.js']});
    await chrome.scripting.executeScript({target, func: () => self.JF_WATCH()});
  } catch (e) { /* a frame we may not read (another site, or a Chrome page) */ }
}

const busy = {};
const again = {};

async function fill(tabId, manual) {
  if (busy[tabId]) { again[tabId] = again[tabId] || manual || 'auto'; return; }
  busy[tabId] = true;
  try {
    const st = (await tabs())[tabId];
    if (!st) return;
    await inject(tabId);
    // Read every frame in order; numbering continues from one frame to the next, as the app expects.
    const frames = (await chrome.webNavigation.getAllFrames({tabId})) || [];
    frames.sort((a, b) => (a.frameId === 0 ? -1 : b.frameId === 0 ? 1 : a.frameId - b.frameId));
    const snap = [];
    let start = 0;
    for (const f of frames) {
      try {
        const [res] = await chrome.scripting.executeScript({
          target: {tabId, frameIds: [f.frameId]},
          func: (s) => self.JF_COLLECT ? {fields: self.JF_COLLECT(s), sig: self.JF_SIGNATURE(), url: location.href,
                                          text: (document.body ? document.body.innerText : '').slice(0, 30000)} : null,
          args: [start],
        });
        if (!res || !res.result) continue;
        snap.push({frameId: f.frameId, start, ...res.result});
        start += res.result.fields.length + 1;
      } catch (e) { /* frame we may not read */ }
    }
    if (!snap.length) return;
    const sig = snap.map((s) => s.sig).join('|');
    if (!manual && sig === st.lastSig && !st.pending) return;  // this page was filled already
    const tab = await chrome.tabs.get(tabId);
    const res = await api('/api/ext/fill', {package: st.package, url: tab.url, frames: snap});
    const failed = [];
    for (const fr of res.frames) {
      try {
        const [out] = await chrome.scripting.executeScript({
          target: {tabId, frameIds: [fr.frameId]}, func: (ops) => self.JF_RUN(ops), args: [fr.ops]});
        failed.push(...((out && out.result) || []));
      } catch (e) {
        failed.push(...fr.ops.map((op) => ({paths: op.paths || [], error: String(e.message || e)})));
      }
    }
    const log = [...res.log];
    if (failed.length) {
      await api('/api/ext/failed', {package: st.package, paths: failed.flatMap((f) => f.paths),
                                    errors: failed.map((f) => `${f.op || ''} ${f.sel || ''}: ${f.error}`)});
      log.push(`${failed.length} step(s) could not be done on this page; please check it.`);
    }
    Object.assign(st, {lastSig: sig, pending: res.pending_uploads, log, needed: res.needed, yours: res.yours, score: res.score,
                       filledAt: Date.now()});
    await saveTab(tabId, st);
    chrome.action.setBadgeText({tabId, text: res.filled ? String(res.filled) : ''});
    chrome.action.setBadgeBackgroundColor({tabId, color: '#2e7d32'});
    // Upload pages take one file at a time: come back once the site has taken the last one.
    if (res.pending_uploads) setTimeout(() => fill(tabId, false), 7000);
  } catch (e) {
    const st = (await tabs())[tabId];
    if (st) { st.log = [String(e.message || e)]; await saveTab(tabId, st); }
  } finally {
    busy[tabId] = false;
    if (again[tabId]) { const m = again[tabId] === true; again[tabId] = false; fill(tabId, m); }
  }
}

const timers = {};
function soon(tabId, ms = 700) {
  clearTimeout(timers[tabId]);
  timers[tabId] = setTimeout(() => fill(tabId, false), ms);
}

// A new page or frame finished loading in a switched-on tab: set it up, then fill once it settles.
chrome.webNavigation.onCompleted.addListener(async ({tabId, frameId}) => {
  if (!(await tabs())[tabId]) return;
  await inject(tabId, [frameId]);
  soon(tabId, 1500);
});

chrome.tabs.onRemoved.addListener((tabId) => saveTab(tabId, null));

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === 'page-changed') {
      if (sender.tab && (await tabs())[sender.tab.id]) soon(sender.tab.id);
      return {};
    }
    if (msg.type === 'state') {
      let packages = [], current = '', error = '';
      try { ({packages, current} = await api('/api/ext/packages')); } catch (e) { error = String(e.message || e); }
      return {packages, current, error, tab: (await tabs())[msg.tabId] || null};
    }
    if (msg.type === 'switch-on') {
      await saveTab(msg.tabId, {package: msg.package, journal: msg.journal, log: ['Switched on. Reading this page...']});
      fill(msg.tabId, true);
      return {};
    }
    if (msg.type === 'switch-off') { await saveTab(msg.tabId, null); chrome.action.setBadgeText({tabId: msg.tabId, text: ''}); return {}; }
    if (msg.type === 'fill-now') { fill(msg.tabId, true); return {}; }
    if (msg.type === 'start-over') {
      const st = (await tabs())[msg.tabId];
      if (st) {
        await api('/api/ext/reset', {package: st.package});
        Object.assign(st, {lastSig: '', pending: false, log: ['Started over: every file will be uploaded again.']});
        await saveTab(msg.tabId, st);
        fill(msg.tabId, true);
      }
      return {};
    }
    return {};
  })().then(reply, (e) => reply({error: String(e.message || e)}));
  return true;  // answer comes asynchronously
});
