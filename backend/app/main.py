"""
Resolve Project Server – Backend

Verwaltet einen PostgreSQL-Container für DaVinci Resolve über die Docker-API.
Alle Datenbankbefehle laufen per `docker exec` IM Postgres-Container. Dadurch
passen pg_dump / pg_restore / psql immer exakt zur laufenden Server-Version,
und das Backend braucht selbst keinen Netzwerkzugang zur Datenbank.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import docker
from docker.errors import ImageNotFound, NotFound
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

APP_VERSION = "1.0.0"


# =========================================================================== #
# Konfiguration
# =========================================================================== #
def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"Umgebungsvariable {name} fehlt – siehe .env.example")
    return value  # type: ignore[return-value]


DATA = Path("/data")
HOST_DATA = env("HOST_DATA_DIR", required=True).rstrip("/")
PG_USER = env("POSTGRES_USER", "postgres")
PG_PASSWORD = env("POSTGRES_PASSWORD", required=True)
PG_AUTH = env("PG_AUTH_METHOD", "md5")
PG_PORT = int(env("PG_PUBLIC_PORT", "5432"))
PG_BIND = env("PG_BIND_ADDRESS", "0.0.0.0")
PG_DEFAULT = env("PG_VERSION", "13")
TZ = env("TZ", "Europe/Berlin")

PG_UID = 999  # UID des postgres-Users in den offiziellen Debian-Images
NET = "rps_database"
LABEL = "resolve-project-server"

CFG, BK, PGD, HOOKS = DATA / "config", DATA / "backups", DATA / "postgres", DATA / "hooks"
for _d in (CFG, BK / "db", BK / "full", PGD, HOOKS):
    _d.mkdir(parents=True, exist_ok=True)
for _d in (BK, BK / "db", BK / "full"):
    os.chown(_d, PG_UID, PG_UID)  # Postgres schreibt die Dumps selbst
CFG.chmod(0o700)


def _load_secret() -> str:
    if env("SECRET_KEY"):
        return env("SECRET_KEY")
    f = CFG / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


SECRET = _load_secret()

# =========================================================================== #
# Persistente JSON-Dateien auf dem Host-Mount
# =========================================================================== #
_io = threading.Lock()

DEFAULT_SETTINGS = {
    "server_name": "Resolve Project Server",
    "public_host": env("PUBLIC_HOST", ""),
    "retention_days": 14,
    "keep_min": 3,
    "full_backup": True,      # nächtliche Vollsicherung inkl. Rollen/Passwörter
    "full_backup_hour": 4,
}
DEFAULT_STATE = {"pg_version": PG_DEFAULT, "history": [], "last_full_backup": None}


def _load(name: str, default: dict) -> dict:
    p = CFG / name
    with _io:
        if p.exists():
            try:
                return {**default, **json.loads(p.read_text())}
            except json.JSONDecodeError:
                pass
        return json.loads(json.dumps(default))


def _save(name: str, data: dict) -> None:
    p = CFG / name
    tmp = p.with_suffix(".tmp")
    with _io:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(p)


def settings() -> dict:
    return _load("settings.json", DEFAULT_SETTINGS)


def state() -> dict:
    return _load("state.json", DEFAULT_STATE)


# Bibliotheks-Register: Zugang (Rolle/Passwort), Zeitplan, Hooks je Bibliothek.
# Bibliotheken, die direkt in Resolve angelegt wurden, stehen hier ggf. ohne Rolle.
def libraries() -> dict:
    return _load("libraries.json", {})


def save_libraries(data: dict) -> None:
    _save("libraries.json", data)


def lib_entry(name: str) -> dict:
    e = libraries().get(name) or {}
    e.setdefault("role", None)
    e.setdefault("password", None)
    e.setdefault("schedule", {"mode": "off", "hour": 2, "weekday": 0})
    e.setdefault("hooks", False)
    e.setdefault("last_auto", None)
    return e


def update_lib(name: str, **changes) -> dict:
    libs = libraries()
    e = {**lib_entry(name), **libs.get(name, {}), **changes}
    libs[name] = e
    save_libraries(libs)
    return e


def remove_lib(name: str) -> None:
    libs = libraries()
    libs.pop(name, None)
    save_libraries(libs)


# =========================================================================== #
# Hilfsfunktionen
# =========================================================================== #
def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def fs_name(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s).strip("_") or "db"


NEW_LIB_RE = re.compile(r"^[A-Za-z0-9_]{1,63}$")          # für neu angelegte Bibliotheken
DB_NAME_RE = re.compile(r"^[\w][\w\- .]{0,62}$")           # auch in Resolve angelegte
VERSION_RE = re.compile(r"^(9\.[56]|1\d)(\.\d{1,2})?$")
RESERVED = {"postgres", "template0", "template1", PG_USER}


def sanitize_lib_name(raw: str) -> str:
    return re.sub(r"\s+", "_", raw.strip())


def check_db_name(name: str) -> str:
    if not DB_NAME_RE.fullmatch(name) or name in RESERVED:
        raise HTTPException(400, "Ungültiger Bibliotheksname.")
    return name


def qi(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def ql(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def pg_major(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2]) if parts[0] == "9" else parts[0]


def random_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


# =========================================================================== #
# Docker / Postgres
# =========================================================================== #
dk = docker.from_env()


def cname(major: str, staging: bool = False) -> str:
    return f"rps-postgres-{major.replace('.', '-')}" + ("-staging" if staging else "")


def get_c(name: str):
    try:
        return dk.containers.get(name)
    except NotFound:
        return None


def ensure_network() -> None:
    try:
        dk.networks.get(NET)
    except NotFound:
        dk.networks.create(NET, driver="bridge", labels={"managed-by": LABEL})


def ensure_image(version: str, log: Callable[[str], None] = print) -> str:
    image = f"postgres:{version}"
    try:
        dk.images.get(image)
    except ImageNotFound:
        log(f"Lade Image {image} herunter …")
        dk.images.pull("postgres", tag=version)
    return image


def run_in(c, cmd: list[str]) -> tuple[int, str, str]:
    r = c.exec_run(cmd, user="postgres", demux=True)
    out, err = r.output if isinstance(r.output, tuple) else (r.output, None)
    return r.exit_code, (out or b"").decode(errors="replace"), (err or b"").decode(errors="replace")


def pexec(cmd: list[str], c=None) -> str:
    code, out, err = run_in(c or pg(), cmd)
    if code != 0:
        raise RuntimeError((err or out).strip() or f"{cmd[0]} ist fehlgeschlagen (Code {code}).")
    return out


def sql(query: str, db: str = "postgres", c=None) -> str:
    return pexec(["psql", "-U", PG_USER, "-d", db, "-X", "-v", "ON_ERROR_STOP=1",
                  "-At", "-F", "\t", "-c", query], c)


def sql_many(statements: list[str], db: str = "postgres", c=None) -> str:
    """Mehrere Befehle in einem einzigen `docker exec`. Jedes -c läuft in einer eigenen
    Transaktion (nötig für CREATE DATABASE); beim ersten Fehler wird abgebrochen."""
    cmd = ["psql", "-U", PG_USER, "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-F", "\t"]
    for st in statements:
        cmd += ["-c", st]
    return pexec(cmd, c)


def wait_ready(c, timeout: int = 180) -> None:
    start = time.time()
    while time.time() - start < timeout:
        c.reload()
        if c.status in ("exited", "dead"):
            logs = c.logs(tail=40).decode(errors="replace")
            raise RuntimeError(f"Postgres-Container wurde beendet:\n{logs}")
        try:
            # -h 127.0.0.1: der temporäre Init-Server lauscht nur auf dem Socket,
            # so warten wir wirklich auf den endgültigen Server.
            code, _, _ = run_in(c, ["pg_isready", "-h", "127.0.0.1", "-U", PG_USER])
            if code == 0:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    raise RuntimeError("Zeitüberschreitung beim Warten auf PostgreSQL.")


def create_pg(version: str, staging: bool = False, log: Callable[[str], None] = print):
    """Erstellt (bzw. ersetzt) den Postgres-Container für `version`.
    Die Daten liegen IMMER auf dem Host: <DATA_DIR>/postgres/<major>."""
    ensure_network()
    image = ensure_image(version, log)
    major = pg_major(version)
    (PGD / major).mkdir(parents=True, exist_ok=True)
    name = cname(major, staging)
    old = get_c(name)
    if old:
        old.remove(force=True)

    legacy = major.startswith("9.")
    auth = "md5" if legacy else PG_AUTH
    command = ["postgres", "-c", "listen_addresses=*", "-c", "max_connections=250"]
    if not legacy:
        command += ["-c", f"password_encryption={'scram-sha-256' if auth == 'scram-sha-256' else 'md5'}"]

    kwargs = dict(
        image=image, name=name, detach=True, command=command,
        environment={
            "POSTGRES_USER": PG_USER,
            "POSTGRES_PASSWORD": PG_PASSWORD,
            "POSTGRES_HOST_AUTH_METHOD": auth,
            "POSTGRES_INITDB_ARGS": f"--auth-host={auth}",
            "PGDATA": "/var/lib/postgresql/data",
            "TZ": TZ,
        },
        volumes={
            f"{HOST_DATA}/postgres/{major}": {"bind": "/var/lib/postgresql/data", "mode": "rw"},
            f"{HOST_DATA}/backups": {"bind": "/backups", "mode": "rw"},
        },
        labels={"managed-by": LABEL, "pg-major": major, "staging": str(staging).lower()},
        network=NET,
    )
    if not staging:
        kwargs["ports"] = {"5432/tcp": (PG_BIND, PG_PORT)}
        kwargs["restart_policy"] = {"Name": "unless-stopped"}
    log(f"Starte {name} ({image}) …")
    c = dk.containers.run(**kwargs)
    wait_ready(c)
    log(f"{name} ist bereit.")
    return c


def pg():
    """Aktueller, laufender Postgres-Container – ohne ihn neu zu erstellen."""
    c = get_c(cname(pg_major(state()["pg_version"])))
    if not c:
        raise HTTPException(503, "Der Datenbankserver ist noch nicht eingerichtet.")
    if c.status != "running":
        raise HTTPException(503, "Der Datenbankserver läuft gerade nicht (evtl. läuft ein Versionswechsel).")
    return c


def ensure_pg(log: Callable[[str], None] = print):
    version = state()["pg_version"]
    current = cname(pg_major(version))
    for other in dk.containers.list(all=True, filters={"label": f"managed-by={LABEL}"}):
        if other.name != current and other.status == "running":
            log(f"Stoppe {other.name}")
            other.stop()
    c = get_c(current)
    if c is None:
        return create_pg(version, log=log)
    if c.status != "running":
        c.start()
        wait_ready(c)
    return c


def db_names(c=None) -> list[str]:
    out = sql("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1", c=c)
    return [line for line in out.splitlines() if line]


def role_exists(role: str, c=None) -> bool:
    return sql(f"SELECT 1 FROM pg_roles WHERE rolname = {ql(role)}", c=c).strip() == "1"


def terminate_connections(name: str, c=None) -> None:
    sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = {ql(name)} AND pid <> pg_backend_pid()", c=c)


def create_library_db(name: str, password: str, c=None) -> None:
    """Legt Rolle + Datenbank in einem einzigen Aufruf an.
    Die Rolle darf sich nur mit ihrer eigenen Bibliothek verbinden."""
    role, pw = qi(name), ql(password)
    try:
        sql_many([
            f"""DO $rps$ BEGIN
                  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = {ql(name)}) THEN
                    RAISE EXCEPTION 'RPS_EXISTS';
                  END IF;
                  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {ql(name)}) THEN
                    EXECUTE 'ALTER ROLE ' || quote_ident({ql(name)}) || ' WITH LOGIN PASSWORD ' || quote_literal({pw});
                  ELSE
                    EXECUTE 'CREATE ROLE ' || quote_ident({ql(name)}) || ' WITH LOGIN PASSWORD ' || quote_literal({pw});
                  END IF;
                END $rps$""",
            f"CREATE DATABASE {role} OWNER {role}",
            f"REVOKE CONNECT, TEMPORARY ON DATABASE {role} FROM PUBLIC",
            f"GRANT CONNECT, TEMPORARY ON DATABASE {role} TO {role}",
        ], c=c or pg())
    except RuntimeError as e:
        if "RPS_EXISTS" in str(e):
            raise HTTPException(409, f"„{name}“ gibt es bereits.")
        raise


# Exakte Zeilenzahl jeder Tabelle – Grundlage der Datenprüfung beim Versionswechsel
COUNT_SQL = """
SELECT n.nspname || '.' || c.relname,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', n.nspname, c.relname),
                                             false, true, '')))[1]::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1
