"""Импорт файла .pst/.ost, который уже лежит на сервере, — каталог импорта.

Имя файла приходит из браузера, поэтому главное здесь — что путь по нему строит
только ``pstimport.inbox.locate`` и что он пускает лишь обычный файл прямо из
каталога импорта. Второе — файл администратора служба не удаляет и не меняет.
Третье — задание привязано к выбранному файлу (имя и приметы), а не к пути.

Тесты API в конце файла требуют FastAPI и без него пропускаются; остальным он
не нужен.
"""
import json
import os
import shutil
import time
from datetime import datetime

import pytest

from mailarchiver.errors import ValidationError
from mailarchiver.pstimport import inbox

PST = b"!BDN" + b"\0" * 60                # содержимое здесь не важно — только имя и вид файла


def _put(directory, name, data=PST):
    path = os.path.join(str(directory), name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


@pytest.fixture()
def box(tmp_path):
    """Каталог импорта и каталог рядом с ним (то, до чего импорт дотягиваться не должен)."""
    directory = tmp_path / "import"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return directory, outside


# ---------------------------------------------------------------------------
#  Список файлов
# ---------------------------------------------------------------------------
def test_list_shows_only_plain_pst_and_ost_files(box):
    directory, outside = box
    _put(directory, "b.pst", PST + b"x" * 10)
    _put(directory, "A.PST")
    _put(directory, "Почта Иванова 2019.ost")
    _put(directory, "notes.txt")
    _put(directory, "archive.pst.part")
    _put(directory, ".hidden.pst")
    _put(directory, "empty.pst", b"")
    (directory / "folder.pst").mkdir()
    _put(directory / "folder.pst", "inner.pst")
    secret = _put(outside, "secret.pst")
    os.symlink(secret, directory / "link-out.pst")
    os.symlink(directory / "b.pst", directory / "link-in.pst")
    os.symlink(directory / "nowhere.pst", directory / "broken.pst")
    os.mkfifo(directory / "pipe.pst")

    files = inbox.list_files(str(directory))
    assert [f["name"] for f in files] == ["A.PST", "b.pst", "Почта Иванова 2019.ost"]
    assert [f["size"] for f in files] == [len(PST), len(PST) + 10, len(PST)]
    assert all(f["readable"] is True for f in files)
    for item in files:
        assert set(item) == {"name", "size", "modified", "readable"}
        moment = datetime.fromisoformat(item["modified"])
        assert moment.utcoffset().total_seconds() == 0 and abs(moment.timestamp() - time.time()) < 300


def test_list_of_missing_or_wrong_directory_is_empty(tmp_path):
    assert inbox.list_files(str(tmp_path / "нет такого")) == []
    assert inbox.list_files(_put(tmp_path, "file.pst")) == []          # файл вместо каталога
    assert inbox.list_files("") == []
    assert inbox.list_files("bad\0name") == []


def test_list_skips_names_that_cannot_be_sent_to_browser(box):
    """Имя не в UTF-8 или с переводом строки не попадает в JSON-ответ (и не роняет его)."""
    directory, _outside = box
    _put(directory, "ok.pst")
    with open(os.path.join(os.fsencode(str(directory)), b"\xff\xfe.pst"), "wb") as fh:
        fh.write(PST)
    _put(directory, "two\nlines.pst")
    _put(directory, "back\\slash.pst")
    files = inbox.list_files(str(directory))
    assert [f["name"] for f in files] == ["ok.pst"]
    json.dumps(files, ensure_ascii=False).encode("utf-8")


def test_list_marks_files_the_service_cannot_read(box, monkeypatch):
    directory, _outside = box
    _put(directory, "mine.pst")
    foreign = _put(directory, "root-owned.pst")
    real = os.access
    monkeypatch.setattr(inbox.os, "access", lambda path, mode: path != foreign and real(path, mode))
    assert {f["name"]: f["readable"] for f in inbox.list_files(str(directory))} == {
        "mine.pst": True, "root-owned.pst": False}
    # выбрать такой файл нельзя: в подсказке — команда, которая отдаёт его службе
    with pytest.raises(ValidationError, match="нет прав") as info:
        inbox.resolve(str(directory), "root-owned.pst")
    assert "chown" in info.value.hint and "root-owned.pst" in info.value.hint


# ---------------------------------------------------------------------------
#  Имя → путь
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["mail.pst", "MAIL.PST", "Mail.Pst", "box.ost", "BOX.OST",
                                  "Почта Иванова (2019).pst", "a.b.c.pst", "-rf.pst", "~home.pst"])
def test_resolve_accepts_plain_files(box, name):
    directory, _outside = box
    _put(directory, name)
    path = inbox.resolve(str(directory), name)
    assert path == os.path.join(os.path.realpath(str(directory)), name)
    assert os.path.isabs(path) and os.path.isfile(path)


@pytest.mark.parametrize("name", [
    "../outside/secret.pst", "..", ".", "../x.pst", "/etc/passwd", "/etc/passwd.pst", "a/b.pst", "sub/inner.pst",
    "./mail.pst", "mail.pst/", "..\\x.pst", "a\\b.pst", "C:\\mail.pst", "x.pst\0", "\0", "mail\0.pst",
    "mail.pst\0.txt", "x.pst\n", "x\r\n.pst", "\x7f.pst", ".hidden.pst", ".pst", "..pst",
])
def test_resolve_rejects_paths_and_odd_names(box, name):
    directory, outside = box
    _put(directory, "mail.pst")
    _put(directory, ".hidden.pst")
    (directory / "sub").mkdir()
    _put(directory / "sub", "inner.pst")
    _put(outside, "secret.pst")
    _put(directory.parent, "x.pst")
    with pytest.raises(ValidationError) as info:
        inbox.resolve(str(directory), name)
    # присланное имя в сообщение не попадает
    assert info.value.hint and info.value.message == "Недопустимое имя файла."


@pytest.mark.parametrize("name", ["", None, 5, b"mail.pst", ["mail.pst"]])
def test_resolve_rejects_empty_and_non_string(box, name):
    directory, _outside = box
    _put(directory, "mail.pst")
    with pytest.raises(ValidationError, match="не выбран"):
        inbox.resolve(str(directory), name)


@pytest.mark.parametrize("name", ["mail.txt", "mail", "mail.pst.bak", "mail.pst ", "mail.zip", "passwd", " "])
def test_resolve_rejects_other_extensions(box, name):
    directory, _outside = box
    _put(directory, name)
    with pytest.raises(ValidationError, match=r"\.pst или \.ost"):
        inbox.resolve(str(directory), name)


def test_resolve_rejects_missing_empty_and_not_files(box):
    directory, _outside = box
    with pytest.raises(ValidationError, match="не найден") as info:
        inbox.resolve(str(directory), "gone.pst")
    assert str(directory) in info.value.hint                    # где искать — в подсказке
    with pytest.raises(ValidationError, match="не найден"):
        inbox.resolve(str(directory / "нет каталога"), "mail.pst")
    with pytest.raises(ValidationError, match="не найден"):
        inbox.resolve(str(directory), "я" * 300 + ".pst")         # длиннее предела файловой системы
    _put(directory, "empty.pst", b"")
    with pytest.raises(ValidationError, match="пуст"):
        inbox.resolve(str(directory), "empty.pst")
    (directory / "dir.pst").mkdir()
    _put(directory / "dir.pst", "inner.pst")
    with pytest.raises(ValidationError, match="не файл"):
        inbox.resolve(str(directory), "dir.pst")
    os.mkfifo(directory / "pipe.pst")
    with pytest.raises(ValidationError, match="не файл"):
        inbox.resolve(str(directory), "pipe.pst")
    # длинное имя в сообщение целиком не попадает
    with pytest.raises(ValidationError) as info:
        inbox.resolve(str(directory), "x" * 200 + ".pst")
    assert len(info.value.message) < 200


