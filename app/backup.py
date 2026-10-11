"""Consistent SQLite backup and linked audio archive. No credentials included."""
import argparse
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .db import connection, data_dir


def backup(destination):
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        db_path = Path(folder) / 'bitacora.sqlite3'
        with connection() as source, sqlite3.connect(db_path) as snapshot:
            source.backup(snapshot)
            audios = [r[0] for r in snapshot.execute('SELECT audio_path FROM reports WHERE audio_path IS NOT NULL')]
        temporary = target.with_name(target.name + '.partial')
        try:
            with tarfile.open(temporary, 'w:gz') as archive:
                archive.add(db_path, arcname='bitacora.sqlite3')
                for audio in audios:
                    path = Path(audio)
                    archive.add(path, arcname='audio/' + path.name)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def daily_backup():
    folder = Path(os.environ.get('BITACORA_BACKUP_DIR', str(data_dir() / 'backups')))
    target = folder / ('bitacora-' + datetime.now(timezone.utc).date().isoformat() + '.tar.gz')
    if not target.exists():
        backup(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('destination')
    args = parser.parse_args()
    print('Respaldo creado:', backup(args.destination))


if __name__ == '__main__':
    main()