"""


def snapshot(c) -> dict[str, dict[str, str]]:
    snap = {}
    for db in db_names(c):
        rows = sql(COUNT_SQL, db, c).splitlines()
        snap[db] = dict(r.split("\t", 1) for r in rows if "\t" in r)
    return snap


def diff_snapshots(before: dict, after: dict) -> list[str]:
    problems = []
    for db in sorted(set(before) | set(after)):
        if db not in after:
            problems.append(f"Bibliothek fehlt: {db}")
        elif db not in before:
            problems.append(f"Unerwartete Bibliothek: {db}")
        else:
            for table, count in before[db].items():
                if after[db].get(table) != count:
                    problems.append(f"{db}: {table} hat {after[db].get(table, 'keine')} statt {count} Zeilen")
    return problems


# =========================================================================== #
# Hooks (Skripte vor/nach einer Sicherung, liegen auf dem Host unter hooks/)
# =========================================================================== #
HOOK_NAMES = ("pre-backup", "post-backup")
HOOK_EXAMPLE = """#!/bin/sh
# Beispiel-Hook. Umbenennen in „{name}“ (ohne .example), damit er aktiv wird,
# und pro Bibliothek in der Web-UI einschalten.
#
# Verfügbare Variablen:
#   RPS_EVENT            pre-backup | post-backup
#   RPS_LIBRARY          Name der Bibliothek
#   RPS_BACKUP_FILE      Pfad der Sicherung im Backend-Container (/data/backups/…)
#   RPS_BACKUP_HOST_FILE Derselbe Pfad auf dem Host
#   RPS_STATUS           ok | fehler   (nur post-backup)
#
# Ein pre-backup-Hook mit Exit-Code ≠ 0 bricht die Sicherung ab.
echo "$RPS_EVENT für $RPS_LIBRARY: $RPS_BACKUP_FILE ($RPS_STATUS)"
"""
for _h in HOOK_NAMES:
    _ex = HOOKS / f"{_h}.example"
    if not _ex.exists():
        _ex.write_text(HOOK_EXAMPLE.replace("{name}", _h))


def hook_file(event: str) -> Path | None:
    p = HOOKS / event
    return p if p.is_file() else None


def run_hook(event: str, library: str, backup_rel: str, status: str, log: Callable[[str], None]) -> None:
    p = hook_file(event)
    if not p:
        return
    envv = {**os.environ,
            "RPS_EVENT": event, "RPS_LIBRARY": library, "RPS_STATUS": status,
            "RPS_BACKUP_FILE": str(BK / backup_rel),
            "RPS_BACKUP_HOST_FILE": f"{HOST_DATA}/backups/{backup_rel}"}
    log(f"Hook {event} läuft …")
    try:
        r = subprocess.run(["/bin/sh", str(p)], env=envv, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Hook {event} nach 30 Minuten abgebrochen.")
    for line in (r.stdout + r.stderr).strip().splitlines()[-30:]:
        log(f"  {event}: {line}")
    if r.returncode != 0:
        raise RuntimeError(f"Hook {event} endete mit Code {r.returncode}.")


# =========================================================================== #
# Sicherungen
# =========================================================================== #
def meta_path(rel: str) -> Path:
    return BK / (rel + ".json")


def read_meta(rel: str) -> dict:
    p = meta_path(rel)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def write_meta(rel: str, data: dict) -> None:
    meta_path(rel).write_text(json.dumps(data, indent=2, ensure_ascii=False))


def pin(rel: str) -> None:
    m = read_meta(rel)
    m["pinned"] = True
    write_meta(rel, m)


def backup_db(name: str, reason: str = "manuell", c=None, log: Callable[[str], None] = print,
              hooks: bool | None = None) -> str:
    c = c or pg()
    use_hooks = lib_entry(name)["hooks"] if hooks is None else hooks
    folder = BK / "db" / fs_name(name)
    folder.mkdir(exist_ok=True)
    os.chown(folder, PG_UID, PG_UID)
    rel = f"db/{fs_name(name)}/{fs_name(name)}__{stamp()}__{fs_name(reason)}.dump"
    if use_hooks:
        run_hook("pre-backup", name, rel, "", log)
    log(f"Sichere „{name}“ …")
    try:
        pexec(["pg_dump", "-U", PG_USER, "-Fc", "-f", f"/backups/{rel}", name], c)
    except Exception:
        if use_hooks:
            run_hook("post-backup", name, rel, "fehler", log)
        raise
    write_meta(rel, {"kind": "db", "database": name, "reason": reason,
                     "pg_version": state()["pg_version"], "created": now_iso(), "pinned": False})
    log(f"„{name}“ gesichert ({(BK / rel).stat().st_size / 1e6:.1f} MB).")
    if use_hooks:
        run_hook("post-backup", name, rel, "ok", log)
    return rel


def backup_full(reason: str = "manuell", c=None, log: Callable[[str], None] = print) -> str:
    c = c or pg()
    version = state()["pg_version"]
    rel = f"full/server__{stamp()}__pg{pg_major(version)}__{fs_name(reason)}.sql"
    log("Erstelle Vollsicherung aller Bibliotheken inkl. Zugänge …")
    pexec(["pg_dumpall", "-U", PG_USER, "-f", f"/backups/{rel}"], c)
    write_meta(rel, {"kind": "full", "database": None, "reason": reason,
                     "pg_version": version, "created": now_iso(), "pinned": False})
    log(f"Vollsicherung fertig ({(BK / rel).stat().st_size / 1e6:.1f} MB).")
    return rel


def list_backups(database: str | None = None) -> list[dict]:
    items = []
    for p in BK.rglob("*"):
        if not p.is_file() or p.suffix not in (".dump", ".sql"):
            continue
        rel = p.relative_to(BK).as_posix()
        m = read_meta(rel)
        st = p.stat()
        item = {
            "path": rel,
            "kind": m.get("kind", "full" if p.suffix == ".sql" else "db"),
            "database": m.get("database"),
            "reason": m.get("reason", ""),
            "pg_version": m.get("pg_version"),
            "created": m.get("created") or iso(st.st_mtime),
            "size": st.st_size,
            "pinned": bool(m.get("pinned")),
        }
        if database is None or item["database"] == database:
            items.append(item)
    return sorted(items, key=lambda b: b["created"], reverse=True)


def backup_file(rel: str) -> Path:
    p = (BK / rel).resolve()
    if BK.resolve() not in p.parents or not p.is_file() or p.suffix not in (".dump", ".sql"):
        raise HTTPException(404, "Sicherung nicht gefunden.")
    return p


def prune_backups(log: Callable[[str], None] = print) -> int:
    s = settings()
    cutoff = time.time() - int(s["retention_days"]) * 86400
    groups: dict[str, list[dict]] = {}
    for b in list_backups():
        key = b["database"] if b["kind"] == "db" else "__full__"
        groups.setdefault(key or "__unknown__", []).append(b)
    removed = 0
    for items in groups.values():
        for i, b in enumerate(items):  # neueste zuerst
            if i < int(s["keep_min"]) or b["pinned"]:
                continue
            p = BK / b["path"]
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                meta_path(b["path"]).unlink(missing_ok=True)
                removed += 1
                log(f"Gelöscht: {b['path']}")
    log(f"Aufräumen fertig – {removed} alte Sicherung(en) entfernt.")
    return removed


# =========================================================================== #
# Hintergrundaufgaben (immer nur eine schreibende Aufgabe gleichzeitig)
# =========================================================================== #
class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.title = kind, title
        self.status = "läuft"
        self.lines: list[str] = []
        self.started, self.finished = time.time(), None
        self.error: str | None = None

    def log(self, msg: str) -> None:
        self.lines.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "log": self.lines[-400:], "error": self.error,
                "started": iso(self.started), "finished": iso(self.finished) if self.finished else None}


JOBS: dict[str, Job] = {}
JOB_LOCK = threading.Lock()
CURRENT_JOB: dict[str, str | None] = {"id": None}


def start_job(kind: str, title: str, fn: Callable[[Job], object]) -> dict:
    if not JOB_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Es läuft bereits eine Aufgabe. Bitte warte, bis sie fertig ist.")
    job = Job(kind, title)
    JOBS[job.id] = job
    CURRENT_JOB["id"] = job.id

    def worker():
        try:
            fn(job)
            job.status = "fertig"
            job.log("Fertig.")
        except Exception as e:  # noqa: BLE001
            job.status = "fehler"
            job.error = str(e)
            job.log(f"FEHLER: {e}")
        finally:
            job.finished = time.time()
            CURRENT_JOB["id"] = None
            JOB_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    if len(JOBS) > 50:
        for old in sorted(JOBS.values(), key=lambda j: j.started)[:-50]:
            JOBS.pop(old.id, None)
    return {"job": job.id}


def backup_all_dbs(job: Job, reason: str) -> None:
    c = pg()
    names = db_names(c)
    job.log(f"{len(names)} Bibliothek(en) werden gesichert.")
    for n in names:
        backup_db(n, reason, c, job.log)
    backup_full(reason, c, job.log)
    prune_backups(job.log)


# =========================================================================== #
# Versionswechsel ohne Datenverlust
# =========================================================================== #
def upgrade(job: Job, target: str) -> None:
    st = state()
    current = st["pg_version"]
    cur_major, tgt_major = pg_major(current), pg_major(target)
    log = job.log
    old = ensure_pg(log)

    log("Erfasse Tabellen und exakte Zeilenzahlen aller Bibliotheken …")
    before = snapshot(old)
    log(f"{len(before)} Bibliothek(en), {sum(len(t) for t in before.values())} Tabellen erfasst.")
    full = backup_full(f"vor-wechsel-auf-{target}", old, log)
    pin(full)  # nie automatisch löschen
    for n in before:
        pin(backup_db(n, f"vor-wechsel-auf-{target}", old, log, hooks=False))

    ensure_image(target, log)

    if cur_major == tgt_major:
        log("Gleiche Hauptversion: Container wird mit neuem Image neu erstellt, Datenverzeichnis bleibt.")
        old.stop()
        old.remove()
        try:
            new = create_pg(target, log=log)
            problems = diff_snapshots(before, snapshot(new))
            if problems:
                raise RuntimeError("Prüfung fehlgeschlagen:\n" + "\n".join(problems[:20]))
        except Exception:
            log("Rolle zurück auf die vorherige Version …")
            broken = get_c(cname(tgt_major))
            if broken:
                broken.remove(force=True)
            create_pg(current, log=log)
            raise
    else:
        target_dir = PGD / tgt_major
        if target_dir.exists() and any(target_dir.iterdir()):
            aside = PGD / f"{tgt_major}.alt-{stamp()}"
            log(f"Vorhandenes Datenverzeichnis {tgt_major} wird nach {aside.name} verschoben.")
            target_dir.rename(aside)

        log(f"Starte PostgreSQL {target} zunächst intern (ohne Port) zum Einspielen …")
        staging = create_pg(target, staging=True, log=log)
        try:
            log("Spiele Vollsicherung ein (Bibliotheken und Zugänge) …")
            _code, _out, err = run_in(staging, ["psql", "-U", PG_USER, "-d", "postgres", "-X", "-q",
                                                 "-f", f"/backups/{full}"])
            # „role already exists“ u. ä. ist erwartbar – maßgeblich ist die Zeilenprüfung.
            for line in [l for l in err.splitlines() if "ERROR" in l and "already exists" not in l][:15]:
                log(f"psql: {line}")
            log("Prüfe jede Tabelle Zeile für Zeile gegen den Ausgangszustand …")
            problems = diff_snapshots(before, snapshot(staging))
            if problems:
                raise RuntimeError("Daten stimmen nicht überein – Wechsel abgebrochen, alte Version läuft weiter:\n"
                                   + "\n".join(problems[:20]))
            log("Alle Daten vollständig übertragen.")
        except Exception:
            staging.remove(force=True)
            raise
        staging.stop()
        staging.remove()

        log(f"Stoppe PostgreSQL {current} (Daten bleiben unter postgres/{cur_major} erhalten).")
        old.stop()
        try:
            new = create_pg(target, log=log)
            problems = diff_snapshots(before, snapshot(new))
            if problems:
                raise RuntimeError("Abschlussprüfung fehlgeschlagen:\n" + "\n".join(problems[:20]))
        except Exception:
            log("Rolle zurück: alte Version wird wieder gestartet …")
            broken = get_c(cname(tgt_major))
            if broken:
                broken.remove(force=True)
            old.start()
            wait_ready(old)
            raise
        old.remove()

    st = state()
    st["pg_version"] = target
    st["history"] = (st.get("history") or []) + [{"from": current, "to": target, "at": now_iso(), "backup": full}]
    _save("state.json", st)
    log(f"PostgreSQL läuft jetzt in Version {target}.")


# =========================================================================== #
# Start & Zeitplan
# =========================================================================== #
BOOT = {"ready": False, "error": None}


def boot() -> None:
    try:
        ensure_pg()
        BOOT.update(ready=True, error=None)
    except Exception as e:  # noqa: BLE001
        BOOT.update(ready=False, error=str(e))
        print("Start fehlgeschlagen:", e)


def due_key(schedule: dict, now: dt.datetime) -> str | None:
    """Liefert einen Schlüssel für den aktuellen Zeitraum, wenn eine Sicherung fällig ist."""
    mode = schedule.get("mode", "off")
    hour = int(schedule.get("hour", 2))
    if mode == "hourly":
        return now.strftime("%Y-%m-%dT%H")
    if mode == "daily" and now.hour >= hour:
        return now.strftime("%Y-%m-%d")
    if mode == "weekly" and now.weekday() == int(schedule.get("weekday", 0)) and now.hour >= hour:
        return now.strftime("%Y-%m-%d")
    return None


def scheduler() -> None:
    while True:
        time.sleep(60)
        if not BOOT["ready"] or CURRENT_JOB["id"]:
            continue
        try:
            now = dt.datetime.now()
            c = pg()
            existing = set(db_names(c))
            due: list[tuple[str, str]] = []
            for name, entry in libraries().items():
                key = due_key(entry.get("schedule") or {}, now)
                if name in existing and key and entry.get("last_auto") != key:
                    due.append((name, key))
            s, st = settings(), state()
            today = now.strftime("%Y-%m-%d")
            full_due = s["full_backup"] and now.hour >= int(s["full_backup_hour"]) and st.get("last_full_backup") != today
            if not due and not full_due:
                continue

            def run(j: Job, due=due, full_due=full_due, today=today):
                for name, key in due:
                    try:
                        backup_db(name, "automatisch", log=j.log)
                    finally:
                        update_lib(name, last_auto=key)
                if full_due:
                    backup_full("automatisch", log=j.log)
                    st2 = state()
                    st2["last_full_backup"] = today
                    _save("state.json", st2)
                prune_backups(j.log)

            start_job("backup", "Automatische Sicherung", run)
        except HTTPException:
            pass  # Server nicht bereit oder andere Aufgabe läuft – nächste Minute erneut
        except Exception as e:  # noqa: BLE001
            print("Zeitplan-Fehler:", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=boot, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    yield


app = FastAPI(title="Resolve Project Server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# =========================================================================== #
# Konto & Anmeldung
# =========================================================================== #
SESSION_HOURS = 12
_fails: dict[str, list[float]] = {}


def hash_pw(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_pw(password: str, stored: str) -> bool:
    try:
        _algo, salt, h = stored.split("$")
        return hmac.compare_digest(hash_pw(password, bytes.fromhex(salt)).split("$")[2], h)
    except ValueError:
        return False


def account() -> dict:
    a = _load("account.json", {})
    if not a.get("pw_hash"):  # Erststart: Werte aus .env übernehmen
        a = {"username": env("ADMIN_USER", "admin"), "name": "Administrator", "email": "",
             "pw_hash": hash_pw(env("ADMIN_PASSWORD", required=True)), "pw_version": 1}
        _save("account.json", a)
    return a


account()


def make_token(a: dict) -> str:
    payload = f"{a['username']}|{a['pw_version']}|{int(time.time()) + SESSION_HOURS * 3600}"
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        user, ver, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except Exception:  # noqa: BLE001
        return False
    expected = hmac.new(SECRET.encode(), f"{user}|{ver}|{exp}".encode(), hashlib.sha256).hexdigest()
    a = account()
    return (hmac.compare_digest(sig, expected) and user == a["username"]
            and ver == str(a["pw_version"]) and int(exp) > time.time())


def auth(request: Request) -> None:
    if not valid_token(request.cookies.get("rps_session")):
        raise HTTPException(401, "Nicht angemeldet.")


def client_ip(request: Request) -> str:
    # Setzt Caddy (das Frontend) selbst – das Backend ist nur über Caddy erreichbar.
    return (request.headers.get("x-rps-client-ip", "").strip()
            or (request.client.host if request.client else "?"))


def set_session(request: Request, response: Response, a: dict) -> None:
    secure = request.headers.get("x-forwarded-proto", "http") == "https"
    response.set_cookie("rps_session", make_token(a), httponly=True, secure=secure,
                        samesite="strict", max_age=SESSION_HOURS * 3600, path="/")


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    ip, now = client_ip(request), time.time()
    fails = [t for t in _fails.get(ip, []) if now - t < 900]
    if len(fails) >= 5:
        raise HTTPException(429, "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.")
    a = account()
    user_ok = hmac.compare_digest(body.username.encode(), a["username"].encode())
    pass_ok = verify_pw(body.password, a["pw_hash"])
    if not (user_ok and pass_ok):
        _fails[ip] = fails + [now]
        time.sleep(1)
        raise HTTPException(401, "Benutzername oder Passwort ist falsch.")
    _fails.pop(ip, None)
    set_session(request, response, a)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("rps_session", path="/")
    return {"ok": True}


@app.get("/api/account")
def get_account(_=Depends(auth)):
    a = account()
    return {"username": a["username"], "name": a["name"], "email": a["email"]}


class AccountIn(BaseModel):
    username: str
    name: str = ""
    email: str = ""


@app.put("/api/account")
def put_account(body: AccountIn, request: Request, response: Response, _=Depends(auth)):
    if not re.fullmatch(r"[A-Za-z0-9_.\-@]{3,64}", body.username):
        raise HTTPException(400, "Benutzername: 3–64 Zeichen, Buchstaben, Ziffern und _ . - @")
    if body.email and not re.fullmatch(r"[^@\s]+@[^@\s]+", body.email):
        raise HTTPException(400, "Ungültige E-Mail-Adresse.")
    a = account()
    a.update(username=body.username, name=body.name.strip()[:80], email=body.email.strip())
    _save("account.json", a)
    set_session(request, response, a)  # Sitzung bleibt gültig
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/account/password")
def change_password(body: PasswordIn, request: Request, response: Response, _=Depends(auth)):
    a = account()
    if not verify_pw(body.current, a["pw_hash"]):
        raise HTTPException(400, "Das aktuelle Passwort stimmt nicht.")
    if len(body.new) < 10:
        raise HTTPException(400, "Das neue Passwort muss mindestens 10 Zeichen lang sein.")
    a.update(pw_hash=hash_pw(body.new), pw_version=int(a["pw_version"]) + 1)  # andere Sitzungen enden
    _save("account.json", a)
    set_session(request, response, a)
    return {"ok": True}


# =========================================================================== #
# Branding (öffentlich – wird auf der Anmeldeseite gebraucht)
# =========================================================================== #
def logo_file() -> Path | None:
    for ext in ("png", "jpg", "webp"):
        p = CFG / f"logo.{ext}"
        if p.exists():
            return p
    return None


@app.get("/api/branding")
def branding():
    lf = logo_file()
    return {"name": settings()["server_name"], "logo": bool(lf), "logo_v": int(lf.stat().st_mtime) if lf else 0}


@app.get("/api/branding/logo")
def branding_logo():
    lf = logo_file()
    if not lf:
        raise HTTPException(404, "Kein Logo hinterlegt.")
    return FileResponse(lf, headers={"Cache-Control": "no-cache"})


# =========================================================================== #
# Übersicht
# =========================================================================== #
@app.get("/api/status")
def status(_=Depends(auth)):
    st, s = state(), settings()
    c = get_c(cname(pg_major(st["pg_version"])))
    info = {
        "boot": BOOT, "pg_version": st["pg_version"], "container": c.status if c else "fehlt",
        "port": PG_PORT, "public_host": s["public_host"], "server_version": None,
        "connections": None, "db_size": None, "app_version": APP_VERSION,
        "job": JOBS[CURRENT_JOB["id"]].dict() if CURRENT_JOB["id"] in JOBS else None,
    }
    if c and c.status == "running":
        try:
            version, size, conns = sql(
                "SELECT current_setting('server_version'), "
                "(SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database), "
                "(SELECT count(*) FROM pg_stat_activity WHERE datname IS NOT NULL "
                " AND datname <> 'postgres' AND pid <> pg_backend_pid())", c=c).strip().split("\t")
            info.update(server_version=version, db_size=int(size), connections=int(conns))
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)
    bks = list_backups()
    info["backups"] = {"count": len(bks), "size": sum(b["size"] for b in bks),
                       "latest": bks[0]["created"] if bks else None}
    db_disk, bk_disk = shutil.disk_usage(PGD), shutil.disk_usage(BK)
    info["disk"] = {"db_total": db_disk.total, "db_free": db_disk.free,
                    "bk_total": bk_disk.total, "bk_free": bk_disk.free}
    return info


# =========================================================================== #
# Projektbibliotheken
# =========================================================================== #
def connection_for(name: str) -> dict:
    e = lib_entry(name)
    s = settings()
    if e["role"]:
        user, password = e["role"], e["password"]
    else:  # in Resolve angelegt → Admin-Zugang
        user, password = PG_USER, PG_PASSWORD
    return {"host": s["public_host"], "port": PG_PORT, "database": name, "user": user,
            "password": password, "own_access": bool(e["role"])}


def project_counts(names: list[str], c) -> dict[str, int]:
    """Projektanzahl aller Bibliotheken in EINEM psql-Aufruf (wechselt per \\c die Datenbank).
    Bibliotheken ohne Resolve-Tabelle (z. B. frisch angelegt) fehlen einfach im Ergebnis."""
    if not names:
        return {}
    cmd = ["psql", "-U", PG_USER, "-d", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-At", "-F", "\t"]
    for n in names:
        cmd += ["-c", "\\c '" + n.replace("'", "''") + "'",
                "-c", 'SELECT current_database(), count(*) FROM "SM_Project"']
    _code, out, _err = run_in(c, cmd)
    counts = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].isdigit():
            counts[parts[0]] = int(parts[1])
    return counts


@app.get("/api/libraries")
def list_libraries(_=Depends(auth)):
    c = pg()
    rows = sql("SELECT d.datname, pg_database_size(d.datname), "
               "(SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) "
               "FROM pg_database d WHERE NOT d.datistemplate AND d.datname <> 'postgres' "
               "ORDER BY lower(d.datname)", c=c)
    last: dict[str, str] = {}
    for b in list_backups():
        if b["kind"] == "db" and b["database"] and b["database"] not in last:
            last[b["database"]] = b["created"]
    parsed = [line.split("\t") for line in rows.splitlines() if line]
    projects = project_counts([p[0] for p in parsed], c)
    libs = libraries()
    result = []
    for name, size, conns in parsed:
        e = {**lib_entry(name), **libs.get(name, {})}
        result.append({"name": name, "size": int(size), "connections": int(conns), "projects": projects.get(name),
                       "last_backup": last.get(name), "own_access": bool(e["role"]),
                       "schedule": e["schedule"], "hooks": e["hooks"]})
    return result


class CreateLibIn(BaseModel):
    name: str
    password: str


@app.post("/api/libraries")
def create_library(body: CreateLibIn, _=Depends(auth)):
    name = sanitize_lib_name(body.name)
    if not NEW_LIB_RE.fullmatch(name) or name.lower() in RESERVED:
        raise HTTPException(400, "Nur Buchstaben (ohne Umlaute), Ziffern und _ sind erlaubt, höchstens 63 Zeichen.")
    if len(body.password) < 8 or "'" in body.password:
        raise HTTPException(400, "Das Passwort muss mindestens 8 Zeichen haben (ohne Apostroph).")
    create_library_db(name, body.password)
    update_lib(name, role=name, password=body.password, created=now_iso())
    return {"name": name, "connection": connection_for(name)}


@app.get("/api/libraries/{name}/connection")
def library_connection(name: str, _=Depends(auth)):
    check_db_name(name)
    return connection_for(name)


class LibPasswordIn(BaseModel):
    password: str


@app.post("/api/libraries/{name}/password")
def library_password(name: str, body: LibPasswordIn, _=Depends(auth)):
    check_db_name(name)
    if len(body.password) < 8 or "'" in body.password:
        raise HTTPException(400, "Das Passwort muss mindestens 8 Zeichen haben (ohne Apostroph).")
    c = pg()
    if name not in db_names(c):
        raise HTTPException(404, "Bibliothek nicht gefunden.")
    e = lib_entry(name)
    if not e["role"]:
        # In Resolve angelegte Bibliothek: eigenen Zugang einrichten und Rechte vergeben
        if role_exists(name, c):
            raise HTTPException(409, f"Es gibt bereits einen Datenbankbenutzer „{name}“.")
        sql(f"CREATE ROLE {qi(name)} WITH LOGIN PASSWORD {ql(body.password)}", c=c)
        sql(f"GRANT CONNECT, TEMPORARY ON DATABASE {qi(name)} TO {qi(name)}", c=c)
        for stmt in ("GRANT ALL ON SCHEMA public TO {r}",
                     "GRANT ALL ON ALL TABLES IN SCHEMA public TO {r}",
                     "GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {r}",
                     "GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO {r}",
                     f"ALTER DEFAULT PRIVILEGES FOR ROLE {qi(PG_USER)} IN SCHEMA public GRANT ALL ON TABLES TO {{r}}",
                     f"ALTER DEFAULT PRIVILEGES FOR ROLE {qi(PG_USER)} IN SCHEMA public GRANT ALL ON SEQUENCES TO {{r}}"):
            sql(stmt.format(r=qi(name)), name, c)
    else:
        sql(f"ALTER ROLE {qi(e['role'])} WITH PASSWORD {ql(body.password)}", c=c)
    update_lib(name, role=e["role"] or name, password=body.password)
    return {"ok": True}


class ScheduleIn(BaseModel):
    mode: str = "off"       # off | hourly | daily | weekly
    hour: int = 2
    weekday: int = 0        # 0 = Montag
    hooks: bool = False


@app.put("/api/libraries/{name}/schedule")
def library_schedule(name: str, body: ScheduleIn, _=Depends(auth)):
    check_db_name(name)
    if body.mode not in ("off", "hourly", "daily", "weekly") or not 0 <= body.hour <= 23 or not 0 <= body.weekday <= 6:
        raise HTTPException(400, "Ungültiger Zeitplan.")
    update_lib(name, schedule={"mode": body.mode, "hour": body.hour, "weekday": body.weekday},
               hooks=body.hooks, last_auto=None)
    return {"ok": True}


@app.post("/api/libraries/{name}/backup")
def library_backup(name: str, _=Depends(auth)):
    check_db_name(name)
    return start_job("backup", f"Sicherung „{name}“", lambda j: backup_db(name, "manuell", log=j.log))


class VacuumIn(BaseModel):
    full: bool = False


@app.post("/api/libraries/{name}/optimize")
def library_optimize(name: str, body: VacuumIn, _=Depends(auth)):
    check_db_name(name)

    def run(j: Job):
        c = pg()
        if body.full:
            j.log("VACUUM FULL sperrt die Bibliothek während der Laufzeit.")
            terminate_connections(name, c)
        before = int(sql(f"SELECT pg_database_size({ql(name)})", c=c).strip())
        pexec(["vacuumdb", "-U", PG_USER, "-z"] + (["-f"] if body.full else []) + ["-d", name], c)
        after = int(sql(f"SELECT pg_database_size({ql(name)})", c=c).strip())
        j.log(f"„{name}“ optimiert: {before / 1e6:.1f} MB → {after / 1e6:.1f} MB.")

    return start_job("optimize", f"„{name}“ optimieren", run)


@app.delete("/api/libraries/{name}")
def library_delete(name: str, backup: bool = True, _=Depends(auth)):
    check_db_name(name)

    def run(j: Job):
        c = pg()
        if name not in db_names(c):
            raise RuntimeError("Diese Bibliothek existiert nicht (mehr).")
        if backup:
            pin(backup_db(name, "vor-dem-loeschen", c, j.log, hooks=False))
        terminate_connections(name, c)
        sql(f"DROP DATABASE {qi(name)}", c=c)
        role = lib_entry(name)["role"]
        if role and role != PG_USER and role_exists(role, c):
            sql(f"DROP ROLE {qi(role)}", c=c)
        remove_lib(name)
        j.log(f"Bibliothek „{name}“ und ihr Zugang wurden gelöscht.")

    return start_job("delete", f"„{name}“ löschen", run)


# =========================================================================== #
# Zugangsschlüssel (.resolvedbkey)
# =========================================================================== #
# ACHTUNG: Blackmagic dokumentiert das Format nicht. Diese Vorlage ist eine
# Annahme. Über „Vorlage aus echter Datei lernen“ in der Konfiguration lässt
# sich eine aus Resolve exportierte Datei als exakte Vorlage übernehmen.
DEFAULT_KEY_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<DbConnectionKey>
    <Name>{{NAME}}</Name>
    <Type>PostgreSQL</Type>
    <Host>{{HOST}}</Host>
    <Port>{{PORT}}</Port>
    <Database>{{DATABASE}}</Database>
    <User>{{USER}}</User>
    <Password>{{PASSWORD}}</Password>
</DbConnectionKey>
"""
KEY_TPL = CFG / "key_template.txt"


