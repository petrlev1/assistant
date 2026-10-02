# test_widget_frame_js.py — лента виджета не задваивает реплики
"""Проверка клиента виджета (templates/widget_frame.html) на дубли в ленте.

Зачем: гостю вопрос и ответ рисуются сразу, а лента ещё и опрашивается раз в 5 с
(`/api/widget/history?...&after_id=<id>`). Если опрос считает уже нарисованное
«новым», каждая пара «вопрос-ответ» появляется в ленте второй раз — ровно то, что
видно на скриншоте из отчёта. Тест поднимает JS виджета в node с крошечной
заглушкой DOM и моком API и считает пузыри ленты.

Запуск:  venv/Scripts/python.exe tests/test_widget_frame_js.py      (с сервера: venv/bin/python ...)
Проверить другую версию шаблона:  python tests/test_widget_frame_js.py путь/к/widget_frame.html
Без node тест пропускается (как tests/test_index_js.py).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT / 'templates' / 'widget_frame.html'

GREETING = 'Привет, гость! Задай мне вопрос по базе знаний.'

# JS-стенд: заглушка DOM + мок /api/widget/*, клиент берётся настоящий (из шаблона)
RUNNER = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');

function makeEl(tag) {
  const el = {
    tagName: tag, className: '', style: {}, children: [], handlers: {}, attrs: {},
    value: '', disabled: false, scrollTop: 0, scrollHeight: 0, _text: '', _parent: null,
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
    appendChild(c) { c._parent = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c._parent = this;
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
      return c;
    },
    remove() {
      if (!this._parent) return;
      const i = this._parent.children.indexOf(this);
      if (i >= 0) this._parent.children.splice(i, 1);
    },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); },
    click() { (this.handlers.click || []).forEach(fn => fn({})); },
    focus() {},
    querySelector(sel) {
      const want = sel.split('.').filter(Boolean);
      const walk = (nodes) => {
        for (const n of nodes) {
          if (want.every(c => String(n.className || '').split(/\s+/).includes(c))) return n;
          const r = walk(n.children); if (r) return r;
        }
        return null;
      };
      return walk(this.children);
    },
  };
  return el;
}

const byId = {};
['chat', 'q', 'send', 'dots', 'newBtn', 'clearBtn', 'closeBtn'].forEach(id => { byId[id] = makeEl('div'); });
const greeting = makeEl('div');
greeting.className = 'msg bot';
greeting._text = 'GREETING_TEXT';
byId.chat.appendChild(greeting);
byId.chat.appendChild(byId.dots);          // add() вставляет пузыри перед индикатором
const sublineEl = makeEl('span');

const timers = [];
const store = [];
let next = 1;
const ANSWER = 'Добрый день! Как я могу помочь вам сегодня?';

function json(o) { return Promise.resolve({ ok: true, json: () => Promise.resolve(o) }); }

function mockFetch(url, opts) {
  const u = String(url);
  if (u.indexOf('/api/widget/history') === 0) {
    const qs = new URLSearchParams(u.split('?')[1] || '');
    const after = qs.get('after_id');
    const out = store.filter(m => (after === null ? true : m.id > Number(after)))
      .map(m => ({ id: m.id, role: m.role, message: m.message, ts: 1 }));
    return json({ messages: out, session_started_at: 0, human: false });
  }
  if (u.indexOf('/api/widget/ask') === 0) {
    const body = JSON.parse((opts && opts.body) || '{}');
    store.push({ id: next++, role: 'user', message: body.question });
    const aid = next++;
    const extra = sandbox.__OP_REPLY;
    return new Promise(res => setTimeout(() => {
      store.push({ id: aid, role: 'assistant', message: ANSWER });
      if (extra) store.push({ id: next++, role: 'operator', message: extra });
      res({ ok: true, json: () => Promise.resolve({ answer: ANSWER, last_id: aid }) });
    }, 20));
  }
  return json({});
}

const sandbox = {
  document: {
    hidden: false,
    getElementById: id => byId[id] || (byId[id] = makeEl('div')),
    createElement: makeEl,
    createTextNode: t => ({ textContent: String(t) }),
    querySelector: sel => (sel === '.hd .ttl span' ? sublineEl : null),
    addEventListener: () => {},
  },
  localStorage: { store: {}, getItem(k) { return this.store[k] || null; }, setItem(k, v) { this.store[k] = v; } },
  fetch: (u, o) => mockFetch(u, o),
  setInterval: fn => { timers.push(fn); return timers.length; },
  parent: { postMessage: () => {} },
  confirm: () => false,
  console: console, JSON: JSON, Math: Math, Number: Number, String: String,
  URLSearchParams: URLSearchParams, setTimeout: setTimeout, Promise: Promise, RegExp: RegExp,
  window: null,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const tick = ms => new Promise(r => setTimeout(r, ms));
const bubbles = () => byId.chat.children
  .filter(c => String(c.className).indexOf('msg') === 0)
  .map(c => String(c.className).replace('msg ', '') + ' | ' + c.textContent);
const poll = () => timers.forEach(fn => fn());        // «прошло 5 секунд»

(async () => {
  await tick(30);                                     // история гостя пуста — остаётся приветствие
  const afterLoad = bubbles();

  byId.q.value = 'Добрый день';
  byId.send.click();
  await tick(60);
  const afterAsk = bubbles();

  poll();                                             // штатный опрос ленты
  await tick(30);
  const afterPoll = bubbles();

  poll();                                             // два опроса подряд (гонка interval/visibilitychange)
  poll();
  await tick(30);
  const afterDoublePoll = bubbles();

  sandbox.__OP_REPLY = 'Соединяю с менеджером';       // реплика менеджера приходит опросом
  byId.q.value = 'И ещё вопрос';
  byId.send.click();
  await tick(60);
  poll();
  await tick(30);
  const afterOperator = bubbles();

  console.log('__RESULT__' + JSON.stringify({
    afterLoad, afterAsk, afterPoll, afterDoublePoll, afterOperator,
    db: store.map(m => m.id + ':' + m.role),
  }));
})();
"""


