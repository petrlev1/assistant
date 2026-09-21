# test_cli_launcher.py - проверки CLI-лаунчера (run_cli.py)
#
#   1) самопочинка интерпретатора: CLI, запущенный питоном без psycopg2, перезапускается
#      через venv; если в venv psycopg2 тоже нет или питон тот же — не перезапускается;
#   2) _db_settings_or_none(): при недоступной БД возвращает None (и НЕ подставляет дефолты,
#      которые нельзя записывать в app_settings), при живой БД — значения + дефолты для
#      отсутствующих ключей;
#   3) main() первым делом зовёт самопочинку (иначе «танца с интерпретатором» не будет).
#
# Запуск из папки проекта:  venv/Scripts/python.exe test_cli_launcher.py
import sys
import types
from types import SimpleNamespace

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import run_cli

PASSED, FAILED = [], []


def check(name, cond, extra=''):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f'  -> {extra!r}' if not cond and extra != '' else ''))


def _without_psycopg2():
    """Эмуляция «питон без psycopg2»: None в sys.modules → `import psycopg2` даёт ImportError."""
    saved = sys.modules.pop('psycopg2', None)
    sys.modules['psycopg2'] = None
    return saved


def _restore(saved):
    sys.modules.pop('psycopg2', None)
    if saved is not None:
        sys.modules['psycopg2'] = saved


def test_ensure_interpreter():
    print('\n1) _ensure_venv_interpreter: перезапуск через venv')
    venv_python = run_cli.os.path.join(run_cli.PROJECT_DIR, 'venv',
                                       'Scripts' if run_cli.os.name == 'nt' else 'bin',
                                       'python.exe' if run_cli.os.name == 'nt' else 'python')

    # 1a) psycopg2 есть — ничего не делаем
    sys.modules['psycopg2'] = types.ModuleType('psycopg2')
    calls = {'execv': 0}
    orig_execv, orig_run = run_cli.os.execv, run_cli.subprocess.run
    run_cli.os.execv = lambda *a, **kw: calls.__setitem__('execv', calls['execv'] + 1)
    try:
        run_cli._ensure_venv_interpreter()
    finally:
        run_cli.os.execv = orig_execv
    check('psycopg2 доступен → перезапуска нет', calls['execv'] == 0, calls)

    # Дальше эмулируем «мы НЕ venv-питон» подменой sys.executable: в тесте сам тест
    # запущен venv-питоном, и без этого проверка «мы уже venv» отсекала бы сценарии
    saved_executable = sys.executable
    sys.executable = 'C:\\not-a-venv\\python.exe'

    # 1b) psycopg2 нет и в venv его нет → не перезапускаемся
    sys.modules.pop('psycopg2', None)
    saved = _without_psycopg2()
    calls['execv'] = 0
    run_cli.os.execv = lambda *a, **kw: calls.__setitem__('execv', calls['execv'] + 1)
    run_cli.subprocess.run = lambda *a, **kw: SimpleNamespace(returncode=1)
    try:
        run_cli._ensure_venv_interpreter()
    finally:
        run_cli.os.execv = orig_execv
        run_cli.subprocess.run = orig_run
        _restore(saved)
    check('psycopg2 нужен и в venv не импортируется → перезапуска нет', calls['execv'] == 0, calls)

    # 1c) psycopg2 нет у текущего, но есть в venv → execv с venv-питоном и тем же скриптом
    sys.modules.pop('psycopg2', None)
    saved = _without_psycopg2()
    calls['execv'] = 0
    recorded = {}
    run_cli.os.execv = lambda path, argv: (calls.__setitem__('execv', calls['execv'] + 1),
                                           recorded.update(path=path, argv=list(argv)))
    run_cli.subprocess.run = lambda *a, **kw: SimpleNamespace(returncode=0)
    try:
        run_cli._ensure_venv_interpreter()
    finally:
        run_cli.os.execv = orig_execv
        run_cli.subprocess.run = orig_run
        _restore(saved)
    check('перезапуск через venv выполнен', calls['execv'] == 1, calls)
    check('цель перезапуска — venv-питон проекта', recorded.get('path') == venv_python, recorded.get('path'))
    check('первым аргументом идёт сам run_cli.py',
          (recorded.get('argv') or [None, None])[1] == run_cli.os.path.abspath(run_cli.__file__), recorded.get('argv'))
    check('аргументы командной строки сохранены',
          (recorded.get('argv') or [])[2:] == sys.argv[1:], recorded.get('argv'))

    # 1d) мы уже venv-питон → перезапуска не делаем даже если psycopg2 не импортируется (нет петли)
    sys.executable = saved_executable
    sys.modules.pop('psycopg2', None)
    saved = _without_psycopg2()
    calls['execv'] = 0
    run_cli.os.execv = lambda *a, **kw: calls.__setitem__('execv', calls['execv'] + 1)
    run_cli.subprocess.run = lambda *a, **kw: SimpleNamespace(returncode=0)
    try:
        run_cli._ensure_venv_interpreter()
    finally:
        run_cli.os.execv = orig_execv
        run_cli.subprocess.run = orig_run
        _restore(saved)
    check('запуск уже venv-питоном → перезапуска нет (нет петли)', calls['execv'] == 0, calls)


def test_db_settings_or_none():
    print('\n2) _db_settings_or_none: не подставлять дефолты вместо БД')
    import auth_db
    orig = auth_db.get_all_settings

    def boom():
        raise RuntimeError('connection refused')

    auth_db.get_all_settings = boom
    try:
        out = run_cli._db_settings_or_none()
    finally:
        auth_db.get_all_settings = orig
    check('БД недоступна → None (запись в app_settings не случится)', out is None, out)

    auth_db.get_all_settings = lambda: {}
    try:
        out = run_cli._db_settings_or_none()
    finally:
        auth_db.get_all_settings = orig
    check('пустая таблица настроек → None (не пишем дефолты)', out is None, out)

    auth_db.get_all_settings = lambda: {'search_top_k': 30, 'llm_model': 'qwen-turbo'}
    try:
        out = run_cli._db_settings_or_none()
    finally:
        auth_db.get_all_settings = orig
    check('живая БД → значения на месте', out and out.get('search_top_k') == 30 and out.get('llm_model') == 'qwen-turbo', out)
    check('недостающие ключи добираются дефолтами',
          out and out.get('search_alpha') == run_cli._DEFAULTS['search_alpha'], out)


def test_main_calls_bootstrap_first():
    print('\n3) main() первым делом вызывает самопочинку')
    order = []
    orig_boot, orig_run_command = run_cli._ensure_venv_interpreter, run_cli.run_command
    run_cli._ensure_venv_interpreter = lambda: order.append('bootstrap')
    run_cli.run_command = lambda args: (order.append('command'), 0)[1]
    try:
        rc = run_cli.main(['status'])
    finally:
        run_cli._ensure_venv_interpreter = orig_boot
        run_cli.run_command = orig_run_command
    check('самопочинка вызвана до выполнения команды', order == ['bootstrap', 'command'], order)
    check('main() вернул код 0', rc == 0, rc)


def main():
    test_ensure_interpreter()
    test_db_settings_or_none()
    test_main_calls_bootstrap_first()
    print('\n' + '=' * 60)
    print(f'Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}')
    for name in FAILED:
        print(f'  ❌ {name}')
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