def key_template() -> tuple[str, bool]:
    return (KEY_TPL.read_text(), True) if KEY_TPL.exists() else (DEFAULT_KEY_TEMPLATE, False)


@app.get("/api/libraries/{name}/connect-key")
def connect_key(name: str, _=Depends(auth)):
    check_db_name(name)
    if name not in db_names():
        raise HTTPException(404, "Bibliothek nicht gefunden.")
    conn = connection_for(name)
    if not conn["host"]:
        raise HTTPException(400, "Bitte zuerst in der Konfiguration die Zugangsadresse eintragen.")
    tpl, _learned = key_template()
    values = {"{{NAME}}": name, "{{HOST}}": conn["host"], "{{PORT}}": str(conn["port"]),
              "{{DATABASE}}": name, "{{USER}}": conn["user"], "{{PASSWORD}}": conn["password"],
              "{{SERVER}}": settings()["server_name"]}
    for k, v in values.items():
        tpl = tpl.replace(k, v)
    filename = f"{fs_name(settings()['server_name'])}_{fs_name(name)}.resolvedbkey"
    return Response(tpl, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/settings/key-template")
def get_key_template(_=Depends(auth)):
    tpl, learned = key_template()
    return {"template": tpl, "custom": learned}


class TemplateIn(BaseModel):
    template: str


@app.put("/api/settings/key-template")
def put_key_template(body: TemplateIn, _=Depends(auth)):
    if len(body.template) > 20000:
        raise HTTPException(400, "Vorlage ist zu groß.")
    KEY_TPL.write_text(body.template)
    return {"ok": True}


@app.delete("/api/settings/key-template")
def reset_key_template(_=Depends(auth)):
    KEY_TPL.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/settings/key-template/learn")
async def learn_key_template(database: str = Form(...), file: UploadFile = File(...), _=Depends(auth)):
    raw = await file.read(200_000)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(422, "Die Datei ist kein Klartext (vermutlich verschlüsselt). Automatisches Erzeugen "
                                 "ist damit nicht möglich – nutze die Verbindungsdaten zum Abtippen.")
    conn = connection_for(check_db_name(database))
    pairs = [(conn["password"], "{{PASSWORD}}"), (database, "{{DATABASE}}"), (conn["host"], "{{HOST}}"),
             (conn["user"], "{{USER}}"), (str(conn["port"]), "{{PORT}}")]
    found, missing = [], []
    for value, ph in sorted(pairs, key=lambda p: -len(p[0] or "")):
        if value and value in text:
            text = text.replace(value, ph)
            found.append(ph)
        else:
            missing.append(ph)
    return {"template": text, "found": found, "missing": missing}


# =========================================================================== #
# Sicherungen
# =========================================================================== #
@app.get("/api/backups")
def backups(library: str | None = None, _=Depends(auth)):
    return list_backups(library)


@app.post("/api/backups/all")
def all_backup(_=Depends(auth)):
    return start_job("backup", "Alle Bibliotheken sichern", lambda j: backup_all_dbs(j, "manuell"))


@app.post("/api/backups/full")
def full_backup(_=Depends(auth)):
    return start_job("backup", "Vollsicherung", lambda j: backup_full("manuell", log=j.log))


@app.get("/api/backups/download")
def download_backup(path: str, _=Depends(auth)):
    p = backup_file(path)
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


class PinIn(BaseModel):
    path: str
    pinned: bool


@app.post("/api/backups/pin")
def pin_backup(body: PinIn, _=Depends(auth)):
    backup_file(body.path)
    m = read_meta(body.path) or {"kind": "db" if body.path.endswith(".dump") else "full"}
    m["pinned"] = body.pinned
    write_meta(body.path, m)
    return {"ok": True}


@app.delete("/api/backups")
def delete_backup(path: str, _=Depends(auth)):
    backup_file(path).unlink()
    meta_path(path).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/backups/prune")
def prune(_=Depends(auth)):
    return start_job("prune", "Alte Sicherungen aufräumen", lambda j: prune_backups(j.log))


class RestoreIn(BaseModel):
    path: str
    target: str
    overwrite: bool = False


@app.post("/api/backups/restore")
def restore(body: RestoreIn, _=Depends(auth)):
    p = backup_file(body.path)
    if p.suffix != ".dump":
        raise HTTPException(400, "Nur Bibliotheks-Sicherungen (.dump) lassen sich einzeln wiederherstellen.")
    target = sanitize_lib_name(body.target)
    check_db_name(target)

    def run(j: Job):
        c = pg()
        entry = lib_entry(target)
        if target in db_names(c):
            if not body.overwrite:
                raise RuntimeError(f"„{target}“ existiert bereits. Überschreiben bestätigen oder neuen Namen wählen.")
            pin(backup_db(target, "vor-wiederherstellung", c, j.log, hooks=False))
            terminate_connections(target, c)
            sql(f"DROP DATABASE {qi(target)}", c=c)
            owner = entry["role"] or PG_USER
            sql(f"CREATE DATABASE {qi(target)} OWNER {qi(owner)}", c=c)
            if entry["role"]:
                sql(f"REVOKE CONNECT, TEMPORARY ON DATABASE {qi(target)} FROM PUBLIC", c=c)
                sql(f"GRANT CONNECT, TEMPORARY ON DATABASE {qi(target)} TO {qi(owner)}", c=c)
        else:
            if not NEW_LIB_RE.fullmatch(target):
                raise RuntimeError("Neue Bibliotheken: nur Buchstaben (ohne Umlaute), Ziffern und _.")
            password = random_password()
            create_library_db(target, password, c)
            update_lib(target, role=target, password=password, created=now_iso())
            owner = target
            j.log(f"Neue Bibliothek „{target}“ mit eigenem Zugang angelegt (Passwort unter „Verbinden“).")
        j.log(f"Stelle „{target}“ aus {p.name} wieder her …")
        code, _out, err = run_in(c, ["pg_restore", "-U", PG_USER, "-d", target, "--no-owner", "--no-acl",
                                     "--role", owner, f"/backups/{body.path}"])
        for line in err.splitlines()[:15]:
            j.log(f"pg_restore: {line}")
        if code != 0:
            j.log("pg_restore meldete Warnungen – bitte die Bibliothek in Resolve prüfen.")
        j.log(f"„{target}“ wiederhergestellt.")

    return start_job("restore", f"„{target}“ wiederherstellen", run)


# =========================================================================== #
# Hooks
# =========================================================================== #
@app.get("/api/hooks")
def hooks_info(_=Depends(auth)):
    return {"host_dir": f"{HOST_DATA}/hooks",
            "hooks": [{"name": h, "active": bool(hook_file(h))} for h in HOOK_NAMES]}


# =========================================================================== #
# Zusammenarbeit (Port 50059–50314)
# =========================================================================== #
# Resolve öffnet für die Zusammenarbeit auf JEDEM Arbeitsplatz einen TCP-Port
# (50059, bei Belegung aufwärts bis 50314) und trägt seine Adresse in die
# Bibliotheks-Datenbank ein. Die Arbeitsplätze verbinden sich dann direkt
# untereinander – der Server selbst lauscht auf diesem Port nicht.
# Die Tabellenstruktur ist nicht dokumentiert; deshalb wird jede Zeile als JSON
# gelesen und Name/IP/Port werden heuristisch herausgesucht.
IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
COLLAB_PORT_RE = re.compile(r"\b(50[0-3]\d\d)\b")


def ip_scope(ip: str) -> str:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unbekannt"
    if a in ipaddress.ip_network("100.64.0.0/10"):
        return "vpn"
    if a.is_loopback:
        return "lokal"
    if a.is_private:
        return "privat"
    return "oeffentlich"


def collab_entries(c) -> list[dict]:
    names = db_names(c)
    if not names:
        return []
    cmd = ["psql", "-U", PG_USER, "-d", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=0", "-At", "-F", "\t"]
    for n in names:
        cmd += ["-c", "\\c '" + n.replace("'", "''") + "'",
                "-c", 'SELECT current_database(), row_to_json(t)::text FROM "Sm2SysIdEntry" t']
    _code, out, _err = run_in(c, cmd)
    entries = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        library, raw = line.split("\t", 1)
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        machine = next((str(v) for k, v in row.items() if "name" in k.lower() and v), "")
        addr_text = " ".join(str(v) for k, v in row.items() if "addr" in k.lower() and v) or \
            " ".join(str(v) for v in row.values() if isinstance(v, str))
        ip_m = IPV4_RE.search(addr_text)
        port = next((int(v) for k, v in row.items() if "port" in k.lower() and str(v).isdigit()), None)
        if port is None:
            pm = COLLAB_PORT_RE.search(addr_text)
            port = int(pm.group(1)) if pm else None
        ip = ip_m.group(1) if ip_m else None
        entries.append({"library": library, "machine": machine, "ip": ip, "port": port,
                        "scope": ip_scope(ip) if ip else "unbekannt", "raw": row})
    return entries


@app.get("/api/collab")
def collab(_=Depends(auth)):
    return {"entries": collab_entries(pg()), "db_port": PG_PORT}


class CollabTestIn(BaseModel):
    ip: str
    port: int = 50059


@app.post("/api/collab/test")
def collab_test(body: CollabTestIn, _=Depends(auth)):
    """Prüft vom Server aus, ob ein Arbeitsplatz auf seinem Kollaborations-Port erreichbar ist.
    Läuft im Postgres-Container, weil nur der ins Netz darf (das Backend ist abgeschottet)."""
    import ipaddress
    try:
        ip = str(ipaddress.ip_address(body.ip))
    except ValueError:
        raise HTTPException(400, "Ungültige IP-Adresse.")
    if not 1 <= body.port <= 65535:
        raise HTTPException(400, "Ungültiger Port.")
    start = time.time()
    code, _out, _err = run_in(pg(), ["bash", "-c", 'timeout 3 bash -c "</dev/tcp/$1/$2" 2>/dev/null',
                                     "_", ip, str(body.port)])
    return {"reachable": code == 0, "ms": int((time.time() - start) * 1000)}


# =========================================================================== #
# Server & Version
# =========================================================================== #
VERSION_HINTS = [
    {"version": "13", "note": "Von Blackmagic für Resolve 18–20 ausgeliefert – empfohlen"},
    {"version": "11", "note": "Für Resolve 17 und älter"},
    {"version": "14", "note": "Nicht offiziell von Blackmagic freigegeben"},
    {"version": "15", "note": "Nicht offiziell von Blackmagic freigegeben"},
    {"version": "16", "note": "Nicht offiziell von Blackmagic freigegeben"},
    {"version": "17", "note": "Nicht offiziell von Blackmagic freigegeben"},
]


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


@app.get("/api/server")
def server_info(_=Depends(auth)):
    st = state()
    current_major = pg_major(st["pg_version"])
    dirs = [{"name": d.name, "size": dir_size(d), "current": d.name == current_major}
            for d in sorted(PGD.iterdir()) if d.is_dir()]
    return {"version": st["pg_version"], "hints": VERSION_HINTS, "history": st.get("history", []),
            "data_dirs": dirs, "host_data_dir": HOST_DATA}


class UpgradeIn(BaseModel):
    version: str
    force: bool = False


@app.post("/api/server/upgrade")
def server_upgrade(body: UpgradeIn, _=Depends(auth)):
    target = body.version.strip()
    if not VERSION_RE.fullmatch(target):
        raise HTTPException(400, "Ungültige Versionsangabe (Beispiele: 13, 13.18, 16).")
    if target == state()["pg_version"]:
        raise HTTPException(400, "Diese Version läuft bereits.")
    if not body.force:
        conns = int(sql("SELECT count(*) FROM pg_stat_activity WHERE datname IS NOT NULL "
                        "AND datname <> 'postgres' AND pid <> pg_backend_pid()").strip())
        if conns:
            raise HTTPException(409, f"Es sind noch {conns} Resolve-Verbindung(en) offen. Bitte alle Clients "
                                     "schließen oder den Wechsel erzwingen.")
    return start_job("upgrade", f"Wechsel auf PostgreSQL {target}", lambda j: upgrade(j, target))


@app.post("/api/server/restart")
def server_restart(_=Depends(auth)):
    def run(j: Job):
        c = get_c(cname(pg_major(state()["pg_version"])))
        if not c:
            ensure_pg(j.log)
        else:
            j.log("Starte den Datenbankserver neu …")
            c.restart()
            wait_ready(c)
        BOOT.update(ready=True, error=None)
        j.log("Datenbankserver läuft.")

    return start_job("restart", "Datenbankserver neu starten", run)


@app.delete("/api/server/data-dirs/{name}")
def delete_data_dir(name: str, _=Depends(auth)):
    if not re.fullmatch(r"[\w.\-]+", name) or name in (".", ".."):
        raise HTTPException(400, "Ungültiger Name.")
    if name == pg_major(state()["pg_version"]):
        raise HTTPException(400, "Das Datenverzeichnis der laufenden Version kann nicht gelöscht werden.")
    for c in dk.containers.list(all=True, filters={"label": f"managed-by={LABEL}"}):
        if c.labels.get("pg-major") == name:
            c.remove(force=True)
    path = PGD / name
    if not path.is_dir():
        raise HTTPException(404, "Verzeichnis nicht gefunden.")
    shutil.rmtree(path)
    return {"ok": True}


# =========================================================================== #
# Konfiguration
# =========================================================================== #
class SettingsIn(BaseModel):
    server_name: str
    public_host: str = ""
    retention_days: int = 14
    keep_min: int = 3
    full_backup: bool = True
    full_backup_hour: int = 4


HOST_RE = re.compile(r"^([A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}$|^\[?[0-9a-fA-F:.]+\]?$")


@app.get("/api/settings")
def get_settings(_=Depends(auth)):
    return settings()


@app.put("/api/settings")
def put_settings(body: SettingsIn, _=Depends(auth)):
    name, host = body.server_name.strip(), body.public_host.strip()
    if not 1 <= len(name) <= 60:
        raise HTTPException(400, "Der Servername muss 1–60 Zeichen lang sein.")
    if host and not HOST_RE.fullmatch(host):
        raise HTTPException(400, "Die Zugangsadresse muss ein Domainname oder eine IP-Adresse sein (ohne http://).")
    if not 1 <= body.retention_days <= 3650 or not 1 <= body.keep_min <= 100 or not 0 <= body.full_backup_hour <= 23:
        raise HTTPException(400, "Ungültige Werte bei den Sicherungseinstellungen.")
    s = settings()
    s.update(server_name=name, public_host=host, retention_days=body.retention_days, keep_min=body.keep_min,
             full_backup=body.full_backup, full_backup_hour=body.full_backup_hour)
    _save("settings.json", s)
    return s


@app.post("/api/settings/logo")
async def upload_logo(file: UploadFile = File(...), _=Depends(auth)):
    raw = await file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(400, "Das Bild darf höchstens 2 MB groß sein.")
    if raw.startswith(b"\x89PNG"):
        ext = "png"
    elif raw.startswith(b"\xff\xd8"):
        ext = "jpg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise HTTPException(400, "Erlaubt sind PNG, JPG oder WebP.")
    for old in ("png", "jpg", "webp"):
        (CFG / f"logo.{old}").unlink(missing_ok=True)
    (CFG / f"logo.{ext}").write_bytes(raw)
    return {"ok": True}


@app.delete("/api/settings/logo")
def delete_logo(_=Depends(auth)):
    for old in ("png", "jpg", "webp"):
        (CFG / f"logo.{old}").unlink(missing_ok=True)
    return {"ok": True}


# =========================================================================== #
# Aufgaben
# =========================================================================== #
@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, _=Depends(auth)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    return job.dict()
