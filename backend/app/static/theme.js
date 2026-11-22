(() => {
  const storageKey = 'hydroplon-theme';
  const system = matchMedia('(prefers-color-scheme: dark)');
  const root = document.documentElement;
  let preference;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === 'dark' || saved === 'light') preference = saved;
  } catch {
    // The system preference also works when browser storage is unavailable.
  }

  let button;
  function applyTheme() {
    const theme = preference ?? (system.matches ? 'dark' : 'light');
    root.dataset.theme = theme;
    button?.setAttribute('aria-pressed', String(theme === 'dark'));
  }

  applyTheme();
  system.addEventListener('change', applyTheme);
  document.addEventListener('DOMContentLoaded', () => {
    button = document.getElementById('theme-toggle');
    applyTheme();
    button.hidden = false;
    button.addEventListener('click', () => {
      preference = root.dataset.theme === 'dark' ? 'light' : 'dark';
      applyTheme();
      try { localStorage.setItem(storageKey, preference); } catch {}
    });
  });
})();
