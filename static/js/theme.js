/* Shared, accessible colour-mode control. Dark is the first-visit default. */
(() => {
  const key = 'gc-colour-mode';
  let stored = null;
  try { stored = localStorage.getItem(key); } catch (_) {}
  const apply = mode => {
    const current = mode === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = current;
    document.documentElement.style.colorScheme = current;
    document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
      btn.setAttribute('aria-label', current === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
      btn.setAttribute('aria-pressed', current === 'light' ? 'true' : 'false');
      const label = btn.querySelector('[data-theme-label]');
      if (label) label.textContent = current === 'dark' ? 'Light mode' : 'Dark mode';
      const icon = btn.querySelector('[data-theme-icon]');
      if (icon) icon.textContent = current === 'dark' ? '☀' : '☾';
    });
  };
  apply(stored);
  document.addEventListener('DOMContentLoaded', () => apply(document.documentElement.dataset.theme));
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-theme-toggle]');
    if (!btn) return;
    const mode = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(key, mode); } catch (_) {}
    apply(mode);
  });
  window.addEventListener('storage', e => { if (e.key === key) apply(e.newValue); });
})();