def test_resolve_rejects_symlinks_wherever_they_point(box):
    directory, outside = box
    secret = _put(outside, "secret.pst")
    inside = _put(directory, "real.pst")
    os.symlink(secret, directory / "out.pst")
    os.symlink(inside, directory / "in.pst")
    os.symlink("real.pst", directory / "relative.pst")
    os.symlink("/etc/passwd", directory / "passwd.pst")
    os.symlink(directory / "nowhere.pst", directory / "broken.pst")
    os.symlink(outside, directory / "dirlink.pst")
    for name in ("out.pst", "in.pst", "relative.pst", "passwd.pst", "broken.pst", "dirlink.pst"):
        with pytest.raises(ValidationError, match="символическая ссылка"):
            inbox.resolve(str(directory), name)
    assert inbox.resolve(str(directory), "real.pst") == os.path.join(os.path.realpath(str(directory)), "real.pst")


def test_resolve_checks_where_the_file_really_is(box, monkeypatch):
    """Страховка поверх проверок имени: файл, чей настоящий путь вне каталога, не проходит."""
    directory, outside = box
    _put(directory, "mail.pst")
    real = os.path.realpath

    def moved(path, *args, **kwargs):
        if os.path.basename(str(path)) == "mail.pst":
            return os.path.join(real(str(outside)), "mail.pst")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(inbox.os.path, "realpath", moved)
    with pytest.raises(ValidationError, match="вне каталога импорта"):
        inbox.resolve(str(directory), "mail.pst")


def test_import_directory_itself_may_be_a_symlink(tmp_path):
    """Каталог импорта на другом диске: ссылкой может быть сам каталог, но не файл в нём."""
    disk = tmp_path / "big-disk"
    disk.mkdir()
    link = tmp_path / "import"
    os.symlink(disk, link)
    _put(disk, "huge.pst")
    assert [f["name"] for f in inbox.list_files(str(link))] == ["huge.pst"]
    path = inbox.resolve(str(link), "huge.pst")
    assert path == os.path.join(os.path.realpath(str(disk)), "huge.pst")
    assert inbox.protected(str(link), path) and inbox.protected(str(link), str(link / "huge.pst"))
    with pytest.raises(ValidationError):
        inbox.resolve(str(link), "../big-disk/huge.pst")


def test_protected_means_directly_inside(box):
    """Уборка бережёт то, что лежит ПРЯМО в каталоге импорта, сам каталог и каталоги над ним."""
    directory, outside = box
    inside = _put(directory, "mail.pst")
    secret = _put(outside, "secret.pst")
    os.symlink(secret, directory / "link.pst")
    (directory / "sub").mkdir()
    assert inbox.protected(str(directory), inside)
    assert inbox.protected(str(directory), str(directory))
    assert inbox.protected(str(directory), str(directory / "sub"))
    assert inbox.protected(str(directory), str(directory / "gone.pst"))             # файла уже нет — каталог тот же
    assert inbox.protected(str(directory), str(directory / "link.pst"))             # ссылка — содержимое каталога
    assert inbox.protected(str(directory), str(outside / ".." / "import" / "mail.pst"))
    assert inbox.protected(str(directory), str(directory.parent))                   # удалить его — удалить и каталог импорта
    # глубже — уже не «в каталоге импорта»: там может лежать каталог временных файлов службы
    assert not inbox.protected(str(directory), str(directory / "sub" / "deep.pst"))
    assert not inbox.protected(str(directory), str(directory / "sub" / "tmp" / "import_1_x.pst"))
    assert not inbox.protected(str(directory), secret)
    assert not inbox.protected(str(directory), str(outside))
    assert not inbox.protected(str(directory), str(directory) + "-old/mail.pst")    # похожее имя каталога
    assert not inbox.protected(str(directory), "")
    assert not inbox.protected("", inside)
    assert not inbox.protected(str(directory), None)
    # ссылка, ведущая на каталог импорта или выше него, — тоже не мусор
    os.symlink(directory, outside / "to-import")
    os.symlink(directory.parent, outside / "to-parent")
    assert inbox.protected(str(directory), str(outside / "to-import"))
    assert inbox.protected(str(directory), str(outside / "to-parent"))


def test_locate_returns_path_and_stat(box):
    directory, _outside = box
    path = _put(directory, "mail.pst", PST + b"1234")
    found, info = inbox.locate(str(directory), "mail.pst")
    assert found == inbox.resolve(str(directory), "mail.pst") == os.path.join(os.path.realpath(str(directory)), "mail.pst")
    assert inbox.fingerprint(info) == {"size": len(PST) + 4, "mtime_ns": os.stat(path).st_mtime_ns,
                                       "dev": os.stat(path).st_dev, "ino": os.stat(path).st_ino}
    json.dumps(inbox.fingerprint(info))


def test_locate_without_server_paths_in_hints(box, monkeypatch):
    """Для журнала задания (его читает и сотрудник) подсказки — без каталогов и команд сервера."""
    directory, outside = box
    foreign = _put(directory, "root-owned.pst")
    for name, kwargs in (("gone.pst", {}), ("root-owned.pst", {"deny": True})):
        if kwargs.get("deny"):
            real = os.access
            monkeypatch.setattr(inbox.os, "access", lambda path, mode: path != foreign and real(path, mode))
        with pytest.raises(ValidationError) as open_info:
            inbox.locate(str(directory), name)
        with pytest.raises(ValidationError) as quiet_info:
            inbox.locate(str(directory), name, disclose=False)
        assert open_info.value.message == quiet_info.value.message
        assert str(directory) in open_info.value.hint
        assert str(directory) not in quiet_info.value.hint and os.sep not in quiet_info.value.hint
    # ссылку советуют заменить ссылкой-каталогом — и сразу говорят, куда служба не видит
    os.symlink(_put(outside, "x.pst"), directory / "link.pst")
    with pytest.raises(ValidationError, match="символическая ссылка") as info:
        inbox.locate(str(directory), "link.pst", disclose=False)
    assert "/home" in info.value.hint and "/tmp" in info.value.hint and "systemd" in info.value.hint


def _stat(size=100, mtime_ns=1_700_000_000_123_456_789, dev=64, ino=5):
    return os.stat_result((0o100600, ino, dev, 1, 0, 0, size, 0, mtime_ns // 10 ** 9, 0,
                           0, mtime_ns / 1e9, 0, 0, mtime_ns, 0))


def test_changed_compares_fingerprint():
    info = _stat()
    saved = inbox.fingerprint(info)
    assert saved == {"size": 100, "mtime_ns": 1_700_000_000_123_456_789, "dev": 64, "ino": 5}
    assert inbox.changed(saved, info) == []
    assert inbox.changed(json.loads(json.dumps(saved)), info) == []          # как в параметрах задания
    grown = inbox.changed(saved, _stat(size=5 * 1024 ** 3))
    assert len(grown) == 1 and "размер" in grown[0] and "100" in grown[0] and "5.0 ГБ" in grown[0]
    big = inbox.fingerprint(_stat(size=5 * 1024 ** 3))                        # разница меньше округления
    assert f"{5 * 1024 ** 3 + 1} байт" in inbox.changed(big, _stat(size=5 * 1024 ** 3 + 1))[0]
    assert inbox.changed(saved, _stat(mtime_ns=1_700_000_000_123_456_790)) == ["время изменения"]
    replaced = inbox.changed(saved, _stat(ino=6))
    assert len(replaced) == 1 and "другой" in replaced[0]
    assert len(inbox.changed(saved, _stat(size=7, mtime_ns=1, ino=6))) == 3
    # диск подключили заново: номер устройства другой, а файл тот же — не повод отказывать
    assert inbox.changed(saved, _stat(dev=65)) == []
    assert inbox.changed(saved, _stat(dev=65, ino=77)) == []
    assert inbox.changed(saved, _stat(dev=65, size=7)) != []
    # примет нет (задание поставлено до их появления) или они испорчены — сверять нечего
    for nothing in (None, "", [], 5, {}):
        assert inbox.changed(nothing, info) == []
    assert inbox.changed({"size": "100", "mtime_ns": None, "ino": True, "dev": 64}, _stat(size=1)) == []
    assert inbox.changed({"size": 100}, _stat(size=1)) != []


