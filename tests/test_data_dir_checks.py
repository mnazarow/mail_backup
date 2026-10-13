"""Каталог данных, которого нет на месте: ссылка на неподключённый диск.

Так сорвалось обновление 10.10.2026: ``/var/lib/mailarchiver`` был ссылкой на
каталог другого диска, диск не был подключён, служба падала с «FileExistsError:
File exists», а ``check-config`` сообщал «Конфигурация корректна ✓».
"""
from __future__ import annotations

import errno
import os
import sys

import pytest

from mailarchiver import config as config_mod
from mailarchiver.__main__ import main
from mailarchiver.config import Config, load_config, os_error_text
from mailarchiver.errors import ConfigError


def _config(tmp_path, data_dir) -> str:
    path = tmp_path / "config.yaml"
    path.write_text('paths:\n  data_dir: "%s"\n' % data_dir, encoding="utf-8")
    return str(path)


def test_dangling_data_dir_is_named(tmp_path):
    data = tmp_path / "data"
    os.symlink(tmp_path / "storage" / "mailarchiver", data)       # диск «не подключён»
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    message = info.value.message
    assert "Каталог данных" in message and "ссылка на" in message and "storage/mailarchiver" in message
    assert "подключён" in (info.value.hint or "")
    assert os.path.islink(data) and not os.path.exists(tmp_path / "storage")     # ничего не создано


def test_data_dir_through_valid_link_works(tmp_path):
    (tmp_path / "storage" / "mailarchiver").mkdir(parents=True)
    data = tmp_path / "data"
    os.symlink(tmp_path / "storage" / "mailarchiver", data)
    cfg = load_config(_config(tmp_path, data))
    assert cfg.dir_problems() == []
    assert os.path.isdir(tmp_path / "storage" / "mailarchiver" / "mailboxes")


def test_data_dir_that_is_a_file(tmp_path):
    data = tmp_path / "data"
    data.write_text("x")
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    assert "не каталог" in info.value.message


def test_check_config_reports_dangling_data_dir(tmp_path, capsys):
    data = tmp_path / "data"
    os.symlink(tmp_path / "storage" / "mailarchiver", data)
    code = main(["-c", _config(tmp_path, data), "check-config"])
    out = capsys.readouterr()
    assert code == 1
    assert "ссылка на" in out.err and "подключён" in out.err
    assert "Конфигурация корректна" not in out.out
    assert not os.path.exists(tmp_path / "storage")                # проверка ничего не создаёт


def test_incident_link_to_unmounted_disk_at_service_start(tmp_path, capsys, monkeypatch):
    """Ровно тот случай: ``/var/lib/mailarchiver -> /storage/storage/mailarchiver/``, диск не подключён.

    Запуск службы (и ``import mailarchiver.web.app``) — это ``load_config(create_dirs=True)``: вместо
    «FileExistsError: [Errno 17] File exists» — ошибка конфигурации, где названы ссылка и её цель.
    """
    import importlib.util
    if importlib.util.find_spec("uvicorn"):
        import uvicorn

        def must_not_start(*args, **kwargs):                    # до запуска сервера дело дойти не должно
            raise AssertionError("служба запустилась с каталогом данных, которого нет")

        monkeypatch.setattr(uvicorn, "run", must_not_start)
    data = tmp_path / "var-lib-mailarchiver"
    target = str(tmp_path / "storage" / "storage" / "mailarchiver") + "/"       # как на том сервере — с «/» на конце
    os.symlink(target, data)
    config = _config(tmp_path, data)
    with pytest.raises(ConfigError) as info:
        load_config(config, create_dirs=True)
    exc = info.value
    assert not isinstance(exc, OSError) and isinstance(exc.__cause__, FileExistsError)
    assert exc.message == f"Каталог данных {data} — ссылка на {target}, а такого каталога сейчас нет."
    assert "диск" in exc.hint and "подключён" in exc.hint and "findmnt" in exc.hint
    # то же, когда каталоги готовит уже загруженная конфигурация (Services.setup)
    with pytest.raises(ConfigError, match="ссылка на"):
        load_config(config, create_dirs=False).ensure_dirs()
    # команды службы: строка об ошибке и подсказка, а не трассировка
    commands = [["replica-ssh-key"]] + ([["serve"]] if importlib.util.find_spec("uvicorn") else [])
    for command in commands:
        assert main(["-c", config, *command]) == 1, command
        out = capsys.readouterr()
        assert out.err.startswith(f"ОШИБКА: Каталог данных {data} — ссылка на {target}"), (command, out.err)
        assert "Подсказка:" in out.err and "подключён" in out.err
        assert "File exists" not in out.err and "Traceback" not in out.err
    # check-config (create_dirs=False) сообщает о том же и кончается ошибкой
    assert main(["-c", config, "check-config"]) == 1
    out = capsys.readouterr()
    assert f"ОШИБКА: Каталог данных {data} — ссылка на {target}, а такого каталога сейчас нет." in out.err
    assert "подключён" in out.err and "Конфигурация корректна" not in out.out
    assert os.path.islink(data) and os.readlink(data) == target and not os.path.exists(tmp_path / "storage")


