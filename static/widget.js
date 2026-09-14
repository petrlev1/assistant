/*
 * RAGSTONE — встраиваемый чат-виджет.
 * Установка на сайт (перед </body>):
 *   <script src="https://ragstone.ru/widget.js" data-key="ВАШ_КЛЮЧ" async></script>
 * Параметры: data-color="#667eea" (цвет кнопки), data-title="Ассистент"
 * Работает без зависимостей; тень DOM защищает от стилей сайта-хоста.
 */
(function () {
  'use strict';
  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName('script');
    for (var i = all.length - 1; i >= 0; i--) {
      if (all[i].src && all[i].src.indexOf('/widget.js') !== -1) return all[i];
    }
    return null;
  })();
  if (!script) return;

  var KEY = script.getAttribute('data-key');
  if (!KEY) { console.error('RAGSTONE widget: не указан data-key'); return; }

  var BASE = script.src.replace(/\/widget\.js(\?.*)?$/, '');   // напр. https://ragstone.ru
  var COLOR = script.getAttribute('data-color') || '#667eea';
  var TITLE = script.getAttribute('data-title') || 'Ассистент';
  var ORIGIN = (function () { try { return location.origin; } catch (e) { return ''; } })();

  var root = document.createElement('div');
  root.setAttribute('style', 'all:initial;position:fixed;z-index:2147483646;top:0;left:0;width:0;height:0;');
  var hasShadow = !!root.attachShadow;
  var shadow = hasShadow ? root.attachShadow({ mode: 'open' }) : null;
  var mount = shadow || root;

  var style = document.createElement('style');
  style.textContent =
    '.rs-bubble{position:fixed;right:22px;bottom:22px;width:62px;height:62px;border-radius:50%;' +
    'background:' + COLOR + ';color:#fff;border:none;cursor:pointer;' +
    'box-shadow:0 6px 24px rgba(0,0,0,.28);display:flex;align-items:center;justify-content:center;' +
    'transition:transform .18s ease;z-index:2147483647;font-size:26px;line-height:1;padding:0}' +
    '.rs-bubble:hover{transform:scale(1.07)}' +
    '.rs-frame{position:fixed;right:22px;bottom:98px;width:384px;height:592px;max-width:calc(100vw - 24px);' +
    'max-height:calc(100vh - 130px);border:none;border-radius:16px;background:#fff;' +
    'box-shadow:0 12px 48px rgba(0,0,0,.32);display:none;z-index:2147483646}' +
    '.rs-frame.rs-open{display:block}' +
    '@media (max-width:480px){.rs-frame{right:8px;left:8px;bottom:86px;width:auto;height:calc(100vh - 120px)}}';
  mount.appendChild(style);

  var frame = document.createElement('iframe');
  frame.className = 'rs-frame';
  frame.setAttribute('allow', 'clipboard-write');
  frame.setAttribute('title', TITLE);

  var bubble = document.createElement('button');
  bubble.className = 'rs-bubble';
  bubble.setAttribute('title', TITLE);
  bubble.innerHTML = '<span style="pointer-events:none">\uD83D\uDCAC</span>'; // 💬

  mount.appendChild(frame);
  mount.appendChild(bubble);
  document.documentElement.appendChild(root);
  if (!hasShadow) mount.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646';

  var loaded = false;
  function open() {
    if (!loaded) {
      frame.src = BASE + '/widget/' + encodeURIComponent(KEY) + '?o=' + encodeURIComponent(ORIGIN);
      loaded = true;
    }
    frame.classList.add('rs-open');
    bubble.innerHTML = '<span style="pointer-events:none">\u2715</span>'; // ✕
  }
  function close() {
    frame.classList.remove('rs-open');
    bubble.innerHTML = '<span style="pointer-events:none">\uD83D\uDCAC</span>';
  }
  bubble.addEventListener('click', function () {
    frame.classList.contains('rs-open') ? close() : open();
  });

  // iframe просит закрыть себя (кнопка ✕ в шапке чата)
  window.addEventListener('message', function (e) {
    if (e.origin === BASE && e.data && e.data.ragstone === 'close') close();
  });

  // Карточка виджета: название и цвет из настроек владельца
  fetch(BASE + '/api/widget/meta?key=' + encodeURIComponent(KEY))
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (meta) {
      if (!meta) return;
      if (meta.name) { TITLE = meta.name; bubble.setAttribute('title', meta.name); }
      if (meta.color) bubble.style.background = meta.color;
    })
    .catch(function () {});
})();