def test_service_user_is_a_name():
    name = inbox.service_user()
    assert isinstance(name, str) and name and "/" not in name


def test_available_tells_unreadable_directory_from_empty_one(box, monkeypatch):
    """Каталог, который служба прочитать не может, — не то же, что каталог без файлов."""
    directory, _outside = box
    assert inbox.available(str(directory)) and inbox.list_files(str(directory)) == []
    assert not inbox.available(str(directory / "нет такого"))
    assert not inbox.available(_put(directory, "file.pst"))               # файл вместо каталога
    assert not inbox.available("") and not inbox.available("bad\0name")
    monkeypatch.setattr(inbox.os, "access", lambda path, mode: False)      # каталог создан от root
    assert not inbox.available(str(directory))


def _import_link(data_dir, target):
    """Каталог импорта — ссылка на ``target`` (каталога данных служба ещё не трогала)."""
    link = os.path.join(data_dir, "import")
    os.symlink(target, link)
    return link


def test_problem_names_the_reason(cfg, tmp_path, monkeypatch):
    """Причина, по которой каталог импорта недоступен, называется точно — от неё зависит совет."""
    directory = cfg.import_dir
    assert inbox.problem(cfg) == ("", "")
    os.rmdir(directory)
    kind, reason = inbox.problem(cfg)
    assert kind == "missing" and directory in reason and not reason.endswith(".")
    _put(cfg.data_dir, "import")
    kind, reason = inbox.problem(cfg)
    assert kind == "not_dir" and "не каталог" in reason
    os.unlink(directory)
    os.symlink(tmp_path / "unmounted" / "pst", directory)
    kind, reason = inbox.problem(cfg)
    assert kind == "dangling" and "ссылка на" in reason and "unmounted/pst" in reason
    os.unlink(directory)
    os.symlink("import", directory)
    kind, reason = inbox.problem(cfg)
    assert kind == "loop" and "Too many" not in reason and "ссылки" in reason
    os.unlink(directory)
    os.mkdir(directory)
    monkeypatch.setattr(inbox.os, "access", lambda path, mode: False)      # каталог создан от root
    kind, reason = inbox.problem(cfg)
    assert kind == "closed" and directory in reason and inbox.service_user() in reason
    monkeypatch.undo()

    # путь к каталогу закрыт: войти в каталог над ним нельзя
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path) == directory:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    kind, reason = inbox.problem(cfg)
    assert kind == "denied" and "нет прав" in reason and "Permission" not in reason


# ---------------------------------------------------------------------------
#  Конфигурация и уборка
# ---------------------------------------------------------------------------
def test_config_creates_import_directory(cfg, data_dir):
    assert cfg.import_dir == os.path.join(data_dir, "import")
    assert os.path.isdir(cfg.import_dir)
    assert os.stat(cfg.import_dir).st_mode & 0o777 == 0o700 == os.stat(cfg.exports_dir).st_mode & 0o777


def test_unavailable_import_directory_does_not_stop_the_service(data_dir, tmp_path):
    """Каталог импорта — ссылка на отключённый диск: служба запускается, список файлов пуст."""
    from mailarchiver.config import load_config
    os.symlink(tmp_path / "unmounted-disk" / "import", os.path.join(data_dir, "import"))
    cfg = load_config()
    assert os.path.isdir(cfg.tmp_dir) and os.path.islink(cfg.import_dir)
    assert inbox.list_files(cfg.import_dir) == [] and not inbox.available(cfg.import_dir)
    assert inbox.problem(cfg)[0] == "dangling" and cfg.warnings == []
    with pytest.raises(ValidationError, match="не найден"):
        inbox.resolve(cfg.import_dir, "mail.pst")


