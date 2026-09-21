r"""
Тесты списка папок и открытия папок по IMAP — на заглушке почтового клиента.

Проверяем три беды, пойманные на боевом сервере (Axigen, mx.vodokomfort.ru):
  * сервер вернул одну и ту же папку в LIST дважды — копировать её надо ОДИН
    раз, иначе счётчики и трафик удваиваются;
  * папка-контейнер (\Noselect / \NonExistent, флаг в любом регистре)
    пропускается молча, без «ошибки»;
  * отказ SELECT повторяется один раз, после чего папка попадает в
    skipped_folders, а в тексте ошибки виден ФАКТИЧЕСКИЙ ответ сервера.

Сеть не нужна: подменяется только самый нижний слой (клиент imapclient),
весь остальной код — настоящий.
"""
import types

from imapclient.exceptions import IMAPClientError

from mailarchiver import models
from mailarchiver.imap import backup as backup_mod
from mailarchiver.imap import client as client_mod

RAW = (b"From: a@example.com\r\nSubject: test\r\nMessage-ID: <1@example.com>\r\n\r\n"
       b"\xd1\x82\xd0\xb5\xd0\xbb\xd0\xbe")  # «тело»


class FakeIMAP:
    """Заглушка imapclient: отвечает заранее заданными данными и считает вызовы."""

    def __init__(self, folders, messages=None, select_errors=None, uidvalidity=1000,
                 status_messages=None):
        self.folders = folders                    # [(флаги, разделитель, имя)]
        self.messages = messages or {}            # {папка: {uid: письмо}}
        # {папка: [исключение | None, …]} — по одному элементу на попытку SELECT;
        # когда список кончился, папка открывается нормально.
        self.select_errors = {k: list(v) for k, v in (select_errors or {}).items()}
        # {папка: сколько писем показывает STATUS}. Папки, которой тут нет,
        # сервер по STATUS не отвечает — как настоящий сервер на битой папке.
        self.status_messages = dict(status_messages or {})
        self.uidvalidity = uidvalidity
        self.select_calls = []
        self.status_calls = []
        self.current = None
        self._imap = types.SimpleNamespace(untagged_responses={})

    # -- то, что вызывает ImapConnection -----------------------------------
    def capabilities(self):
        return [b"IMAP4REV1"]

    def list_folders(self, directory="", pattern="*"):
        if pattern and pattern != "*":
            # Точная проверка имени: сервер отвечает только на своё имя.
            return [f for f in self.folders if f[2] == pattern]
        return list(self.folders)

    def select_folder(self, folder, readonly=False):
        self.select_calls.append(folder)
        queue = self.select_errors.get(folder)
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc
        self.current = folder
        msgs = self.messages.get(folder, {})
        return {b"UIDVALIDITY": self.uidvalidity, b"UIDNEXT": 9999, b"EXISTS": len(msgs)}

    def folder_status(self, folder, what):
        self.status_calls.append(folder)
        if folder not in self.status_messages:
            raise IMAPClientError("STATUS failed")
        return {b"MESSAGES": int(self.status_messages[folder]),
                b"UIDNEXT": 9999, b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria):
        return sorted(self.messages.get(self.current, {}))

    def fetch(self, uids, fields):
        msgs = self.messages.get(self.current, {})
        out = {}
        for uid in uids:
            raw = msgs.get(uid)
            if raw is None:
                continue
            if b"BODY.PEEK[]" in fields:
                out[uid] = {b"BODY[]": raw, b"FLAGS": (b"\\Seen",),
                            b"INTERNALDATE": None, b"RFC822.SIZE": len(raw)}
            else:
                out[uid] = {b"RFC822.SIZE": len(raw)}
        return out

    def logout(self):
        return b"BYE"

    def shutdown(self):
        pass


