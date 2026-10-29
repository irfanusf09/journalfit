// A small stand-in for a journal submission site: each step checks its required boxes when
// Next is pressed, shows an error like real sites do, and otherwise moves to the next page.
function mark(key) { localStorage.setItem(key, (localStorage.getItem(key) || 0) * 1 + 1); }
function next(page, check) {
  const err = check ? check() : '';
  if (err) { document.getElementById('err').textContent = err; return; }
  // Remember what was entered, so the test can check it at the end.
  document.querySelectorAll('input, textarea, select').forEach(e => {
    if (!e.name) return;
    if (e.type === 'radio' && !e.checked) return;
    const v = e.type === 'checkbox' ? (e.checked ? 'on' : '') : e.type === 'file' ? [...e.files].map(f => f.name).join(',') : e.value;
    localStorage.setItem('v:' + e.name, v);
  });
  location.href = page;
}
function required(names) {
  for (const n of names) {
    const els = document.getElementsByName(n);
    if (!els.length) return 'Missing field ' + n;
    const e = els[0];
    if (e.type === 'radio' ? ![...els].some(x => x.checked) : e.type === 'checkbox' ? !e.checked : !e.value.trim())
      return 'Please complete: ' + n;
  }
  return '';
}
