"""
Учётная запись почтового сервера ↔ ящик архива.

Ящик архива принадлежит учётной записи подключения, если:

1. он копируется «через подключение» именно этого сервера (``auth_type =
   server`` и ``server_id``) и логин — адрес учётной записи (или её
   псевдоним);
2. иначе — логин совпадает с адресом, а сервер ящика — IMAP-сервер
   подключения;
3. иначе — просто логин совпадает с адресом (ящик заведён вручную).

Сначала — точные адреса (и удалённых учётных записей: архив уволенного остаётся
за ним), потом — псевдонимы живых учётных записей без адресов, которые
принадлежали другим учётным записям (переданных коллеге), потом — псевдонимы
удалённых. Один ящик архива достаётся одной учётной записи; ящики, которые
копируются через подключение к другому серверу, чужим не достаются.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..models import Account, AuthType

Key = Tuple[int, str]


def inherited(rows: Iterable[Dict]) -> Set[str]:
    """Адреса всех учётных записей и псевдонимы удалённых — у живой учётной записи такой псевдоним чужой."""
    out: Set[str] = set()
    for row in rows:
        out.add(str(row["email"]).strip().lower())
        if row.get("deleted_at"):
            out.update(str(a).strip().lower() for a in row.get("aliases") or [])
    return out


def _score(acc: Account, server_id: int, imap_host: str) -> int:
    if acc.auth_type == AuthType.SERVER:
        return 3 if getattr(acc, "server_id", None) == server_id else -1
    if acc.auth_type == AuthType.AXIGEN_FTP:
        return -1
    if imap_host and (acc.host or "").strip().lower() == imap_host:
        return 2
    return 1


def match(rows_by_server: Dict[int, List[Dict]], accounts: Sequence[Account], hosts: Dict[int, str],
          taken: Optional[Set[int]] = None) -> Dict[Key, Account]:
    """Учётные записи подключений → ящики архива. ``taken`` — уже занятые (Axigen) ящики."""
    taken = set(taken or ())
    by_login: Dict[str, List[Account]] = {}
    for acc in accounts:
        if acc.id in taken:
            continue
        login = (acc.username or "").strip().lower()
        if login:
            by_login.setdefault(login, []).append(acc)
    out: Dict[Key, Account] = {}

    def pick(address: str, server_id: int) -> Optional[Account]:
        best, best_score = None, 0
        for acc in by_login.get(address, ()):
            if acc.id in taken:
                continue
            score = _score(acc, server_id, hosts.get(server_id, ""))
            if score > best_score:
                best, best_score = acc, score
        return best

    all_rows = [r for rows in rows_by_server.values() for r in rows]
    foreign = inherited(all_rows)
    # 1) точные адреса — сначала явные связи (через подключение), потом по серверу, потом прочие
    candidates = []
    for server_id, rows in rows_by_server.items():
        for row in rows:
            for acc in by_login.get(row["email"], ()):
                score = _score(acc, server_id, hosts.get(server_id, ""))
                if score > 0:
                    candidates.append((-score, server_id, row["email"], acc))
    for _neg, server_id, email, acc in sorted(candidates, key=lambda c: (c[0], c[1], c[2], c[3].id or 0)):
        key = (server_id, email)
        if key in out or acc.id in taken:
            continue
        out[key] = acc
        taken.add(acc.id)
    # 2) псевдонимы живых, 3) псевдонимы удалённых
    live_emails = {str(r["email"]).lower() for r in all_rows if not r.get("deleted_at")}
    for deleted_pass in (False, True):
        for server_id, rows in rows_by_server.items():
            for row in rows:
                if bool(row.get("deleted_at")) != deleted_pass or (server_id, row["email"]) in out:
                    continue
                skip = live_emails if deleted_pass else foreign
                for alias in row.get("aliases") or []:
                    alias = str(alias).strip().lower()
                    if alias in skip:
                        continue
                    acc = pick(alias, server_id)
                    if acc is not None:
                        out[(server_id, row["email"])] = acc
                        taken.add(acc.id)
                        break
    return out