class FakeDB:
    """Минимальная БД в памяти: состояние папок и индекс писем."""

    def __init__(self):
        self.folder_state = {}
        self.indexed = []

    def get_folder_state(self, account_id, folder):
        return self.folder_state.get((account_id, folder))

    def upsert_folder_state(self, account_id, folder, uidvalidity, last_uid, msg_count):
        self.folder_state[(account_id, folder)] = {
            "uidvalidity": uidvalidity, "last_uid": last_uid, "msg_count": msg_count,
        }

    def existing_uids(self, account_id, folder, uidvalidity):
        return {r["uid"] for r in self.indexed
                if r["folder"] == folder and r["uidvalidity"] == uidvalidity}

    def add_message_index(self, account_id, folder, uidvalidity, uid, message_id, size,
                          internaldate, flags, stored_path, sha256,
                          subject="", from_addr="", has_attach=0):
        self.indexed.append({"folder": folder, "uidvalidity": uidvalidity, "uid": uid})


class FakeStore:
    """Хранилище-заглушка: запоминает сохранения, файлы не пишет."""

    def __init__(self):
        self.stored = []

    def store_message(self, account_id, folder, delimiter, uid, raw, flags=(), internaldate=None):
        self.stored.append((folder, uid))
        return f"cur/{folder}-{uid}", "sha256", len(raw)


def _patch_connection(monkeypatch, fake):
    """Подменить соединение: сеть не нужна, разбор ответов — настоящий."""

    class _Conn(client_mod.ImapConnection):
        def connect(self):
            self.client = fake

        def close(self, *, force=False):
            self.client = None

    monkeypatch.setattr(backup_mod, "ImapConnection", _Conn)
    monkeypatch.setattr(client_mod, "ImapConnection", _Conn)
    monkeypatch.setattr(client_mod, "SELECT_RETRY_DELAY_S", 0)  # тесты не спят
    return _Conn


def _run_backup(monkeypatch, fake, **engine_kw):
    """Прогнать бэкап на заглушке. Возвращает (результат, БД, хранилище, журнал)."""
    _patch_connection(monkeypatch, fake)
    db, store = FakeDB(), FakeStore()
    engine = backup_mod.BackupEngine(db, store, client_mod.ConnectOptions(), **engine_kw)
    acc = models.Account(id=1, name="Ящик", host="mx.example.org", username="u", password="p")
    events = []
    res = engine.run(acc, event_cb=lambda level, msg: events.append((level, msg)))
    return res, db, store, events


def _messages(*names):
    return {name: {i: RAW for i in range(1, 3)} for name in names}


