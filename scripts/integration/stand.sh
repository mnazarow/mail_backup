#!/usr/bin/env bash
# Стенд интеграционных тестов MailArchiver (1.22.0): настоящие Dovecot (IMAP, LMTP, ManageSieve, master users),
# Postfix (SMTP, журналирование always_bcc), OpenLDAP (memberOf) и Radicale (CalDAV, CardDAV) — на этой машине,
# без Docker. Нужен root (sudo) и пакеты:
#   apt-get install dovecot-imapd dovecot-lmtpd dovecot-managesieved dovecot-sieve postfix slapd ldap-utils
#   pip install radicale
# Запуск:   sudo scripts/integration/stand.sh start      (данные — в /tmp/ma-stand или $MA_STAND_DIR)
#           MA_IT=1 python -m pytest tests/integration
#           sudo scripts/integration/stand.sh stop
# Postfix настраивается в системном экземпляре (/etc/postfix) — запускайте стенд на тестовой машине (CI).
set -euo pipefail

DIR="${MA_STAND_DIR:-/tmp/ma-stand}"
PY="${MA_STAND_PYTHON:-python3}"
IMAP_PORT=1143; LMTP_PORT=24024; SIEVE_PORT=14190; SMTP_PORT=2526; LDAP_PORT=3389; DAV_PORT=5232
JOURNAL_PORT="${MA_IT_JOURNAL_PORT:-2525}"
PASS="Passw0rd-1"; MASTER_PASS="Master-Pass-1"

