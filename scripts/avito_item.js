// Страница объявления Авито Работа → поля для `python -m snpr.avito add`.
// Выполняется во встроенном браузере Claude на открытом объявлении.
// Только читает текст: телефон не открывает, ничего не нажимает и не отправляет.
(() => {
  const t = s => ((document.querySelector(s) || {}).innerText || "").trim();
  const id = (t('[data-marker="item-view/item-id"]').match(/\d{6,}/) || [""])[0]
    || (location.pathname.match(/_(\d{6,})$/) || ["", ""])[1];
  return {
    id,
    url: location.origin + location.pathname,
    title: t("h1"),
    price: t('[data-marker="item-view/item-price-container"]') || t('[data-marker="item-view/item-price"]'),
    date: t('[data-marker="item-view/item-date"]'),
    conditions: t('[data-marker="item-view/item-params"]'),
    description: t('[data-marker="item-view/item-description"]'),
    location: t('[itemprop="address"]') || t('[data-marker="delivery/location"]') || t('[class*="style-item-address"]'),
    employer: t('[data-marker="seller-info/name"]'),
    blocked: /Доступ ограничен|Пройти проверку/.test(document.body.innerText.slice(0, 2000)),
  };
})()