# ---------------------------------------------------------------------------
#  (а) дубликат папки в LIST обрабатывается один раз
# ---------------------------------------------------------------------------
def test_duplicate_folder_in_list_processed_once(monkeypatch):
    fake = FakeIMAP(
        folders=[
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Отправленные"),
            ((b"\\HasNoChildren",), b"/", "Отправленные"),  # сервер повторил запись
        ],
        messages=_messages("INBOX", "Отправленные"),
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.folders_total == 2  # дубль снят, порядок сервера сохранён
    # SELECT на папку ровно дважды: планирование + загрузка (а не четырежды)
    assert fake.select_calls.count("Отправленные") == 2
    # счётчики не удвоены
    assert res.messages_total == 4 and res.messages_new == 4
    assert [m for lvl, m in events if m.startswith("Новых писем к загрузке")] == \
           ["Новых писем к загрузке: 4"]
    assert sum(1 for _lvl, m in events if "«Отправленные»: загрузка" in m) == 1
    # письма сохранены по одному разу — без «сирот» в Maildir
    assert store.stored.count(("Отправленные", 1)) == 1
    assert len(db.indexed) == 4
    # причина видна в журнале задания
    assert any("повторно" in m for lvl, m in events if lvl == "WARNING")
    assert res.errors == 0 and res.status_label == "success"


def test_folders_differing_only_in_case_are_kept(monkeypatch):
    """Разный регистр — это могут быть РАЗНЫЕ папки: склеивать их нельзя."""
    fake = FakeIMAP(
        folders=[
            ((b"\\HasNoChildren",), b"/", "Отправленные"),
            ((b"\\HasNoChildren",), b"/", "отправленные"),
        ],
        messages=_messages("Отправленные", "отправленные"),
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.folders_total == 2 and res.messages_new == 4
    assert {f for f, _uid in store.stored} == {"Отправленные", "отправленные"}
    # но администратора предупреждаем: возможно, это одна и та же папка
    assert any("различающиеся только регистром" in m for lvl, m in events if lvl == "WARNING")


# ---------------------------------------------------------------------------
#  (б) папка с \Noselect пропускается без ошибки
# ---------------------------------------------------------------------------
def test_noselect_folder_skipped_without_error(monkeypatch):
    fake = FakeIMAP(
        folders=[
            ((b"\\Noselect", b"\\HasChildren"), b"/", "Отправленные"),
            ((b"\\NoSelect",), b"/", "Архив"),        # тот же флаг в другом регистре
            ((b"\\NONEXISTENT",), b"/", "Удалённая"),
            ((b"\\HasNoChildren",), b"/", "Отправленные/s2018"),
        ],
        messages=_messages("Отправленные/s2018"),
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.folders_total == 1  # осталась только настоящая папка с письмами
    assert fake.select_calls == ["Отправленные/s2018", "Отправленные/s2018"]
    # пропуск контейнера — не ошибка и не «непрочитанная папка»
    assert res.errors == 0 and res.skipped_folders == []
    assert res.messages_new == 2 and res.status_label == "success"
    assert not any("Пропуск папки" in m for _lvl, m in events)


def test_list_folders_marks_unselectable_flags(monkeypatch):
    r"""Регистр флага значения не имеет: \Noselect, \NoSelect, \NOSELECT."""
    fake = FakeIMAP(folders=[
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\Noselect",), b"/", "A"),
        ((b"\\NoSelect",), b"/", "B"),
        ((b"\\NOSELECT",), b"/", "C"),
        ((b"\\NonExistent",), b"/", "D"),
    ])
    conn_cls = _patch_connection(monkeypatch, fake)
    acc = models.Account(id=1, name="Ящик", host="h", username="u", password="p")
    with conn_cls(acc) as conn:
        selectable = {fi.name: fi.selectable for fi in conn.list_folders()}
    assert selectable == {"INBOX": True, "A": False, "B": False, "C": False, "D": False}


# ---------------------------------------------------------------------------
#  (в) отказ SELECT: повтор, пропущенная папка в результате, ответ сервера
# ---------------------------------------------------------------------------
def _axigen_refusal():
    # ровно то, что отдаёт imapclient на «NO failed EXAMINE» от Axigen
    return IMAPClientError("select failed: failed EXAMINE")


def test_select_failure_is_retried_and_reported(monkeypatch):
    fake = FakeIMAP(
        folders=[
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Отправленные/s2018"),
        ],
        messages=_messages("INBOX"),
        # три отказа: два EXAMINE и запасная попытка обычным SELECT
        select_errors={"Отправленные/s2018": [_axigen_refusal()] * 3},
        status_messages={"Отправленные/s2018": 7},   # письма в папке есть — это потеря
    )
    # сервер объяснил причину в untagged-строке — её тоже нельзя терять
    fake._imap.untagged_responses = {"NO": [b"[ALERT] mailbox is locked by another session"]}

    res, db, store, events = _run_backup(monkeypatch, fake)

    # две попытки EXAMINE и одна запасная SELECT; папка в план не попала
    assert fake.select_calls.count("Отправленные/s2018") == 3
    assert res.skipped_folders == ["Отправленные/s2018"]
    assert res.errors == 1 and res.folders_read == 1

    detail = res.error_details[0]
    assert "Отправленные/s2018" in detail
    assert "попыток: 2" in detail
    assert "Ответ сервера: «failed EXAMINE»" in detail          # слова сервера как есть
    assert "STATUS сервер сообщает: писем 7" in detail           # сколько писем потеряно
    assert "mailbox is locked by another session" in detail      # и его untagged-строка
    assert "Что проверить на сервере:" in detail                 # подсказка администратору

    # копия неполная — это видно в статусе и в итоговой строке
    assert res.status_label == "partial"
    final = [m for _lvl, m in events if m.startswith("Бэкап завершён")][-1]
    assert "не удалось прочитать папки (1): Отправленные/s2018" in final
    assert [lvl for lvl, m in events if m.startswith("Бэкап завершён")] == ["WARNING"]
    # остальной ящик скопирован
    assert res.messages_new == 2


def test_select_retry_recovers_after_temporary_refusal(monkeypatch):
    """Первый отказ временный: вторая попытка открывает папку, потерь нет."""
    fake = FakeIMAP(
        folders=[((b"\\HasNoChildren",), b"/", "Отправленные/S2019")],
        messages=_messages("Отправленные/S2019"),
        select_errors={"Отправленные/S2019": [_axigen_refusal()]},
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert fake.select_calls.count("Отправленные/S2019") == 3  # 2 на план + 1 на загрузку
    assert res.skipped_folders == [] and res.errors == 0
    assert res.messages_new == 2 and res.status_label == "success"


def test_select_error_message_keeps_server_words(monkeypatch):
    """Прямая проверка select(): ошибка несёт ответ сервера и подсказку."""
    fake = FakeIMAP(folders=[], select_errors={"X": [_axigen_refusal()] * 3})
    conn_cls = _patch_connection(monkeypatch, fake)
    acc = models.Account(id=1, name="Ящик", host="h", username="u", password="p")
    from mailarchiver.errors import ImapProtocolError

    with conn_cls(acc) as conn:
        try:
            conn.select("X", readonly=True)
        except ImapProtocolError as exc:
            assert "failed EXAMINE" in exc.message
            assert exc.hint and "ACL" in exc.hint
        else:  # pragma: no cover - ошибка обязана подняться
            raise AssertionError("select() должен был бросить ошибку")
    assert fake.select_calls == ["X", "X", "X"]   # 2 × EXAMINE + запасной SELECT


# ---------------------------------------------------------------------------
#  диагностика: проверка подключения показывает дубли LIST
# ---------------------------------------------------------------------------
def test_probe_account_marks_duplicate_folders(monkeypatch):
    fake = FakeIMAP(folders=[
        ((b"\\HasNoChildren",), b"/", "INBOX"),
        ((b"\\HasNoChildren",), b"/", "Отправленные"),
        ((b"\\HasNoChildren",), b"/", "Отправленные"),
        ((b"\\Noselect", b"\\HasChildren"), b"/", "Архив"),
    ])
    _patch_connection(monkeypatch, fake)
    acc = models.Account(id=1, name="Ящик", host="h", username="u", password="p")
    res = client_mod.probe_account(acc)

    assert res["ok"] is True
    assert res["duplicate_folders"] == ["Отправленные"]
    dupes = [f["duplicate"] for f in res["folders"] if f["name"] == "Отправленные"]
    assert dupes == [False, True]
    archive = [f for f in res["folders"] if f["name"] == "Архив"][0]
    # по флагам видно, ПОЧЕМУ папка не копируется — SELECT для этого не нужен
    assert archive["selectable"] is False and "\\Noselect" in archive["flags"]


# ---------------------------------------------------------------------------
#  (г) папка не открывается по EXAMINE: запасной SELECT, STATUS и пустые папки
# ---------------------------------------------------------------------------
def test_examine_refused_but_plain_select_works(monkeypatch):
    """Axigen отвечает «failed EXAMINE», но обычный SELECT папку открывает.

    Раньше такая папка объявлялась непрочитанной и копия — неполной, хотя
    письма были доступны. Читать через SELECT безопасно: письма скачиваются
    командой BODY.PEEK, флаг \\Seen на сервере не ставится.
    """
    fake = FakeIMAP(
        folders=[((b"\\HasNoChildren",), b"/", "Отправленные/s2022")],
        messages=_messages("Отправленные/s2022"),
        select_errors={"Отправленные/s2022": [_axigen_refusal(), _axigen_refusal()]},
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.skipped_folders == [] and res.errors == 0
    assert res.messages_new == 2 and res.status_label == "success"
    assert fake.select_calls.count("Отправленные/s2022") == 4   # 2 EXAMINE + SELECT + загрузка


def test_unreadable_but_empty_folder_is_not_a_loss(monkeypatch):
    """Папка не открывается, но по STATUS в ней 0 писем — терять нечего.

    Так выглядят битые папки, оставшиеся на сервере: объявлять из-за них копию
    неполной на каждом прогоне неправильно, администратор перестанет замечать
    настоящие пропажи.
    """
    fake = FakeIMAP(
        folders=[
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren",), b"/", "Отправленные/s2022_000"),
        ],
        messages=_messages("INBOX"),
        select_errors={"Отправленные/s2022_000": [_axigen_refusal()] * 3},
        status_messages={"Отправленные/s2022_000": 0},
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.errors == 0 and res.skipped_folders == []
    assert res.empty_unreadable_folders == ["Отправленные/s2022_000"]
    assert res.status_label == "success"
    assert res.messages_new == 2                       # остальной ящик скопирован
    warned = [m for lvl, m in events if lvl == "WARNING" and "s2022_000" in m]
    assert warned and "ПУСТАЯ" in warned[0]
    final = [m for _lvl, m in events if m.startswith("Бэкап завершён")][-1]
    assert "писем в них нет" in final
    assert "КОПИЯ НЕПОЛНАЯ" not in final


def test_unreadable_folder_hints_at_server_made_duplicate(monkeypatch):
    """Имя вида «s2022_000» рядом с «s2022» — дубликат, созданный сервером."""
    fake = FakeIMAP(
        folders=[
            ((b"\\HasNoChildren",), b"/", "Отправленные/s2022"),
            ((b"\\HasNoChildren",), b"/", "Отправленные/s2022_000"),
        ],
        messages=_messages("Отправленные/s2022"),
        select_errors={"Отправленные/s2022_000": [_axigen_refusal()] * 3},
        status_messages={"Отправленные/s2022_000": 4},
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.skipped_folders == ["Отправленные/s2022_000"] and res.errors == 1
    skip = [m for lvl, m in events if lvl == "WARNING" and m.startswith("Пропуск папки")][0]
    assert "Похоже на дубликат папки «Отправленные/s2022»" in skip
    assert "STATUS сервер сообщает: писем 4" in skip
    # подсказка объясняет и как перестать получать «неполную копию»
    assert "Пропускать папки" in skip


def test_status_unavailable_keeps_folder_as_loss(monkeypatch):
    """Сервер молчит и на STATUS — считаем, что письма потеряны, и говорим об этом."""
    fake = FakeIMAP(
        folders=[((b"\\HasNoChildren",), b"/", "Архив/битая")],
        select_errors={"Архив/битая": [_axigen_refusal()] * 3},
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    assert res.skipped_folders == ["Архив/битая"] and res.errors == 1
    assert res.empty_unreadable_folders == []
    assert res.status_label == "failed"                # прочитать не удалось ничего
    final = [m for _lvl, m in events if m.startswith("Бэкап завершён")][-1]
    assert "КОПИЯ НЕПОЛНАЯ" in final


def test_select_error_names_every_attempt(monkeypatch):
    """Отчёт об отказе перечисляет всё, что пробовали: этим он и полезен.

    Администратору почтового сервера нужно показать, что клиент не «не умеет»
    открывать папку, а перебрал все способы: EXAMINE, обычный SELECT, STATUS и
    LIST по точному имени.
    """
    fake = FakeIMAP(
        folders=[((b"\\HasNoChildren",), b"/", "Отправленные/s2022")],
        select_errors={"Отправленные/s2022": [_axigen_refusal()] * 3},
    )
    res, db, store, events = _run_backup(monkeypatch, fake)

    detail = res.error_details[0]
    assert "EXAMINE — отказ (попыток: 2)" in detail
    assert "обычный SELECT — отказ" in detail
    assert "на команду STATUS сервер тоже не ответил" in detail
    # сервер имя знает — значит битая сама папка, а не имя
    assert "имя верное, а сама папка на сервере нерабочая" in detail


def test_select_error_detects_name_mismatch(monkeypatch):
    """LIST по точному имени не находит папку — значит шлём не то имя.

    Так выглядит беда с кодировкой или невидимым символом: сервер показал имя
    в общем списке, но своим его не признаёт.
    """
    hidden = "Отправленные/s2022 "          # NBSP на конце имени
    fake = FakeIMAP(
        folders=[((b"\\HasNoChildren",), b"/", hidden)],
        select_errors={hidden: [_axigen_refusal()] * 3},
    )
    # сервер знает папку под именем БЕЗ невидимого символа
    fake.folders_exact = []
    monkeypatch.setattr(fake, "list_folders",
                        lambda directory="", pattern="*": ([] if pattern != "*" else list(fake.folders)))
    res, db, store, events = _run_backup(monkeypatch, fake)

    detail = res.error_details[0]
    assert "LIST по точному имени эту папку НЕ находит" in detail
    assert "невидимый символ NBSP" in detail
