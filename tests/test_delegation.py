"""Общие и делегированные ящики (1.22.0): чтение и полный доступ, срок, увольнение, «Все входящие»."""
import pytest

import test_webmail as base

from mailarchiver import delegation, models
from mailarchiver.errors import ValidationError

world, box, api = base.world, base.box, base.api
smtp = base.smtp


def test_read_only_and_full_access(api):
    admin, mb, svc, a, b = api
    # сотрудник ящика a (Иванов) получает доступ к чужому ящику b
    emp = int(svc.db.get_account(a).employee_id)
    assert mb.get(f"/api/accounts/{b}/messages?folder=INBOX").status_code == 403
    r = admin.post(f"/api/accounts/{b}/delegates", json={"employee_id": emp, "rights": "read"})
    assert r.status_code == 200 and r.json()["items"][0]["rights"] == "read"
    me = mb.get("/api/me").json()
    assert {x["id"]: x["rights"] for x in me["accounts"]}[b] == "read"
    pk = svc.db.query_one("SELECT id FROM messages WHERE account_id=? LIMIT 1", (b,))
    if pk is not None:
        assert mb.get(f"/api/accounts/{b}/messages/{pk['id']}").status_code == 200        # читать — можно
    st = mb.get(f"/api/webmail/status?account_id={b}").json()
    assert st["can_act"] is False and "только для чтения" in st["act_problem"] and not st["mark_read"]
    assert mb.post(f"/api/accounts/{b}/export", json={"format": "eml"}).status_code == 403
    assert mb.post(f"/api/accounts/{b}/backup", json={}).status_code == 403
    # полный доступ — как свой ящик
    admin.post(f"/api/accounts/{b}/delegates", json={"employee_id": emp, "rights": "full"})
    assert mb.get(f"/api/webmail/status?account_id={b}").json()["can_act"] is True
    # снятие — сразу
    admin.delete(f"/api/accounts/{b}/delegates/{emp}")
    assert mb.get(f"/api/webmail/status?account_id={b}").status_code == 403
    # срок и увольнение
    admin.post(f"/api/accounts/{b}/delegates", json={"employee_id": emp, "rights": "read", "until": "2000-01-01"})
    assert b not in [x["id"] for x in mb.get("/api/me").json()["accounts"]]
    admin.post(f"/api/accounts/{b}/delegates", json={"employee_id": emp, "rights": "read"})
    assert b in [x["id"] for x in mb.get("/api/me").json()["accounts"]]
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='mailbox_delegate'")
    assert mb.post(f"/api/accounts/{b}/delegates", json={"employee_id": emp}).status_code == 403    # только админ


def test_grant_checks_and_unified(services):
    svc = services
    a = svc.db.create_account(models.Account(name="Свой", host="h", username="own@x.ru", password="p"))
    b = svc.db.create_account(models.Account(name="info@", host="h", username="info@x.ru", password="p"))
    emp = svc.db.create_employee(full_name="Иванов", email="own@x.ru")
    svc.db.link_employee_account(emp, a)
    with pytest.raises(ValidationError):
        delegation.grant(svc, "adm", a, emp)                     # свой ящик
    with pytest.raises(ValidationError):
        delegation.grant(svc, "adm", b, emp, rights="owner")
    delegation.grant(svc, "adm", b, emp, "full", note="общий ящик")
    ids, _ = svc.db.mailbox_scope(a)
    assert ids == [a, b] and svc.db.mailbox_delegated(a) == {b: "full"}
    for aid, subj, ts in ((a, "Своё", "2026-11-10T10:00:00+00:00"), (b, "Общее", "2026-11-11T10:00:00+00:00")):
        svc.db.add_message_index_batch([(aid, "INBOX", 1, 1, f"<{subj}@x>", 10, ts, "", f"p{aid}", "", subj, "x@y", 0)])
    res = delegation.unified_inbox(svc, ids)
    assert [m["subject"] for m in res["items"]] == ["Общее", "Своё"] and res["items"][0]["account"] == "info@"
    svc.db.update_employee(emp, status="dismissed")
    assert svc.db.mailbox_delegated(a) == {}
