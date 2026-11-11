// Ранняя установка темы (до отрисовки — без мигания). Отдельным файлом, а не встроенным в страницу скриптом:
// политика безопасности страницы (CSP) разрешает только скрипты с самого сервера.
(function () {
  try {
    var t = localStorage.getItem('ma-theme');
    if (t) document.documentElement.setAttribute('data-theme', t);
    else if (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches)
      document.documentElement.setAttribute('data-theme', 'dark');
  } catch (e) { /* хранилище недоступно — тема по умолчанию */ }
})();
