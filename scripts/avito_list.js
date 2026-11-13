// Выдача поиска Авито Работа → список карточек (для `python -m snpr.avito pick`).
// Выполняется во встроенном браузере Claude на открытой странице поиска.
// Только читает текст страницы: ничего не нажимает и не отправляет.
(() => {
  const q = (e, m) => ((e.querySelector('[data-marker="' + m + '"]') || {}).innerText || "").trim();
  return [...document.querySelectorAll('[data-marker="item"]')].map(e => {
    const a = e.querySelector('a[data-marker="item-title"]');
    return {
      id: e.getAttribute("data-item-id") || "",
      url: a ? a.href.split("?")[0] : "",
      title: q(e, "item-title"),
      price: q(e, "item-price"),
      params: q(e, "item-specific-params"),
      location: q(e, "item-location"),
    };
  }).filter(c => c.id && c.url);
})()