def test_tmp_dir_inside_import_directory_only_disables_server_import(data_dir, tmp_path):
    """Каталоги пересеклись — это не причина не запускаться: отключается только импорт с сервера."""
    from mailarchiver.config import load_config
    import_dir = os.path.join(data_dir, "import")
    path = tmp_path / "config.yaml"
    for tmp_dir, relation in ((import_dir, "совпадает с каталогом"), (os.path.join(import_dir, "tmp"), "содержит каталог")):
        path.write_text(f"export:\n  tmp_dir: {tmp_dir}\n", encoding="utf-8")
        cfg = load_config(str(path))                              # раньше — ConfigError, служба не запускалась
        assert os.path.isdir(cfg.tmp_dir) and os.path.isdir(cfg.import_dir)
        assert len(cfg.warnings) == 1
        warning = cfg.warnings[0]
        assert import_dir in warning and tmp_dir in warning and relation in warning
        assert "отключён" in warning and "export.tmp_dir" in warning
        kind, reason = inbox.problem(cfg)
        assert kind == "overlap" and import_dir in reason and tmp_dir in reason
    # каталог данных целиком под временные файлы — можно: каталог импорта в нём уборка не трогает
    path.write_text(f"export:\n  tmp_dir: {data_dir}\n", encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.tmp_dir == data_dir and cfg.warnings == [] and inbox.problem(cfg) == ("", "")


def test_import_link_to_disk_with_service_data_does_not_stop_the_service(data_dir, tmp_path, capsys):
    """``import -> /mnt/big`` при данных в ``/mnt/big/mailarchiver``: служба запускается и говорит, в чём дело.

    Раньше перезапуск кончался ошибкой про ``export.tmp_dir`` (который не задан), и служба не поднималась.
    """
    from mailarchiver.__main__ import main
    from mailarchiver.config import load_config
    big = os.path.realpath(str(tmp_path))                         # «диск», на котором лежит и каталог данных
    link = _import_link(data_dir, big)
    for _restart in range(2):
        cfg = load_config()
        assert os.path.islink(link) and os.path.isdir(cfg.tmp_dir)
        assert len(cfg.warnings) == 1
        warning = cfg.warnings[0]
        # названы оба настоящих пути, а не параметр, которого в конфигурации нет
        assert big in warning and os.path.realpath(cfg.tmp_dir) in warning and link in warning
        assert "export.tmp_dir" not in warning and "отключён" in warning and "отдельный каталог" in warning
    kind, reason = inbox.problem(cfg)
    assert kind == "overlap" and big in reason and os.path.realpath(cfg.tmp_dir) in reason
    # check-config: предупреждение, а не ошибка
    config = tmp_path / "config.yaml"
    config.write_text('paths:\n  data_dir: "%s"\n' % data_dir, encoding="utf-8")
    assert main(["-c", str(config), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "Предупреждение: Каталог импорта" in out and "есть предупреждения" in out


def _aged(path, hours=100):
    old = time.time() - hours * 3600
    os.utime(path, (old, old), follow_symlinks=False)
    return path


def test_cleanup_never_touches_import_directory(services, monkeypatch):
    """Ни ежечасная уборка, ни уборка при запуске не удаляют файлы каталога импорта."""
    from mailarchiver import maintenance
    cfg = services.cfg
    kept = [_aged(_put(cfg.import_dir, name)) for name in ("import_1_old.pst", "Архив.pst", "export_x.pst")]
    stale = _aged(_put(cfg.tmp_dir, "import_1_abc_upload.pst"))
    maintenance.run_maintenance(services)
    maintenance.startup_cleanup(services)
    assert not os.path.exists(stale) and all(os.path.exists(path) for path in kept)

    # временные файлы настроены в сам каталог данных: каталог «import» — не «временный остаток»
    monkeypatch.setattr(cfg, "tmp_dir", cfg.data_dir)
    _aged(cfg.import_dir)
    stale = _aged(_put(cfg.data_dir, "import_2_abc_upload.pst"))
    assert maintenance.clean_tmp(services, 0) == 1
    assert not os.path.exists(stale) and all(os.path.exists(path) for path in kept)

    # и даже если каталоги совпали (импорт с сервера при этом отключён) — файлы остаются
    monkeypatch.setattr(cfg, "tmp_dir", cfg.import_dir)
    assert maintenance.clean_tmp(services, 0) == 0
    assert all(os.path.exists(path) for path in kept)


# ---------------------------------------------------------------------------
#  Задание импорта: файл с сервера остаётся на месте
# ---------------------------------------------------------------------------
def _mail(subject, body="text"):
    from mailarchiver.export.base import MailItem
    raw = (f"From: a@example.ru\r\nTo: b@example.ru\r\nSubject: {subject}\r\n"
           f"Message-ID: <{subject}@example.ru>\r\nDate: Mon, 01 Sep 2025 10:00:00 +0300\r\n\r\n{body}\r\n").encode()
    return MailItem(folder="INBOX", raw=raw, flags=["\\Seen"], internaldate=1_756_700_000, size=len(raw))


@pytest.fixture(scope="module")
def small_pst(tmp_path_factory):
    """Настоящий .pst из пяти писем (его пишет собственный писатель службы)."""
    from mailarchiver.export import pst_native
    tmp = tmp_path_factory.mktemp("inbox-pst")
    out = str(tmp / "five.pst")
    items = [_mail(f"letter-{n}") for n in range(5)]
    res = pst_native.NativePstExportEngine().export(iter(items), out, options={"tmp_dir": str(tmp)},
                                                    total_hint=len(items))
    assert res.errors == 0
    return out


def _account(services):
    from mailarchiver import models
    return services.db.create_account(models.Account(name="Архив Иванова", host="h", username="u", password="p"))


def _job(services, acc_id, params):
    from mailarchiver.models import JobType
    from mailarchiver.queue.jobs import JobContext, handle_import_pst
    params = dict({"target": "local", "target_prefix": "Импорт PST"}, **params)
    job_id = services.db.enqueue_job(JobType.IMPORT_PST, acc_id, params, 5, 1, "tester")
    return handle_import_pst(JobContext(services, job_id, JobType.IMPORT_PST, acc_id, params)), job_id


def _server_file(services, small_pst, name="Архив 2019.pst"):
    """Файл в каталоге импорта с заметно старым временем изменения."""
    path = os.path.join(services.cfg.import_dir, name)
    shutil.copy(small_pst, path)
    return _aged(path, hours=1000)


def _chosen(path):
    """Параметры задания «с сервера» — какими их ставит API: имя и приметы файла, без пути."""
    return {"source": "server", "server_file": os.path.basename(path),
            "fingerprint": inbox.fingerprint(os.lstat(path))}


def _untouched(path, small_pst):
    return (os.path.isfile(path) and os.path.getsize(path) == os.path.getsize(small_pst)
            and time.time() - os.path.getmtime(path) > 999 * 3600)


def _events(services, job_id):
    return [e["message"] for e in services.db.query("SELECT message FROM job_events WHERE job_id=? ORDER BY id",
                                                    (job_id,))]


def _failure(services, acc_id, params):
    """Провал задания так, как его записывает очередь: (исключение, строка журнала задания)."""
    with pytest.raises(ValidationError) as info:
        _job(services, acc_id, params)
    exc = info.value
    return exc, exc.message + (f" | {exc.hint}" if exc.hint else "")


def _no_server_paths(services, text):
    cfg = services.cfg
    return not any(part in text for part in (cfg.import_dir, os.path.realpath(cfg.import_dir), cfg.data_dir,
                                             cfg.tmp_dir, "sudo "))


def test_server_file_is_imported_and_left_in_place(services, small_pst):
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    path = _server_file(services, small_pst)
    params = _chosen(path)
    assert "pst_path" not in params
    res, job_id = _job(services, acc_id, params)
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 5 and res["source"] == "server"
    assert services.db.count_messages(acc_id) == 5
    assert _untouched(path, small_pst)
    events = _events(services, job_id)
    assert any("взят из каталога импорта на сервере и остаётся там" in text for text in events)
    # журнал задания читает и сотрудник, вошедший по своему ящику: ни путей сервера, ни советов администратору
    assert all(_no_server_paths(services, text) and "удалите" not in text for text in events), events
    # «Повторить» у того же задания: файл на месте, второй раз письма не записываются
    res, _ = _job(services, acc_id, params)
    assert res["imported"] == 0 and res["duplicates"] == 5 and _untouched(path, small_pst)


def test_uploaded_file_is_still_removed_and_old_jobs_work(services, small_pst, tmp_path):
    """Задание без признака источника (поставлено прежней версией) — это загрузка: файл удаляется."""
    acc_id = _account(services)
    upload = str(tmp_path / "import_1_abc_Outlook.pst")
    shutil.copy(small_pst, upload)
    res, _ = _job(services, acc_id, {"pst_path": upload})
    assert res["imported"] == 5 and res["source"] == "upload" and not os.path.exists(upload)
    shutil.copy(small_pst, upload)
    res, _ = _job(services, acc_id, {"pst_path": upload, "source": "upload", "target_prefix": "Ещё раз"})
    assert res["imported"] == 5 and res["source"] == "upload" and not os.path.exists(upload)
    # у загрузки файла нет — прежнее сообщение, каталог импорта ни при чём
    with pytest.raises(ValidationError, match="для импорта не найден") as info:
        _job(services, acc_id, {"pst_path": upload, "source": "upload"})
    assert "Загрузите файл заново" in info.value.hint
    with pytest.raises(ValidationError, match="для импорта не найден"):
        _job(services, acc_id, {"source": "upload", "server_file": "x.pst"})


def test_server_job_without_fingerprint_still_runs(services, small_pst):
    """Задания «с сервера», поставленные до появления имени и примет в параметрах, выполняются как раньше."""
    acc_id = _account(services)
    path = _server_file(services, small_pst)
    res, _ = _job(services, acc_id, {"pst_path": path, "source": "server"})          # путь вместо имени
    assert res["imported"] == 5 and res["source"] == "server" and _untouched(path, small_pst)
    res, _ = _job(services, acc_id, {"server_file": os.path.basename(path), "source": "server",
                                     "target_prefix": "Без примет"})
    assert res["imported"] == 5 and _untouched(path, small_pst)
    # из пути берётся только имя: файл ищется в каталоге импорта и больше нигде
    res, _ = _job(services, acc_id, {"pst_path": "/somewhere/else/" + os.path.basename(path), "source": "server",
                                     "target_prefix": "Путь не важен"})
    assert res["imported"] == 5 and _untouched(path, small_pst)


def test_job_is_bound_to_the_chosen_file(services, small_pst, tmp_path):
    """Файл заменили между постановкой задания и запуском (или «Повторить») — чужой архив не импортируется."""
    acc_id = _account(services)
    path = _server_file(services, small_pst)
    params = _chosen(path)

    def refused(why):
        exc, line = _failure(services, acc_id, params)
        assert "изменился после постановки задания" in exc.message and "запустите импорт заново" in exc.message
        assert os.path.basename(path) in exc.message and why in exc.hint, exc.hint
        assert _no_server_paths(services, line), line
        assert services.db.count_messages(acc_id) == 0            # не импортировано ничего
        return exc

    # 1. другой архив под тем же именем (скопирован поверх): другой размер и время
    other = str(tmp_path / "other.pst")
    with open(other, "wb") as fh:
        fh.write(open(small_pst, "rb").read() + b"\0" * 512)
    shutil.copy(other, path)
    refused("размер")
    assert os.path.getsize(path) == os.path.getsize(other)        # и сам файл не тронут

    # 2. того же размера, но записан позже (файл ещё копировали, когда ставили задание)
    shutil.copy(small_pst, path)
    assert os.path.getsize(path) == params["fingerprint"]["size"]
    exc = refused("время изменения")
    assert "размер" not in exc.hint and "копируется" in exc.hint

    # 3. подменён переименованием: размер и время те же (cp -p), файл — другой
    twin = os.path.join(services.cfg.data_dir, "twin.pst")     # та же файловая система, прежний файл ещё на месте
    shutil.copy(small_pst, twin)
    os.utime(twin, ns=(params["fingerprint"]["mtime_ns"],) * 2)
    os.replace(twin, path)
    now = os.lstat(path)
    assert (now.st_size, now.st_mtime_ns) == (params["fingerprint"]["size"], params["fingerprint"]["mtime_ns"])
    assert now.st_ino != params["fingerprint"]["ino"]
    refused("другой")

    # тот же файл, что выбирали, — импортируется; «Повторить» — тоже
    params = _chosen(path)
    res, _ = _job(services, acc_id, params)
    assert res["imported"] == 5
    res, _ = _job(services, acc_id, params)
    assert res["imported"] == 0 and res["duplicates"] == 5
    # диск с каталогом импорта подключили заново (другой номер устройства) — файл тот же, «Повторить» работает
    remounted = dict(params, fingerprint=dict(params["fingerprint"], dev=params["fingerprint"]["dev"] + 1))
    res, _ = _job(services, acc_id, remounted)
    assert res["duplicates"] == 5


def test_server_file_survives_every_outcome(services, small_pst, monkeypatch):
    from mailarchiver.errors import DiskSpaceError, ImapAuthError, JobCancelled
    from mailarchiver.imap import client as client_mod
    from mailarchiver.models import JobStatus
    from mailarchiver.queue.jobs import JobContext
    acc_id = _account(services)
    path = _server_file(services, small_pst)
    params = _chosen(path)

    # кончилось место на диске архива: загруженный файл здесь удаляется, файл с сервера — нет
    real = services.store.store_message
    stored = [0]

    def store(*args, **kwargs):
        if stored[0] >= 2:
            raise DiskSpaceError("Недостаточно места на диске: свободно 10 МБ")
        stored[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(services.store, "store_message", store)
    res, _ = _job(services, acc_id, params)
    assert res["final_status"] == JobStatus.PARTIAL and res["imported"] == 2
    assert "остаётся в каталоге импорта" in res["summary"] and "«Повторить»" in res["summary"]
    assert "Загруженный файл сохранён" not in res["summary"] and _untouched(path, small_pst)
    assert _no_server_paths(services, res["summary"])
    monkeypatch.undo()

    # почтовый сервер не пустил: у загрузки здесь обновляется время изменения, тут — ничего
    class Refusing(client_mod.ReconnectingSession):
        def open(self):
            raise ImapAuthError("Сервер отклонил пароль")

    monkeypatch.setattr(client_mod, "ReconnectingSession", Refusing)
    with pytest.raises(ImapAuthError):
        _job(services, acc_id, dict(params, target="imap"))
    assert _untouched(path, small_pst)
    monkeypatch.undo()

    # отмена
    monkeypatch.setattr(JobContext, "is_cancelled", lambda self: True)
    with pytest.raises(JobCancelled):
        _job(services, acc_id, dict(params, target_prefix="Отмена"))
    assert _untouched(path, small_pst)
    monkeypatch.undo()

    # файл не разобрался (не .pst): загруженный удалили бы, этот остаётся — его положил администратор
    junk = _aged(_put(services.cfg.import_dir, "junk.pst", b"this is not a pst file"), hours=1000)
    with pytest.raises(ValidationError, match="не разобран"):
        _job(services, acc_id, _chosen(junk))
    assert os.path.getsize(junk) == 22 and time.time() - os.path.getmtime(junk) > 999 * 3600


def test_source_is_decided_by_job_params_not_by_path(data_dir, tmp_path, small_pst):
    """Каталог импорта — ссылка на диск, где лежат и данные службы: загрузка остаётся загрузкой.

    Раньше «файл администратора» узнавали ещё и по пути, а «внутри каталога импорта» тогда оказывался
    весь каталог временных файлов: задание загрузки падало с «не найден в каталоге импорта»,
    загруженный файл не удалялся, а уборка обходила каталог временных файлов целиком.
    """
    from mailarchiver import maintenance
    from mailarchiver.config import load_config
    from mailarchiver.service import Services
    big = os.path.realpath(str(tmp_path))
    _import_link(data_dir, big)
    services = Services(load_config())
    services.setup()
    cfg = services.cfg
    acc_id = _account(services)
    admin_file = _aged(shutil.copy(small_pst, os.path.join(big, "import_archive.pst")), hours=1000)

    upload = os.path.join(cfg.tmp_dir, "import_1_abc_Outlook.pst")
    shutil.copy(small_pst, upload)
    res, _ = _job(services, acc_id, {"pst_path": upload, "source": "upload"})
    assert res["imported"] == 5 and res["source"] == "upload" and not os.path.exists(upload)
    shutil.copy(small_pst, upload)
    res, _ = _job(services, acc_id, {"pst_path": upload, "target_prefix": "Прежняя версия"})     # без признака
    assert res["imported"] == 5 and res["source"] == "upload" and not os.path.exists(upload)

    # уборка каталога временных файлов работает; файл администратора на «диске» она не трогает
    stale = _aged(_put(cfg.tmp_dir, "import_9_old_upload.pst"))
    assert maintenance.clean_tmp(services, 0) == 1 and not os.path.exists(stale)
    maintenance.startup_cleanup(services)
    assert _untouched(admin_file, small_pst)

    # импорт с сервера при таком каталоге отключён — и задание об этом говорит, а не ищет файл
    exc, line = _failure(services, acc_id, {"source": "server", "server_file": "import_archive.pst"})
    assert "отключён" in exc.message and "пересекается" in exc.message and _no_server_paths(services, line)
    assert big not in line and _untouched(admin_file, small_pst)


def test_server_file_is_checked_again_when_the_job_runs(services, small_pst, tmp_path):
    """Между постановкой задания и запуском файл могли убрать или подменить."""
    acc_id = _account(services)
    path = _server_file(services, small_pst)
    params = _chosen(path)
    os.unlink(path)
    exc, line = _failure(services, acc_id, params)
    assert "не найден в каталоге импорта" in exc.message and "Загрузите файл заново" not in exc.hint
    assert _no_server_paths(services, line), line              # сотруднику каталоги сервера знать незачем

    outside = str(tmp_path / "outside.pst")
    shutil.copy(small_pst, outside)
    os.symlink(outside, path)
    exc, line = _failure(services, acc_id, params)
    assert "символическая ссылка" in exc.message and _no_server_paths(services, line)
    assert os.path.islink(path) and os.path.exists(outside)

    # в параметрах задания — только имя: путь мимо каталога импорта не проходит
    for bad in (outside, "/etc/passwd", "../outside.pst", os.path.join("..", "mailarchiver.db"), "", None, 5):
        with pytest.raises(ValidationError):
            _job(services, acc_id, {"source": "server", "server_file": bad})
    assert os.path.exists(outside)
    # каталог импорта пропал целиком (отключили диск) — понятный отказ без путей
    os.unlink(path)
    os.rmdir(services.cfg.import_dir)
    exc, line = _failure(services, acc_id, params)
    assert "Каталог импорта на сервере сейчас недоступен" in exc.message and "«Повторить»" in exc.hint
    assert _no_server_paths(services, line)
    assert services.db.count_messages(acc_id) == 0


def test_half_copied_server_file_is_explained(services, small_pst, tmp_path):
    """Недокопированный файл с сервера: не «повреждён, загрузите заново», а «дождитесь конца копирования»."""
    acc_id = _account(services)
    data = open(small_pst, "rb").read()
    part = _aged(_put(services.cfg.import_dir, "Outlook.pst", data[:16384]), hours=1000)
    exc, line = _failure(services, acc_id, _chosen(part))
    assert "повреждён и не читается" in exc.message
    assert "копировали" in exc.hint and "запустите импорт заново" in exc.hint
    assert "загрузите его заново" not in exc.hint and "scanpst" in exc.hint
    assert _no_server_paths(services, line), line
    assert os.path.getsize(part) == 16384 and time.time() - os.path.getmtime(part) > 999 * 3600

    # не .pst вовсе — подсказка про копирование та же, совет другой
    junk = _put(services.cfg.import_dir, "junk.pst", b"this is not a pst file")
    exc, _line = _failure(services, acc_id, _chosen(junk))
    assert "не разобран" in exc.message and "копировали" in exc.hint and "scanpst" not in exc.hint

    # копирование закончилось: прежнее задание («Повторить») файл уже не возьмёт, новое — импортирует
    stale = _chosen(part)
    shutil.copy(small_pst, part)
    exc, _line = _failure(services, acc_id, stale)
    assert "изменился после постановки задания" in exc.message
    res, _ = _job(services, acc_id, _chosen(part))
    assert res["imported"] == 5

    # у загруженного файла подсказка прежняя: копирование на сервер тут ни при чём
    upload = str(tmp_path / "import_1_abc_Outlook.pst")
    with open(upload, "wb") as fh:
        fh.write(data[:16384])
    with pytest.raises(ValidationError, match="повреждён и не читается") as info:
        _job(services, acc_id, {"pst_path": upload})
    assert "загрузите его заново" in info.value.hint and "копировали" not in info.value.hint
    assert not os.path.exists(upload)


def test_server_file_written_during_import_is_reported(services, small_pst, monkeypatch):
    """Файл дописывали, пока шёл импорт: итог — «частично» с объяснением, а не «успешно»."""
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    path = _server_file(services, small_pst)
    real = services.store.store_message

    def meanwhile(action):
        """Импорт, во время которого (на первом же письме) с файлом происходит ``action``."""
        done = []

        def store(*args, **kwargs):
            if not done:
                done.append(action())
            return real(*args, **kwargs)

        monkeypatch.setattr(services.store, "store_message", store)
        try:
            return _job(services, acc_id, dict(_chosen(path), target_prefix=action.__name__))
        finally:
            monkeypatch.undo()

    def append():                                              # копирование на сервер ещё идёт
        with open(path, "ab") as fh:
            fh.write(b"\0" * 4096)

    def replace():
        shutil.copy(small_pst, path + ".new")
        os.replace(path + ".new", path)

    def remove():
        os.unlink(path)

    res, job_id = meanwhile(append)
    assert res["final_status"] == JobStatus.PARTIAL and res["imported"] == 5
    assert "менялся, пока шёл импорт" in res["summary"] and "запустите импорт заново" in res["summary"]
    assert any("менялся, пока шёл импорт" in text for text in _events(services, job_id))
    # файл, который во время импорта заменили другим или убрали, читался прежний, открытый — он цел
    for action in (replace, remove):
        res, job_id = meanwhile(action)
        assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 5, action.__name__
        assert "менялся" not in res["summary"] and not any("менялся" in text for text in _events(services, job_id))
    assert not os.path.exists(path)


def test_unreadable_server_file_error_has_no_path(services, small_pst, monkeypatch):
    """Сбой чтения самого файла: в сообщении ОС — полный путь, в журнал задания он не попадает."""
    from mailarchiver.pstimport import importer
    acc_id = _account(services)
    path = _server_file(services, small_pst)

    def broken(pst_path, **kwargs):
        raise OSError(5, "Input/output error", pst_path)

    monkeypatch.setattr(importer, "PstReader", broken)
    exc, line = _failure(services, acc_id, _chosen(path))
    assert "не читается" in exc.message and os.path.basename(path) in exc.message
    assert _no_server_paths(services, line) and _untouched(path, small_pst)


def test_cleanup_copes_with_jobs_without_path(services, small_pst):
    """В параметрах задания «с сервера» пути нет — уборке временных файлов это не мешает."""
    from mailarchiver import maintenance
    from mailarchiver.models import JobType
    path = _server_file(services, small_pst)
    upload = _aged(_put(services.cfg.tmp_dir, "import_1_abc_Outlook.pst"))
    services.db.enqueue_job(JobType.IMPORT_PST, _account(services), _chosen(path), 5, 1, "tester")
    services.db.enqueue_job(JobType.IMPORT_PST, _account(services), {"pst_path": upload, "source": "upload"},
                            5, 1, "tester")
    stale = _aged(_put(services.cfg.tmp_dir, "import_2_def_Outlook.pst"))
    assert maintenance._referenced_tmp_paths(services) == {upload}
    assert maintenance.clean_tmp(services, 0) == 1
    assert os.path.exists(upload) and not os.path.exists(stale) and _untouched(path, small_pst)


# ---------------------------------------------------------------------------
#  API (нужен FastAPI)
# ---------------------------------------------------------------------------
PW = "Sw0rdfish!"
HDR = {"X-Requested-With": "fetch"}


@pytest.fixture()
def web(request):
    """Вошедший администратор, ящик и службы. Без FastAPI тест пропускается."""
    pytest.importorskip("fastapi")
    client = request.getfixturevalue("client")
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})
    aid = client.post("/api/accounts", json={"name": "Иванов", "host": "h", "port": 993,
                                             "username": "ivanov@x.ru", "password": "p"}).json()["id"]
    return client, aid, client.app.state.services


def _params(svc, response):
    assert response.status_code == 200, response.text
    return json.loads(svc.db.get_job(response.json()["job_id"])["params"])


def _import_jobs(svc):
    """Задания импорта в любом состоянии (служба может ставить и свои задания — их не считаем)."""
    return svc.db.query("SELECT id FROM jobs WHERE type='import_pst'")


BOUNDARY = "----WebKitFormBoundaryMailArchiverTest"
#: Поле файла, в котором файл не выбран, — как его шлёт браузер: часть с ПУСТЫМ именем файла.
#: Через ``files={"file": ("", b"")}`` такого не отправить: httpx пустое имя выбрасывает, и до
#: сервера доходит обычное текстовое поле, а не то, что проверяется.
EMPTY_FILE = ('name="file"; filename=""', b"", "application/octet-stream")


def _multipart(*parts):
    """Тело multipart/form-data байт в байт: (заголовок Content-Disposition, содержимое[, тип])."""
    body = b""
    for disposition, data, *content_type in parts:
        body += f"--{BOUNDARY}\r\nContent-Disposition: form-data; {disposition}\r\n".encode()
        if content_type:
            body += f"Content-Type: {content_type[0]}\r\n".encode()
        body += b"\r\n" + (data if isinstance(data, bytes) else str(data).encode()) + b"\r\n"
    body += f"--{BOUNDARY}--\r\n".encode()
    return {"content": body, "headers": {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}}


def _field(name, value):
    return (f'name="{name}"', value)


def test_multipart_helper_sends_what_a_browser_sends():
    """Сам помощник: пустая часть файла уходит с ``filename=""`` (FastAPI для этого не нужен)."""
    request = _multipart(EMPTY_FILE, _field("server_file", "Архив.pst"))
    assert request["headers"]["Content-Type"].endswith(BOUNDARY)
    text = request["content"].decode()
    assert 'Content-Disposition: form-data; name="file"; filename=""\r\nContent-Type: application/octet-stream\r\n\r\n\r\n' in text
    assert 'Content-Disposition: form-data; name="server_file"\r\n\r\nАрхив.pst\r\n' in text
    assert text.startswith(f"--{BOUNDARY}\r\n") and text.endswith(f"--{BOUNDARY}--\r\n")


def test_api_lists_files_of_import_directory(web):
    client, _aid, svc = web
    r = client.get("/api/import-files")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["directory"] == svc.cfg.import_dir and body["files"] == [] and body["available"] is True
    assert body["max_upload_bytes"] == 2 * 1024 ** 3 and body["user"]
    assert body["problem"] == "" and body["reason"] == "" and body["link_target"] == ""
    assert set(body) == {"directory", "link_target", "files", "available", "problem", "reason", "user",
                         "max_upload_bytes"}
    _put(svc.cfg.import_dir, "Архив 2019.PST", PST + b"1234")
    _put(svc.cfg.import_dir, "box.ost")
    _put(svc.cfg.import_dir, ".hidden.pst")
    _put(svc.cfg.import_dir, "readme.txt")
    os.symlink(svc.cfg.db_path, os.path.join(svc.cfg.import_dir, "db.pst"))
    files = client.get("/api/import-files").json()["files"]
    assert [(f["name"], f["size"], f["readable"]) for f in files] == [
        ("box.ost", len(PST), True), ("Архив 2019.PST", len(PST) + 4, True)]
    assert all(f["modified"].endswith("+00:00") for f in files)


def test_api_tells_why_import_directory_is_unavailable(web, tmp_path):
    """Окну импорта нужна точная причина: от неё зависит, что посоветовать."""
    client, aid, svc = web
    svc.queue.stop()
    directory = svc.cfg.import_dir
    _put(directory, "server.pst")
    url = f"/api/accounts/{aid}/import-pst"

    def state():
        body = client.get("/api/import-files").json()
        assert body["files"] == [] and body["available"] is False and body["directory"] == directory
        assert body["reason"] and not body["reason"].endswith(".")
        r = client.post(url, data={"server_file": "server.pst", "target": "local"})
        assert r.status_code == 400 and body["reason"] in r.json()["message"], r.text
        return body

    os.rename(directory, directory + ".off")
    assert state()["problem"] == "missing"
    os.symlink(tmp_path / "unmounted" / "pst", directory)                  # диск не подключён
    body = state()
    assert body["problem"] == "dangling" and "ссылка на" in body["reason"]
    assert body["link_target"] == str(tmp_path / "unmounted" / "pst")
    os.unlink(directory)
    os.symlink(svc.cfg.data_dir, directory)                                # ссылка на каталог с данными службы
    body = state()
    assert body["problem"] == "overlap" and os.path.realpath(svc.cfg.tmp_dir) in body["reason"]
    assert body["link_target"] == os.path.realpath(svc.cfg.data_dir)
    # загрузка с компьютера при этом работает
    r = client.post(url, data={"target": "local"}, files={"file": ("Outlook.pst", PST, "application/octet-stream")})
    assert _params(svc, r)["source"] == "upload"
    os.unlink(directory)
    os.rename(directory + ".off", directory)
    body = client.get("/api/import-files").json()
    assert body["available"] is True and [f["name"] for f in body["files"]] == ["server.pst"]
    assert len(_import_jobs(svc)) == 1


def test_api_import_from_server_file(web, small_pst):
    client, aid, svc = web
    name = "Архив 2019.PST"
    path = _aged(shutil.copy(small_pst, os.path.join(svc.cfg.import_dir, name)), hours=1000)
    # так запрос шлёт браузер: multipart/form-data без части с файлом
    r = client.post(f"/api/accounts/{aid}/import-pst",
                    files={"server_file": (None, name), "target": (None, "local"),
                           "target_prefix": (None, "Архив"), "skip_existing": (None, "1")})
    params = _params(svc, r)
    assert params["source"] == "server" and params["target"] == "local" and params["target_prefix"] == "Архив"
    # В задании — имя и приметы файла, пути нет: прежняя версия после импорта удаляла файл по pst_path,
    # и после отката на неё «Повторить» стёрло бы файл администратора.
    info = os.lstat(path)
    assert params["server_file"] == name and "pst_path" not in params
    assert params["fingerprint"] == {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
                                     "dev": info.st_dev, "ino": info.st_ino}
    assert not any(isinstance(value, str) and os.sep in value for value in params.values())
    # файл не копировался во временный каталог
    assert not [n for n in os.listdir(svc.cfg.tmp_dir) if n.startswith("import_")]
    job_id = r.json()["job_id"]
    deadline = time.time() + 60
    while time.time() < deadline and svc.db.get_job(job_id)["status"] not in ("success", "partial", "failed"):
        time.sleep(0.2)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "success" and job["result"]["imported"] == 5 and job["result"]["source"] == "server"
    assert svc.db.count_messages(aid) == 5
    assert _untouched(path, small_pst)                       # на месте, время изменения прежнее
    detail = [row["detail"] for row in svc.db.query("SELECT detail FROM audit WHERE action='import_pst'")]
    assert len(detail) == 1 and name in detail[0] and "из каталога импорта" in detail[0]
    # обычной формой (application/x-www-form-urlencoded) — тоже можно
    svc.queue.stop()
    again = _params(svc, client.post(f"/api/accounts/{aid}/import-pst", data={"server_file": name}))
    assert again["source"] == "server" and again["target"] == "imap" and again["target_prefix"] == "Импорт PST"
    assert again["server_file"] == name and again["fingerprint"] == params["fingerprint"]
    # журнал задания (его видит и сотрудник, вошедший по ящику) — без путей сервера
    events = client.get(f"/api/jobs/{job_id}/events").json()
    assert events and not any(svc.cfg.data_dir in e["message"] or "удалите" in e["message"] for e in events)


def test_api_job_refuses_replaced_server_file(web, small_pst):
    """Файл заменили после постановки задания: задание проваливается с понятной ошибкой, письма не залиты."""
    client, aid, svc = web
    svc.queue.stop()
    name = "Outlook.pst"
    path = shutil.copy(small_pst, os.path.join(svc.cfg.import_dir, name))
    r = client.post(f"/api/accounts/{aid}/import-pst", data={"server_file": name, "target": "local"})
    job_id = r.json()["job_id"]
    with open(path, "ab") as fh:                                  # под тем же именем уже другой файл
        fh.write(b"\0" * 1024)
    svc.queue.start()
    deadline = time.time() + 60
    while time.time() < deadline and svc.db.get_job(job_id)["status"] not in ("success", "partial", "failed"):
        time.sleep(0.2)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed" and "изменился после постановки задания" in job["error"]
    assert "запустите импорт заново" in job["error"] and svc.cfg.data_dir not in job["error"]
    assert svc.db.count_messages(aid) == 0 and os.path.getsize(path) == os.path.getsize(small_pst) + 1024


@pytest.mark.parametrize("name", ["../x.pst", "/etc/passwd", "a/b.pst", "x.pst\0", "..", ".hidden.pst",
                                  "link.pst", "dir.pst", "empty.pst", "gone.pst", "notes.txt",
                                  "../mailarchiver.db", "../tmp/x.pst", "..\\x.pst"])
def test_api_rejects_names_outside_import_directory(web, name):
    client, aid, svc = web
    svc.queue.stop()
    import_dir = svc.cfg.import_dir
    _put(svc.cfg.data_dir, "x.pst")
    _put(svc.cfg.tmp_dir, "x.pst")
    _put(import_dir, ".hidden.pst")
    _put(import_dir, "notes.txt")
    _put(import_dir, "empty.pst", b"")
    os.mkdir(os.path.join(import_dir, "dir.pst"))
    os.mkdir(os.path.join(import_dir, "a"))
    _put(os.path.join(import_dir, "a"), "b.pst")
    os.symlink(os.path.join(svc.cfg.data_dir, "x.pst"), os.path.join(import_dir, "link.pst"))
    r = client.post(f"/api/accounts/{aid}/import-pst", data={"server_file": name, "target": "local"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "validation_error" and body["message"] and body["hint"]
    assert not _import_jobs(svc)
    assert not svc.db.query("SELECT 1 FROM audit WHERE action='import_pst'")


def test_api_needs_exactly_one_source(web, small_pst):
    client, aid, svc = web
    svc.queue.stop()
    shutil.copy(small_pst, os.path.join(svc.cfg.import_dir, "server.pst"))
    data = open(small_pst, "rb").read()
    url = f"/api/accounts/{aid}/import-pst"
    real_file = ('name="file"; filename="Outlook.pst"', data, "application/octet-stream")
    # ничего не выбрано — понятный отказ, а не «422, поле file не указано»
    nothing = ({"data": {"target": "local"}},                                      # обычная форма
               {"data": {"target": "local", "server_file": ""}},
               _multipart(_field("target", "local")),                              # только поля
               _multipart(EMPTY_FILE),                                             # пустое поле файла и больше ничего
               _multipart(EMPTY_FILE, _field("target", "local")),
               _multipart(EMPTY_FILE, _field("server_file", ""), _field("target", "local")))
    for request_kwargs in nothing:
        r = client.post(url, **request_kwargs)
        assert r.status_code == 400 and r.json()["message"] == "Файл для импорта не выбран.", (request_kwargs, r.text)
    # и то, и другое — тоже отказ
    for request_kwargs in (_multipart(real_file, _field("server_file", "server.pst")),
                           _multipart(_field("server_file", "server.pst"), real_file)):
        r = client.post(url, **request_kwargs)
        assert r.status_code == 400 and "дважды" in r.json()["message"], r.text
    assert not _import_jobs(svc) and not [n for n in os.listdir(svc.cfg.tmp_dir) if n.startswith("import_")]
    # файл с сервера: только поля (так шлёт окно импорта) …
    r = client.post(url, **_multipart(_field("server_file", "server.pst"), _field("target", "local")))
    assert _params(svc, r)["source"] == "server"
    # … и пустое поле файла рядом с ним (так шлёт обычная форма браузера)
    for request_kwargs in (_multipart(EMPTY_FILE, _field("server_file", "server.pst")),
                           _multipart(_field("server_file", "server.pst"), EMPTY_FILE)):
        params = _params(svc, client.post(url, **request_kwargs))
        assert params["source"] == "server" and params["server_file"] == "server.pst" and "pst_path" not in params
    # обычной формой (application/x-www-form-urlencoded) — тоже файл с сервера
    assert _params(svc, client.post(url, data={"server_file": "server.pst"}))["source"] == "server"
    # настоящий файл — загрузка
    params = _params(svc, client.post(url, **_multipart(real_file, _field("target", "local"))))
    assert params["source"] == "upload" and os.path.getsize(params["pst_path"]) == len(data)
    assert "server_file" not in params and "fingerprint" not in params
    assert not [n for n in os.listdir(svc.cfg.import_dir) if n != "server.pst"]


def test_api_upload_still_works_and_accepts_ost(web, small_pst):
    client, aid, svc = web
    svc.queue.stop()
    data = open(small_pst, "rb").read()
    url = f"/api/accounts/{aid}/import-pst"

    def upload(filename, **form):
        return client.post(url, data=form, files={"file": (filename, data, "application/octet-stream")})

    params = _params(svc, upload("Outlook.pst", target="local"))
    assert params["source"] == "upload" and os.path.dirname(params["pst_path"]) == svc.cfg.tmp_dir
    assert os.path.basename(params["pst_path"]).startswith(f"import_{aid}_") and params["pst_path"].endswith(".pst")
    assert os.path.getsize(params["pst_path"]) == len(data)
    params = _params(svc, upload("Почта Иванова.OST", target="local"))
    assert params["source"] == "upload" and params["pst_path"].endswith(".OST")
    assert not inbox.protected(svc.cfg.import_dir, params["pst_path"])
    for filename in ("notes.txt", "mail.pst.exe", "pst", "archive.zip"):
        r = upload(filename, target="local")
        assert r.status_code == 400 and ".pst или .ost" in r.json()["message"], (filename, r.text)
    detail = [row["detail"] for row in svc.db.query("SELECT detail FROM audit WHERE action='import_pst' ORDER BY id")]
    assert len(detail) == 2 and all("загружен через интерфейс" in text for text in detail)
    # загруженные файлы в списке «на сервере» не показываются
    assert client.get("/api/import-files").json()["files"] == []


def test_api_import_files_is_for_administrator_only(web, small_pst):
    from fastapi.testclient import TestClient
    from mailarchiver.security import sign_value
    from mailarchiver.web.auth import COOKIE_NAME
    client, aid, svc = web
    svc.queue.stop()
    shutil.copy(small_pst, os.path.join(svc.cfg.import_dir, "server.pst"))
    form = {"server_file": "server.pst", "target": "local"}

    anonymous = TestClient(client.app, headers=HDR)
    assert anonymous.get("/api/import-files").status_code == 401
    assert anonymous.post(f"/api/accounts/{aid}/import-pst", data=form).status_code == 401

    # сотрудник, вошедший по своему ящику
    svc.db.create_session("mbtoken", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=aid)
    mailbox = TestClient(client.app, headers=HDR)
    mailbox.cookies.set(COOKIE_NAME, sign_value("mbtoken", svc.cfg.secret_key()))
    assert mailbox.get("/api/me").json()["role"] == "mailbox"
    assert mailbox.get("/api/import-files").status_code == 403
    assert mailbox.post(f"/api/accounts/{aid}/import-pst", data=form).status_code == 403
    assert mailbox.post(f"/api/accounts/{aid}/import-pst", data={"target": "local"}).status_code == 403

    # оператор
    assert client.post("/api/users", json={"username": "op", "password": PW, "role": "operator"}).status_code == 200
    operator = TestClient(client.app, headers=HDR)
    assert operator.post("/api/login", json={"username": "op", "password": PW}).status_code == 200
    assert operator.get("/api/import-files").status_code == 403
    assert operator.post(f"/api/accounts/{aid}/import-pst", data=form).status_code == 403

    assert not _import_jobs(svc)
    assert client.get("/api/import-files").json()["files"][0]["name"] == "server.pst"
    # подсказка к выбору источника отдаётся интерфейсу
    assert "source" in client.get("/api/help").json()["import"]
