"""Тесты движков экспорта, включая валидацию нативного PST через readpst."""
import os
import shutil
import subprocess

import pytest

from mailarchiver.export import resolve_engine, list_engines
from mailarchiver.export.base import MailItem


def _items(n=5, folders=("INBOX", "Work")):
    items = []
    for i in range(1, n + 1):
        folder = folders[i % len(folders)]
        raw = (f"From: a{i}@ex.com\r\nTo: b@ex.com\r\nSubject: Тема {i}\r\n"
               f"Message-ID: <m{i}@ex.com>\r\nDate: Mon, 01 Jan 2024 10:00:00 +0000\r\n"
               f"Content-Type: text/plain; charset=utf-8\r\n\r\nТело {i}. Проверка.\r\n").encode()
        items.append(MailItem(folder=folder, raw=raw, internaldate=1700000000 + i,
                              message_id=f"<m{i}@ex.com>", size=len(raw)))
    return items


def test_engines_listed():
    names = {e["name"] for e in list_engines()}
    assert {"eml", "mbox", "native", "aspose"} <= names


def test_eml_export(tmp_path):
    out = str(tmp_path / "eml")
    res = resolve_engine("eml").export(iter(_items(6)), out, total_hint=6)
    assert res.count == 6 and res.is_dir
    files = [f for _r, _d, fs in os.walk(out) for f in fs]
    assert len(files) == 6


def test_mbox_export(tmp_path):
    out = str(tmp_path / "mbox")
    res = resolve_engine("mbox").export(iter(_items(6)), out, total_hint=6)
    assert res.count == 6
    mboxes = [f for _r, _d, fs in os.walk(out) for f in fs if f.endswith(".mbox")]
    assert len(mboxes) >= 1


def test_native_pst_builds(tmp_path):
    out = str(tmp_path / "test.pst")
    res = resolve_engine("native").export(iter(_items(10)), out, total_hint=10)
    assert res.count == 10
    assert os.path.getsize(out) > 512  # заголовок + данные
    assert res.warning  # помечен как экспериментальный


@pytest.mark.skipif(shutil.which("readpst") is None, reason="readpst не установлен")
def test_native_pst_readpst_roundtrip(tmp_path):
    """Ключевой тест: созданный PST читается стандартным readpst (libpst)."""
    pst = str(tmp_path / "rt.pst")
    n = 30
    resolve_engine("native").export(iter(_items(n)), pst, total_hint=n)
    outdir = str(tmp_path / "extracted")
    os.makedirs(outdir)
    proc = subprocess.run(["readpst", "-e", "-o", outdir, pst], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"readpst failed: {proc.stdout} {proc.stderr}"
    eml = [f for _r, _d, fs in os.walk(outdir) for f in fs if f.endswith(".eml")]
    assert len(eml) == n, f"ожидалось {n} писем, извлечено {len(eml)}"