die() { echo "stand: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "нужен root (sudo)"

start_dovecot() {
  id vmail >/dev/null 2>&1 || useradd -r -M -s /usr/sbin/nologin vmail
  mkdir -p "$DIR/mail" "$DIR/dovecot-run"
  chown -R vmail:vmail "$DIR/mail"
  : > "$DIR/users"
  for u in ivanov petrov sidorova director; do
    echo "$u@corp.test:{PLAIN}$PASS::::::" >> "$DIR/users"
  done
  echo "archiver:{PLAIN}$MASTER_PASS::::::" > "$DIR/master-users"
  cat > "$DIR/dovecot.conf" <<EOF
protocols = imap lmtp sieve
listen = 127.0.0.1
base_dir = $DIR/dovecot-run
state_dir = $DIR/dovecot-run/state
log_path = $DIR/dovecot.log
ssl = no
disable_plaintext_auth = no
auth_mechanisms = plain login
auth_master_user_separator = *
mail_location = maildir:~/Maildir
first_valid_uid = 1
mail_plugins = \$mail_plugins
passdb {
  driver = passwd-file
  master = yes
  args = $DIR/master-users
}
passdb {
  driver = passwd-file
  args = scheme=PLAIN username_format=%u $DIR/users
}
userdb {
  driver = static
  args = uid=vmail gid=vmail home=$DIR/mail/%d/%n
}
namespace inbox {
  inbox = yes
  separator = /
  mailbox Drafts {
    special_use = \\Drafts
    auto = subscribe
  }
  mailbox Sent {
    special_use = \\Sent
    auto = subscribe
  }
  mailbox Trash {
    special_use = \\Trash
    auto = subscribe
  }
  mailbox Junk {
    special_use = \\Junk
    auto = subscribe
  }
  mailbox Archive {
    special_use = \\Archive
    auto = subscribe
  }
}
service imap-login {
  inet_listener imap {
    address = 127.0.0.1
    port = $IMAP_PORT
  }
  inet_listener imaps {
    port = 0
  }
}
service lmtp {
  inet_listener lmtp {
    address = 127.0.0.1
    port = $LMTP_PORT
  }
}
service managesieve-login {
  inet_listener sieve {
    address = 127.0.0.1
    port = $SIEVE_PORT
  }
}
protocol lmtp {
  mail_plugins = \$mail_plugins sieve
  postmaster_address = postmaster@corp.test
}
protocol imap {
  mail_max_userip_connections = 50
}
plugin {
  sieve = file:~/sieve;active=~/.dovecot.sieve
}
EOF
  dovecot -c "$DIR/dovecot.conf"
}

start_postfix() {
  postconf -e "inet_interfaces = loopback-only" "mydestination = localhost" "myhostname = mx.test" \
    "mynetworks = 127.0.0.0/8 [::1]/128" "smtpd_relay_restrictions = permit_mynetworks, reject" \
    "virtual_mailbox_domains = corp.test" "virtual_transport = lmtp:inet:127.0.0.1:$LMTP_PORT" \
    "always_bcc = journal@archive.test" "transport_maps = texthash:$DIR/transport" \
    "smtp_tls_security_level = none" "compatibility_level = 3.6" "maillog_file_prefixes = /var, /dev/stdout, $DIR" "maillog_file = $DIR/postfix.log"
  echo "archive.test smtp:[127.0.0.1]:$JOURNAL_PORT" > "$DIR/transport"
  if ! grep -q "^127.0.0.1:$SMTP_PORT " /etc/postfix/master.cf; then
    echo "127.0.0.1:$SMTP_PORT inet n - n - - smtpd" >> /etc/postfix/master.cf
  fi
  postfix check || true
  postfix start 2>/dev/null || postfix reload
}

start_ldap() {
  rm -rf "$DIR/ldap-db"; mkdir -p "$DIR/ldap-db"
  cat > "$DIR/slapd.conf" <<EOF
include /etc/ldap/schema/core.schema
include /etc/ldap/schema/cosine.schema
include /etc/ldap/schema/inetorgperson.schema
include /etc/ldap/schema/nis.schema
modulepath /usr/lib/ldap
moduleload back_mdb
moduleload memberof
pidfile $DIR/slapd.pid
database mdb
suffix "dc=test"
rootdn "cn=admin,dc=test"
rootpw admin-pass
directory $DIR/ldap-db
overlay memberof
EOF
  slapd -f "$DIR/slapd.conf" -h "ldap://127.0.0.1:$LDAP_PORT/"
  for _ in $(seq 1 50); do
    ldapsearch -x -H "ldap://127.0.0.1:$LDAP_PORT" -b "" -s base >/dev/null 2>&1 && break; sleep 0.2
  done
  ldapadd -x -H "ldap://127.0.0.1:$LDAP_PORT" -D "cn=admin,dc=test" -w admin-pass >/dev/null <<EOF
dn: dc=test
objectClass: dcObject
objectClass: organization
o: Test
dc: test

dn: ou=people,dc=test
objectClass: organizationalUnit
ou: people

dn: ou=groups,dc=test
objectClass: organizationalUnit
ou: groups

dn: uid=svc,ou=people,dc=test
objectClass: inetOrgPerson
uid: svc
cn: Service
sn: Service
userPassword: svc-pass

dn: uid=anna,ou=people,dc=test
objectClass: inetOrgPerson
uid: anna
cn: Анна Админова
sn: Админова
givenName: Анна
mail: anna@corp.test
title: Начальник ИТ
departmentNumber: ИТ
userPassword: $PASS

dn: uid=bob,ou=people,dc=test
objectClass: inetOrgPerson
uid: bob
cn: Борис Операторов
sn: Операторов
mail: bob@corp.test
userPassword: $PASS

dn: uid=ivanov,ou=people,dc=test
objectClass: inetOrgPerson
uid: ivanov
cn: Иван Иванов
sn: Иванов
givenName: Иван
mail: ivanov@corp.test
telephoneNumber: +7 495 000-00-01
employeeNumber: 1001
userPassword: $PASS

dn: cn=archive-admins,ou=groups,dc=test
objectClass: groupOfNames
cn: archive-admins
member: uid=anna,ou=people,dc=test

dn: cn=archive-operators,ou=groups,dc=test
objectClass: groupOfNames
cn: archive-operators
member: uid=bob,ou=people,dc=test
EOF
}

start_radicale() {
  mkdir -p "$DIR/radicale"
  : > "$DIR/htpasswd"
  for u in ivanov petrov; do echo "$u@corp.test:$PASS" >> "$DIR/htpasswd"; done
  nohup "$PY" -m radicale --server-hosts "127.0.0.1:$DAV_PORT" --storage-filesystem-folder "$DIR/radicale" \
    --auth-type htpasswd --auth-htpasswd-filename "$DIR/htpasswd" --auth-htpasswd-encryption plain \
    --logging-level warning > "$DIR/radicale.log" 2>&1 &
  echo $! > "$DIR/radicale.pid"
}

wait_port() {
  for _ in $(seq 1 100); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0; sleep 0.2
  done
  die "порт $1 не открылся ($2)"
}

stop_all() {
  [ -f "$DIR/dovecot.conf" ] && dovecot -c "$DIR/dovecot.conf" stop 2>/dev/null || true
  postfix stop 2>/dev/null || true
  [ -f "$DIR/slapd.pid" ] && kill "$(cat "$DIR/slapd.pid")" 2>/dev/null || true
  [ -f "$DIR/radicale.pid" ] && kill "$(cat "$DIR/radicale.pid")" 2>/dev/null || true
  sleep 1
}

case "${1:-}" in
  start)
    stop_all
    # Ubuntu: профили AppArmor slapd и Dovecot не пускают к файлам в /tmp — на тестовой машине их снимаем
    if command -v apparmor_parser >/dev/null 2>&1; then
      for p in /etc/apparmor.d/usr.sbin.slapd /etc/apparmor.d/dovecot* /etc/apparmor.d/usr.lib.dovecot.*; do
        [ -f "$p" ] && apparmor_parser -R "$p" 2>/dev/null || true
      done
    fi
    rm -rf "$DIR"; mkdir -p "$DIR"; chmod 755 "$DIR"
    start_dovecot; start_postfix; start_ldap; start_radicale
    wait_port $IMAP_PORT dovecot; wait_port $LMTP_PORT lmtp; wait_port $SIEVE_PORT managesieve
    wait_port $SMTP_PORT postfix; wait_port $LDAP_PORT slapd; wait_port $DAV_PORT radicale
    cat > "$DIR/env" <<EOF
MA_IT=1
MA_IT_IMAP=127.0.0.1:$IMAP_PORT
MA_IT_SMTP=127.0.0.1:$SMTP_PORT
MA_IT_SIEVE=127.0.0.1:$SIEVE_PORT
MA_IT_LDAP=ldap://127.0.0.1:$LDAP_PORT
MA_IT_DAV=http://127.0.0.1:$DAV_PORT
MA_IT_JOURNAL_PORT=$JOURNAL_PORT
MA_IT_PASS=$PASS
MA_IT_MASTER=archiver:$MASTER_PASS
MA_IT_DIR=$DIR
EOF
    chmod 644 "$DIR/env"
    echo "стенд запущен: $DIR/env"
    ;;
  stop) stop_all; echo "стенд остановлен" ;;
  status)
    for p in $IMAP_PORT $LMTP_PORT $SIEVE_PORT $SMTP_PORT $LDAP_PORT $DAV_PORT; do
      (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && echo "$p открыт" || echo "$p закрыт"
    done ;;
  *) echo "использование: $0 start|stop|status" >&2; exit 2 ;;
esac
