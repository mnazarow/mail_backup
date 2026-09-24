"""
Движок экспорта в .pst через Aspose.Email (Python via .NET).

Это НАДЁЖНЫЙ путь получения корректного .pst для всех версий Outlook:
  * Unicode PST — Outlook 2003 и новее (рекомендуется, размер до десятков ГБ);
  * ANSI PST    — Outlook 97–2002 (ограничение 2 ГБ).

Библиотека коммерческая. В бесплатном (evaluation) режиме действуют
ограничения (не более 50 писем на папку и водяные знаки), поэтому для
промышленного использования нужна лицензия Aspose. Путь к файлу лицензии
задаётся параметром ``export.aspose_license_path`` или в настройках задания.

Модуль не требует Aspose на этапе импорта: если библиотека не установлена,
движок просто помечается как недоступный (:meth:`available`).
"""
from __future__ import annotations

import os
import tempfile
from typing import Iterable, Optional

from ..errors import PstEngineError
from ..logging_setup import get_logger
from .base import CancelCB, ExportEngine, ExportResult, MailItem, ProgressCB, folder_to_fs

log = get_logger("export.aspose")


def _try_import():
    """Импортировать нужные символы Aspose.Email или вернуть None."""
    try:
        import aspose.email as ae  # noqa: F401
        from aspose.email.storage.pst import PersonalStorage, FileFormatVersion
        from aspose.email.mapi import MapiMessage
        return {"ae": ae, "PersonalStorage": PersonalStorage,
                "FileFormatVersion": FileFormatVersion, "MapiMessage": MapiMessage}
    except Exception:  # noqa: BLE001
        return None


class AsposePstExportEngine(ExportEngine):
    name = "aspose"
    fmt = "pst"
    experimental = False

    @classmethod
    def available(cls):
        mod = _try_import()
        if mod is None:
            return False, ("Библиотека Aspose.Email не установлена. Установите: "
                           "pip install Aspose.Email-for-Python-via-NET (требуется .NET Runtime).")
        return True, ""

    def _apply_license(self, mod, license_path: str) -> bool:
        if not license_path:
            return False
        if not os.path.isfile(license_path):
            raise PstEngineError(f"Файл лицензии Aspose не найден: {license_path}",
                                 hint="Проверьте путь export.aspose_license_path.")
        try:
            from aspose.email import License
            License().set_license(license_path)
            return True
        except Exception as exc:  # noqa: BLE001
            raise PstEngineError(f"Не удалось применить лицензию Aspose: {exc}", cause=exc) from exc

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None, progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None, total_hint: int = 0) -> ExportResult:
        options = options or {}
        mod = _try_import()
        if mod is None:
            raise PstEngineError("Aspose.Email недоступна.",
                                 hint="Установите Aspose.Email-for-Python-via-NET или выберите движок native/eml/mbox.")
        PersonalStorage = mod["PersonalStorage"]
        FileFormatVersion = mod["FileFormatVersion"]
        MapiMessage = mod["MapiMessage"]

        result = ExportResult(path=out_path, is_dir=False, engine=self.name, fmt=self.fmt)
        licensed = self._apply_license(mod, options.get("aspose_license_path", ""))
        if not licensed:
            result.warning = ("Aspose работает в бесплатном режиме: не более 50 писем на папку и "
                              "водяные знаки. Для полного экспорта укажите лицензию Aspose.")

        version = (options.get("pst_format", "unicode") or "unicode").lower()
        fmt_version = FileFormatVersion.ANSI if version == "ansi" else FileFormatVersion.UNICODE

        tmp_dir = options.get("tmp_dir") or tempfile.gettempdir()
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

        pst = None
        try:
            pst = PersonalStorage.create(out_path, fmt_version)
            folder_cache = {"": pst.root_folder}

            def get_folder(imap_folder: str):
                key = folder_to_fs(imap_folder).replace(os.sep, "/")
                if key in folder_cache:
                    return folder_cache[key]
                parent = pst.root_folder
                accum = ""
                for part in key.split("/"):
                    accum = f"{accum}/{part}" if accum else part
                    if accum in folder_cache:
                        parent = folder_cache[accum]
                        continue
                    try:
                        parent = parent.add_sub_folder(part)
                    except Exception as exc:  # noqa: BLE001
                        # папка уже существует или недопустимое имя
                        try:
                            parent = parent.get_sub_folder(part)
                        except Exception as exc2:  # noqa: BLE001
                            raise PstEngineError(f"Не удалось создать папку PST «{part}»: {exc2}", cause=exc) from exc
                    folder_cache[accum] = parent
                return parent

            for item in items:
                if cancel_cb and cancel_cb():
                    break
                folder = get_folder(item.folder)
                # Уникальное имя на каждое письмо: параллельные экспорты в одном
                # процессе (потоки очереди) получали одинаковое имя вида
                # aspose_<pid>_<N>.eml — и в PST одного сотрудника попадали
                # письма другого.
                fd, tmp_eml = tempfile.mkstemp(prefix="aspose_", suffix=".eml", dir=tmp_dir)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(item.raw)
                    mapi = self._load_mapi(MapiMessage, tmp_eml)
                    folder.add_message(mapi)
                    result.count += 1
                    result.bytes_written += len(item.raw)
                except Exception as exc:  # noqa: BLE001
                    result.errors += 1
                    result.error_details.append(f"{item.folder}: {exc}")
                    log.warning("Aspose: ошибка добавления письма: %s", exc)
                finally:
                    try:
                        os.unlink(tmp_eml)
                    except OSError:
                        pass
                if progress_cb and result.count % 20 == 0:
                    progress_cb(result.count, total_hint, f"PST(Aspose): {result.count}")
        except PstEngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PstEngineError(f"Ошибка создания PST через Aspose: {exc}",
                                 hint="Проверьте установку .NET Runtime и корректность параметров.", cause=exc) from exc
        finally:
            if pst is not None:
                try:
                    pst.dispose()
                except Exception:  # noqa: BLE001
                    pass

        result.bytes_written = os.path.getsize(out_path) if os.path.exists(out_path) else result.bytes_written
        if progress_cb:
            progress_cb(result.count, total_hint or result.count, "PST(Aspose): готово")
        return result

    @staticmethod
    def _load_mapi(MapiMessage, eml_path: str):
        # Пытаемся напрямую, затем через MailMessage (разные версии API)
        try:
            return MapiMessage.load(eml_path)
        except Exception:  # noqa: BLE001
            from aspose.email import MailMessage
            mail = MailMessage.load(eml_path)
            return MapiMessage.from_mail_message(mail)
