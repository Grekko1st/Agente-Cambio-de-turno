"""Prepare a Telegram link without requiring access to the web interface."""
import argparse
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import connection, init_db, now, password_hash, token_hash
from .domain import create_cycle


def prepare_link(username=None, demo=False):
    init_db()
    if demo:
        username = 'prueba_telegram'
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if row and (row['role'] != 'supervisor' or row['project'] != 'Obra de prueba Telegram'):
                raise ValueError('El usuario de prueba ya pertenece a otra obra; no se modificó.')
            if not row:
                # This account is for Telegram only. No shared/default web password is issued.
                db.execute('INSERT INTO users(username,name,password,role,project,specialty,created_at) VALUES(?,?,?,?,?,?,?)',
                           (username, 'Prueba Telegram', password_hash(secrets.token_urlsafe(48)), 'supervisor', 'Obra de prueba Telegram', 'Prueba', now()))
    with connection() as db:
        row = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not row:
            raise ValueError('Usuario no encontrado. Usa --demo para una cuenta de prueba aislada.')
        user = dict(row)
        candidates = db.execute('SELECT * FROM cycles WHERE user_id=? AND end_at>=?', (user['id'], now())).fetchall()
        active = next((c for c in candidates if c['start_date'] <= datetime.now(ZoneInfo(c['timezone'])).date().isoformat()), None)
    if demo and not active:
        create_cycle(user, datetime.now(ZoneInfo('America/Santiago')).date().isoformat(), '18:00', 'America/Santiago')
    token = secrets.token_urlsafe(24)
    with connection() as db:
        db.execute('DELETE FROM telegram_links WHERE user_id=? OR expires_at<=?', (user['id'], now()))
        db.execute('INSERT INTO telegram_links VALUES(?,?,?)', (token_hash(token), user['id'], (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()))
    return '/vincular ' + token


def main():
    parser = argparse.ArgumentParser(description='Genera un código temporal de vinculación con Telegram.')
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--username', help='Usuario existente al que se vinculará Telegram')
    target.add_argument('--demo', action='store_true', help='Crea o reutiliza una cuenta supervisora y un período de prueba separados de las obras reales')
    args = parser.parse_args()
    try:
        command = prepare_link(args.username, args.demo)
    except ValueError as error:
        parser.error(str(error))
    print('Envía este comando en el chat privado de tu bot. Vence en 15 minutos y se usa una sola vez:')
    print(command)


if __name__ == '__main__':
    main()
