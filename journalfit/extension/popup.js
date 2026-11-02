const $ = (id) => document.getElementById(id);
const send = (msg) => chrome.runtime.sendMessage(msg);
let tabId = null;
let savingAuto = false;

function showError(text) {
  $('error').textContent = text || '';
  $('error').classList.toggle('hidden', !text);
}

async function refresh() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab) return;
  tabId = tab.id;
  const s = await send({type: 'state', tabId});
  if (!s) return;
  // If Chrome is still running an older background service worker in memory, reload it automatically.
  if (s.bgVersion !== '0.3.2') {
    chrome.runtime.reload();
    return;
  }
  showError(s.error);
  $('onBox').classList.toggle('hidden', !s.tab);
  $('offBox').classList.toggle('hidden', !!s.tab || !s.packages.length);
  if (!s.error && !s.packages.length) showError('No prepared submissions yet. In JournalFit, choose a journal and prepare its package first.');
  if (s.tab) {
    $('journal').textContent = s.tab.journal || '';
    if (s.tab.autopilot === undefined && !savingAuto) {
      savingAuto = true;
      await send({type: 'set-autopilot', tabId, autopilot: true});
      savingAuto = false;
      s.tab.autopilot = true;
    }
    if ($('autoOn') && !savingAuto) $('autoOn').checked = !!s.tab.autopilot;
    const sc = s.tab.score;
    $('score').textContent = sc && sc.percent !== null && sc.percent !== undefined ?
      `So far: ${sc.filled} box(es) filled by JournalFit, ${sc.left_for_you} left for you (${sc.percent}% done for you).` : '';
    const add = (list, text) => { const li = document.createElement('li'); li.textContent = text; $(list).appendChild(li); };
    $('log').innerHTML = '';
    (s.tab.log || []).forEach((l) => add('log', l));
    $('yours').innerHTML = '';
    (s.tab.yours || []).forEach((l) => add('yours', l));
    $('yoursBox').classList.toggle('hidden', !(s.tab.yours || []).length);
  } else if (s.packages.length && !$('pkg').options.length) {
    for (const p of s.packages) {
      const o = new Option(`${p.journal}  (${p.paper})`, p.package);
      o.dataset.journal = p.journal;
      $('pkg').appendChild(o);
    }
    if (s.current) $('pkg').value = s.current;
  }
}

$('on').onclick = async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const origin = new URL(tab.url).origin + '/*';
  // Sites outside the usual submission systems need the author's permission once.
  if (!(await chrome.permissions.contains({origins: [origin]}))) {
    if (!(await chrome.permissions.request({origins: [origin]}))) return showError('JournalFit needs permission to fill forms on this site.');
  }
  const o = $('pkg').selectedOptions[0];
  const autopilot = $('autoStart') ? $('autoStart').checked : true;
  savingAuto = true;
  await send({type: 'switch-on', tabId, package: o.value, journal: o.dataset.journal, autopilot});
  savingAuto = false;
  refresh();
};
if ($('autoOn')) {
  $('autoOn').onchange = async () => {
    savingAuto = true;
    const want = $('autoOn').checked;
    try {
      await send({type: 'set-autopilot', tabId, autopilot: want});
    } finally {
      savingAuto = false;
    }
    refresh();
  };
}
$('off').onclick = async () => { await send({type: 'switch-off', tabId}); $('pkg').innerHTML = ''; refresh(); };
$('fill').onclick = () => send({type: 'fill-now', tabId});
$('over').onclick = () => send({type: 'start-over', tabId});

refresh();
setInterval(refresh, 1500);