def render_client(template_text):
    """Клиентский JS из Jinja-шаблона: подстановки и мок API внутри самого шаблона."""
    blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', template_text, re.S)
    assert blocks, 'в шаблоне виджета не найдено встроенных скриптов'
    js = '\n'.join(blocks)
    js = js.replace('{{ key|tojson }}', json.dumps('TESTKEY'))
    js = js.replace('{{ site|tojson }}', json.dumps(''))
    js = js.replace('{{ greeting|tojson }}', json.dumps(GREETING))
    return js


class WidgetFrameJsCase(unittest.TestCase):
    def _run(self, template: Path):
        node = shutil.which('node')
        if not node:
            self.skipTest('node не установлен — проверка JS виджета пропущена')
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / 'widget_frame.js'
            runner_path = Path(tmp) / 'runner.js'
            js_path.write_text(render_client(template.read_text(encoding='utf-8')), encoding='utf-8')
            runner_path.write_text(RUNNER.replace('GREETING_TEXT', GREETING), encoding='utf-8')
            res = subprocess.run([node, str(runner_path), str(js_path)],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(res.returncode, 0, f'стенд виджета не отработал:\n{res.stderr[:2000]}')
        for line in res.stdout.splitlines():
            if line.startswith('__RESULT__'):
                return json.loads(line[len('__RESULT__'):])
        self.fail(f'стенд не вернул результат:\n{res.stdout[:1000]}\n{res.stderr[:1000]}')

    def test_feed_has_no_duplicates(self):
        r = self._run(TEMPLATE)
        answer = 'bot | Добрый день! Как я могу помочь вам сегодня?'

        self.assertEqual(len(r['afterLoad']), 1, r['afterLoad'])          # только приветствие
        self.assertEqual(r['afterAsk'],
                         ['bot | ' + GREETING, 'user | Добрый день', answer], r['afterAsk'])
        # Главный инвариант: опрос ленты НЕ повторяет то, что уже нарисовано
        self.assertEqual(r['afterPoll'], r['afterAsk'], 'опрос ленты задвоил реплики')
        self.assertEqual(r['afterDoublePoll'], r['afterAsk'], 'двойной опрос задвоил реплики')
        # …но новые реплики (ответ менеджера) опрос по-прежнему доносит — ровно раз
        self.assertEqual(r['afterOperator'],
                         r['afterAsk'] + ['user | И ещё вопрос', answer,
                                          'op | МенеджерСоединяю с менеджером'],
                         r['afterOperator'])

    def test_message_sent_once_to_backend(self):
        """Дубль нельзя лечить повтором вопроса на сервере: в ленте БД всё по одному разу."""
        r = self._run(TEMPLATE)
        self.assertEqual(r['db'], ['1:user', '2:assistant', '3:user', '4:assistant', '5:operator'],
                         r['db'])


if __name__ == '__main__':
    # Необязательный аргумент — путь к проверяемому шаблону (по умолчанию рабочий)
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        TEMPLATE = Path(sys.argv[1])
        sys.argv = [sys.argv[0]]
    unittest.main(verbosity=2)
