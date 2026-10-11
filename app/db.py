import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def data_dir():
    path = Path(os.environ.get('BITACORA_DATA_DIR', 'data')).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connection():
    db = sqlite3.connect(data_dir() / 'bitacora.sqlite3', timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with connection() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','coordinator','supervisor')),
          project TEXT NOT NULL, specialty TEXT NOT NULL, telegram_id INTEGER UNIQUE,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invites (
          token_hash TEXT PRIMARY KEY, project TEXT NOT NULL, specialty TEXT NOT NULL,
          role TEXT NOT NULL, used_at TEXT, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS telegram_links (
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cycles (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          project TEXT NOT NULL, specialty TEXT NOT NULL, start_date TEXT NOT NULL,
          end_at TEXT NOT NULL, timezone TEXT NOT NULL, close_time TEXT NOT NULL,
          work_days INTEGER NOT NULL DEFAULT 8, rest_days INTEGER NOT NULL DEFAULT 6,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reports (
          id INTEGER PRIMARY KEY, cycle_id INTEGER NOT NULL REFERENCES cycles(id),
          user_id INTEGER NOT NULL REFERENCES users(id), front TEXT NOT NULL DEFAULT '',
          text TEXT NOT NULL, category TEXT NOT NULL, audio_path TEXT,
          processing TEXT NOT NULL DEFAULT 'complete', source TEXT NOT NULL,
          source_id TEXT UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id),
          title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          responsible TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_events (
          id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
          user_id INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS report_revisions (
          id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id),
          user_id INTEGER NOT NULL REFERENCES users(id), previous_text TEXT NOT NULL,
          corrected_text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS clarifications (
          id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id),
          requested_by INTEGER NOT NULL REFERENCES users(id), question TEXT NOT NULL,
          response TEXT, answered_by INTEGER REFERENCES users(id), hours REAL,
          people INTEGER, classification TEXT, classified_by INTEGER REFERENCES users(id),
          classified_at TEXT, created_at TEXT NOT NULL, answered_at TEXT);
        CREATE TABLE IF NOT EXISTS handovers (
          id INTEGER PRIMARY KEY, cycle_id INTEGER UNIQUE NOT NULL REFERENCES cycles(id),
          snapshot TEXT NOT NULL, created_at TEXT NOT NULL, reviewed_at TEXT, received_at TEXT,
          received_by INTEGER REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reports_cycle ON reports(cycle_id);
        CREATE INDEX IF NOT EXISTS cycles_scope ON cycles(project,specialty);
        ''')


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1)
    return salt + ':' + key.hex()


def password_matches(password, stored):
    salt, expected = stored.split(':')
    actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return secrets.compare_digest(actual, expected)


def public_user(row):
    return {k: row[k] for k in ('id', 'username', 'name', 'role', 'project', 'specialty')}