def test_recovery_commands_name_the_unmounted_disk(tmp_path, capsys):
    """``restore-snapshot`` и ``replica-pull`` пишут в каталог данных сами — и отвечали «File exists»."""
    data = tmp_path / "data"
    os.symlink(tmp_path / "storage" / "mailarchiver", data)
    config = _config(tmp_path, data)
    (tmp_path / "copy").mkdir()
    for command in (["restore-snapshot", "mailarchiver-20261010-0300.db.gz"],
                    ["replica-pull", "--dir", str(tmp_path / "copy")]):
        assert main(["-c", config, *command]) == 1, command
        err = capsys.readouterr().err
        assert "Каталог данных" in err and "ссылка на" in err and "storage/mailarchiver" in err, (command, err)
        assert "подключён" in err and "повторите команду" in err and "File exists" not in err
    assert os.path.islink(data) and not os.path.exists(tmp_path / "storage")
    # скачать копию в другой каталог можно и без каталога данных: его проверка тут не нужна
    assert main(["-c", config, "replica-pull", "--dir", str(tmp_path / "copy"), "--to", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    assert "ссылка на" not in err and "нет копии" in err


def test_check_config_missing_data_dir_is_only_a_note(tmp_path, capsys):
    data = tmp_path / "data"
    code = main(["-c", _config(tmp_path, data), "check-config"])
    out = capsys.readouterr()
    assert code == 0 and "Конфигурация корректна" in out.out
    # не обещаем, что каталог «будет создан»: служба из unit-файла без него не запустится
    assert "Замечание: каталога данных" in out.out and "будет создан" not in out.out
    assert "не запустится" in out.out and "подключён" in out.out
    assert not data.exists()


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root входит в любой каталог")
def test_unreachable_data_dir(tmp_path, capsys):
    closed = tmp_path / "closed"
    (closed / "data").mkdir(parents=True)
    os.chmod(closed, 0o000)
    try:
        cfg = load_config(_config(tmp_path, closed / "data"), create_dirs=False)
        assert any("нет прав" in p for p in cfg.dir_problems())
        assert [p.kind for p in cfg.dir_problems()] == ["denied"]
        # чужому пользователю закрытый каталог — предупреждение, а не ошибка конфигурации
        assert main(["-c", _config(tmp_path, closed / "data"), "check-config"]) == 0
        out = capsys.readouterr()
        assert "Предупреждение:" in out.out and "нет прав" in out.out and out.err == ""
    finally:
        os.chmod(closed, 0o700)


# ---------------------------------------------------------------------------
#  Каталог ЗА ссылкой в никуда: «ещё нет — будет создан» было неправдой
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("below", ["mailarchiver", "srv/mail/archive"])
def test_data_dir_under_dangling_link_is_reported(tmp_path, capsys, below):
    """``/data -> /mnt/not-mounted``, каталог данных ``/data/mailarchiver``: диск не подключён."""
    link = tmp_path / "data"
    os.symlink(tmp_path / "mnt" / "not-mounted", link)
    data = link / below
    cfg = load_config(_config(tmp_path, data), create_dirs=False)
    problems = cfg.dir_problems()
    assert len(problems) == 1 and problems[0].kind == "dangling"          # причина одна — и названа один раз
    assert str(data) in problems[0] and f"за ссылкой {link}" in problems[0] and "mnt/not-mounted" in problems[0]

    assert main(["-c", _config(tmp_path, data), "check-config"]) == 1
    out = capsys.readouterr()
    assert "за ссылкой" in out.err and "подключён" in out.err
    assert "будет создан" not in out.out and "Конфигурация корректна" not in out.out

    with pytest.raises(ConfigError) as info:                               # так же отвечает и запуск службы
        load_config(_config(tmp_path, data))
    assert "за ссылкой" in info.value.message and "подключён" in info.value.hint
    assert os.path.islink(link) and not os.path.exists(tmp_path / "mnt")   # ничего не создано


def test_dangling_link_with_relative_target_and_healthy_neighbours(tmp_path):
    os.symlink("not-mounted", tmp_path / "data")                           # ссылка с относительным путём
    problem = Config.dir_problem(str(tmp_path / "data" / "ma"), "Каталог данных")
    assert problem.kind == "dangling" and "→ not-mounted" in problem
    # каталога просто ещё нет (а ссылки на пути целы) — это не беда: он будет создан
    (tmp_path / "disk").mkdir()
    os.symlink(tmp_path / "disk", tmp_path / "good")
    assert Config.dir_problem(str(tmp_path / "good" / "ma" / "deep"), "Каталог данных") is None
    assert Config.dir_problem(str(tmp_path / "nothing" / "here"), "Каталог данных") is None
    cfg = load_config(_config(tmp_path, tmp_path / "good" / "ma"))
    assert cfg.dir_problems() == [] and os.path.isdir(tmp_path / "disk" / "ma" / "mailboxes")
    assert os.stat(tmp_path / "disk" / "ma").st_mode & 0o777 == 0o700


# ---------------------------------------------------------------------------
#  Нет прав у того, кто запустил проверку, — не ошибка конфигурации
# ---------------------------------------------------------------------------
def _closed_to_checker(monkeypatch, *closed):
    """Тот, кто запустил проверку, не может войти в каталоги ``closed`` (тест идёт и от root)."""
    real = os.stat

    def stat(path, *args, **kwargs):
        text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
        if any(text.startswith(str(root) + os.sep) for root in closed):
            raise PermissionError(errno.EACCES, "Permission denied", text)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def test_check_config_by_another_user_is_a_warning(tmp_path, capsys, monkeypatch):
    """Каталог данных 0700 принадлежит службе, проверку запустил другой пользователь."""
    data = tmp_path / "data"
    load_config(_config(tmp_path, data))                                   # служба уже работала: каталоги есть
    monkeypatch.setattr(config_mod, "current_user", lambda: "ivanov")
    _closed_to_checker(monkeypatch, data)
    code = main(["-c", _config(tmp_path, data), "check-config"])
    out = capsys.readouterr()
    assert code == 0 and out.err == ""                                     # раньше: четыре ошибки и код 1
    warnings = [line for line in out.out.splitlines() if line.startswith("Предупреждение:")]
    assert len(warnings) == 1                                              # одна строка, а не по строке на каталог
    assert "нет прав" in warnings[0] and "«ivanov»" in warnings[0] and "права службы" in warnings[0]
    assert "ещё каталогов: 3" in warnings[0] and "sudo -u" in warnings[0] and "check-config" in warnings[0]
    assert "подключён" not in out.out and "Permission denied" not in out.out
    assert "будет создан" not in out.out
    assert "Конфигурация корректна ✓ (есть предупреждения)" in out.out


def test_check_config_when_data_dir_itself_is_out_of_reach(tmp_path, capsys, monkeypatch):
    """Закрыт путь к самому каталогу данных: не ошибка — и не «каталога ещё нет»."""
    data = tmp_path / "closed" / "data"
    load_config(_config(tmp_path, data))
    monkeypatch.setattr(config_mod, "current_user", lambda: "ivanov")
    _closed_to_checker(monkeypatch, tmp_path / "closed")
    code = main(["-c", _config(tmp_path, data), "check-config"])
    out = capsys.readouterr()
    assert code == 0 and out.err == ""
    assert out.out.count("Предупреждение:") == 1 and "Каталог данных" in out.out and "«ivanov»" in out.out
    assert "ещё каталогов" not in out.out and "будет создан" not in out.out


def test_check_config_names_the_owner_of_data_dir(tmp_path, capsys, monkeypatch):
    """Подсказка называет, от чьего имени проверять; владельцу каталога — что служба с ним не запустится."""
    import pwd
    data = tmp_path / "data"
    load_config(_config(tmp_path, data))
    owner = pwd.getpwuid(os.stat(data).st_uid).pw_name
    _closed_to_checker(monkeypatch, data)
    monkeypatch.setattr(config_mod, "current_user", lambda: "ivanov")
    assert main(["-c", _config(tmp_path, data), "check-config"]) == 0
    out = capsys.readouterr().out
    assert f"sudo -u {owner} mailarchiver -c " in out and f"принадлежит пользователю «{owner}»" in out
    monkeypatch.setattr(config_mod, "current_user", lambda: owner)
    assert main(["-c", _config(tmp_path, data), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "владелец каталога данных" in out and "не запустится" in out and "sudo chown -R" in out


def test_service_itself_without_access_is_an_error_with_fitting_hint(tmp_path, monkeypatch):
    """Самой службе закрытый каталог — ошибка запуска; совет — про права, а не про диск."""
    data = tmp_path / "data"
    load_config(_config(tmp_path, data))
    _closed_to_checker(monkeypatch, data)
    monkeypatch.setattr(os.path, "isdir", lambda path: False if str(path).startswith(str(data) + os.sep)
                        else os.path.exists(path))

    def denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(os, "makedirs", denied)
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    assert "Каталог журналов" in info.value.message and "нет прав" in info.value.message
    assert "namei -l" in info.value.hint and "chown" in info.value.hint and "подключён" not in info.value.hint


# ---------------------------------------------------------------------------
#  Причина — по-русски, совет — по причине
# ---------------------------------------------------------------------------
def test_file_on_the_way_is_named_in_russian(tmp_path, capsys):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    data = blocker / "sub" / "data"
    problem = Config.dir_problem(str(data), "Каталог данных")
    assert problem.kind == "not_dir" and f"{blocker} — файл, а не каталог" in problem
    assert "Not a directory" not in problem
    assert main(["-c", _config(tmp_path, data), "check-config"]) == 1
    err = capsys.readouterr().err
    assert "файл, а не каталог" in err and "Not a directory" not in err
    assert "Подсказка:" in err and "подключён" not in err and "уберите" in err        # диск тут ни при чём
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    assert "файл, а не каталог" in info.value.message and "подключён" not in info.value.hint


def test_link_loop_is_named_in_russian(tmp_path, capsys):
    os.symlink("b", tmp_path / "a")
    os.symlink("a", tmp_path / "b")
    problem = Config.dir_problem(str(tmp_path / "a"), "Каталог данных")
    assert problem.kind == "loop" and "ссылки указывают друг на друга" in problem
    assert "Too many levels" not in problem
    assert main(["-c", _config(tmp_path, tmp_path / "a"), "check-config"]) == 1
    err = capsys.readouterr().err
    assert "ссылки указывают друг на друга" in err and "Too many" not in err and "ls -l" in err
    assert "подключён" not in err


def test_os_error_text_is_russian_for_common_errors():
    texts = {code: os_error_text(OSError(code, os.strerror(code))) for code in
             (errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS, errno.ENOSPC)}
    assert texts[errno.EACCES] == "нет прав" and "только для чтения" in texts[errno.EROFS]
    assert "нет места" in texts[errno.ENOSPC] and "нет такого файла" in texts[errno.ENOENT]
    for code, text in texts.items():
        assert text != os.strerror(code) and all(ord(ch) > 127 or not ch.isalpha() for ch in text), (code, text)
    # редкая ошибка — как назвала её система, но не пустая строка
    assert os_error_text(OSError(errno.EIO, os.strerror(errno.EIO))) == os.strerror(errno.EIO)
    assert os_error_text(OSError("нечто")) == "нечто" and os_error_text(OSError()) == "OSError"


@pytest.mark.parametrize("code, text, hint", [(errno.EROFS, "только для чтения", "dmesg"),
                                              (errno.ENOSPC, "нет места", "df -h"),
                                              (errno.EACCES, "нет прав", "namei -l")])
def test_directory_that_cannot_be_created(tmp_path, monkeypatch, code, text, hint):
    """Каталога нет и создать его не вышло: причина по-русски, совет под неё."""
    data = tmp_path / "data"

    def failing(path, *args, **kwargs):
        raise OSError(code, os.strerror(code), str(path))

    monkeypatch.setattr(os, "makedirs", failing)
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    message = info.value.message
    assert f"Не удалось создать каталог данных {data}: " in message and text in message
    assert os.strerror(code) not in message
    assert hint in info.value.hint and "подключён" not in info.value.hint


def test_permission_hint_names_a_directory_that_exists(tmp_path, monkeypatch):
    """Совет про права — выполнимая команда: ``chown`` каталога, которого ещё нет, не сработал бы."""
    import shlex
    data = tmp_path / "mail archive"                            # пробел в пути — в командах он в кавычках
    quoted = shlex.quote(str(data))

    def denied(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(os, "makedirs", denied)
    monkeypatch.setattr(config_mod, "current_user", lambda: "mailarchiver")
    # каталога данных нет, а создать его служба не вправе (каталог над ним принадлежит root)
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    assert f"Не удалось создать каталог данных {data}: нет прав." == info.value.message
    assert f"sudo mkdir -p {quoted} && sudo chown mailarchiver: {quoted}" in info.value.hint
    assert "chown -R" not in info.value.hint and f"namei -l {quoted}" in info.value.hint
    # каталог данных есть, но чужой (создан командой от root): отдать службе нужно его, а не каталог в нём
    data.mkdir()
    with pytest.raises(ConfigError) as info:
        load_config(_config(tmp_path, data))
    assert f"Не удалось создать каталог журналов {data / 'logs'}: нет прав." == info.value.message
    assert f"sudo chown -R mailarchiver: {quoted}" in info.value.hint and f"namei -l {quoted}" in info.value.hint
    assert "logs" not in info.value.hint and "mkdir" not in info.value.hint
    # каталог временных файлов вынесен в другое место — совет о нём самом
    monkeypatch.undo()
    load_config(_config(tmp_path, data))                        # каталог данных теперь в порядке
    monkeypatch.setattr(os, "makedirs", denied)
    monkeypatch.setattr(config_mod, "current_user", lambda: "mailarchiver")
    config = tmp_path / "tmp.yaml"
    config.write_text('paths:\n  data_dir: "%s"\nexport:\n  tmp_dir: "%s"\n' % (data, tmp_path / "fast" / "tmp"),
                      encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(str(config))
    assert "Не удалось создать каталог временных файлов" in info.value.message
    assert f"sudo mkdir -p {tmp_path / 'fast' / 'tmp'} && sudo chown mailarchiver: " in info.value.hint


def test_main_reports_os_errors_in_russian(tmp_path, capsys, monkeypatch):
    """Общий обработчик ошибок ОС у команд: причина по-русски, файл назван."""
    from mailarchiver import __main__ as cli

    def denied(args):
        raise PermissionError(errno.EACCES, "Permission denied", "/etc/mailarchiver/config.yaml")

    monkeypatch.setattr(cli, "cmd_version", denied)
    assert main(["version"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "ОШИБКА: нет прав: /etc/mailarchiver/config.yaml"
