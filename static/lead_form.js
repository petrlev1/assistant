/* lead_form.js - заявка с публичных страниц: открытие окна, проверка полей, отправка на /api/lead.
   Открывается любой кнопкой с data-lead-open (+ необязательный data-lead-source в письмо). */
(function () {
  var backdrop = document.getElementById('leadBackdrop');
  if (!backdrop) return;

  var form = document.getElementById('leadForm');
  var done = document.getElementById('leadDone');
  var status = document.getElementById('leadStatus');
  var sendBtn = document.getElementById('leadSend');
  var srcNote = document.getElementById('leadSrc');
  var source = '';
  var lastFocus = null;

  function el(id) { return document.getElementById(id); }

  function open(src) {
    source = src || '';
    srcNote.textContent = source ? 'Источник: ' + source : '';
    done.hidden = true;
    form.hidden = false;
    backdrop.hidden = false;
    document.body.classList.add('lead-open');
    lastFocus = document.activeElement;
    el('leadName').focus();
  }

  function close() {
    backdrop.hidden = true;
    document.body.classList.remove('lead-open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-lead-open]');
    if (trigger) { e.preventDefault(); open(trigger.getAttribute('data-lead-source') || ''); }
  });
  el('leadClose').addEventListener('click', close);
  el('leadDoneClose').addEventListener('click', close);
  backdrop.addEventListener('mousedown', function (e) { if (e.target === backdrop) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !backdrop.hidden) close(); });

  function value(id) { return el(id).value.trim(); }
  function err(text) { status.textContent = text; status.classList.add('err'); }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    status.classList.remove('err');
    status.textContent = '';

    var name = value('leadName'), phone = value('leadPhone'), email = value('leadEmail');
    var messenger = value('leadMessenger'), message = value('leadMessage');
    // Те же правила, что и на сервере: имя, сообщение и хотя бы один контакт
    if (name.length < 2) { err('Укажите имя'); el('leadName').focus(); return; }
    if (message.length < 5) { err('Напишите сообщение — хотя бы несколько слов'); el('leadMessage').focus(); return; }
    if (!phone && !email && !messenger) {
      err('Оставьте хотя бы один контакт: телефон, e-mail или мессенджер');
      el('leadPhone').focus(); return;
    }
    if (email && !/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(email)) { err('E-mail: проверьте адрес'); el('leadEmail').focus(); return; }
    if (phone && (phone.replace(/\D/g, '').length < 5)) { err('Телефон: нужно не меньше 5 цифр'); el('leadPhone').focus(); return; }

    sendBtn.disabled = true;
    status.textContent = 'Отправляем…';
    fetch('/api/lead', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: name, phone: phone, email: email, messenger: messenger, message: message,
        source: source, page: location.pathname, company: value('leadCompany')
      })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    }).then(function (res) {
      if (res.ok && res.data.success) {
        form.reset();
        form.hidden = true;
        done.hidden = false;
        status.textContent = '';
        if (window.ragstoneGoal) { window.ragstoneGoal('lead'); }
      } else {
        err((res.data && res.data.error) || 'Не удалось отправить заявку. Попробуйте позже.');
      }
    }).catch(function () {
      err('Ошибка сети. Проверьте соединение и попробуйте ещё раз.');
    }).finally(function () {
      sendBtn.disabled = false;
    });
  });
})();
