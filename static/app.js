/* progressive enhancement only — the app must work without JS */
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(function () {
    if (!btn) return;
    var old = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(function () { btn.textContent = old; }, 1200);
  });
}

document.addEventListener('click', function (e) {
  var t = e.target.closest('[data-copy]');
  if (t) { copyText(t.getAttribute('data-copy'), t); }
});
