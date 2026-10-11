import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from .db import connection, now

CATEGORIES = {'progress', 'ongoing', 'pending', 'planned', 'interference', 'additional'}


def scope(user, project, specialty):
    return user['project'] == project and (user['role'] in ('admin', 'coordinator') or user['specialty'] == specialty)


def cycle_access(db, user, cycle_id):
    row = db.execute('SELECT * FROM cycles WHERE id=?', (cycle_id,)).fetchone()
    if not row or not scope(user, row['project'], row['specialty']):
        raise HTTPException(404, 'Turno no encontrado')
    return row


def report_access(db, user, report_id):
    row = db.execute('SELECT r.*,c.project,c.specialty FROM reports r JOIN cycles c ON c.id=r.cycle_id WHERE r.id=?', (report_id,)).fetchone()
    if not row or not scope(user, row['project'], row['specialty']):
        raise HTTPException(404, 'Reporte no encontrado')
    return row


def create_cycle(user, start_date, close_time, zone):
    try:
        start = date.fromisoformat(start_date)
        close = time.fromisoformat(close_time)
        if close.tzinfo:
            raise ValueError()
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, 'Revisa fecha, hora y zona horaria')
    end = datetime.combine(start + timedelta(days=7), close, tzinfo=tz).astimezone(timezone.utc)
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT id FROM cycles WHERE user_id=? AND start_date<=? AND end_at>=?',
                              (user['id'], (start + timedelta(days=7)).isoformat(), datetime.combine(start, time(), tzinfo=tz).astimezone(timezone.utc).isoformat())).fetchone()
        if existing:
            raise HTTPException(409, 'Ya existe un turno que se superpone con estas fechas')
        cursor = db.execute('INSERT INTO cycles(user_id,project,specialty,start_date,end_at,timezone,close_time,created_at) VALUES(?,?,?,?,?,?,?,?)',
                            (user['id'], user['project'], user['specialty'], start.isoformat(), end.isoformat(), zone, close.isoformat(), now()))
        return cursor.lastrowid


def add_report(user, cycle_id, text, category='progress', front='', tasks=None, source='web', audio_path=None, processing='complete', source_id=None):
    if category not in CATEGORIES:
        raise HTTPException(422, 'Categoría inválida')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        cycle = cycle_access(db, user, cycle_id)
        if cycle['user_id'] != user['id']:
            raise HTTPException(403, 'Registra notas en tu propio turno')
        current = datetime.now(timezone.utc)
        start = datetime.combine(date.fromisoformat(cycle['start_date']), time(), tzinfo=ZoneInfo(cycle['timezone'])).astimezone(timezone.utc)
        if current < start or current > datetime.fromisoformat(cycle['end_at']):
            raise HTTPException(409, 'El turno no está activo; inicia el período correspondiente')
        stamp = now()
        if source_id:
            existing = db.execute('SELECT id FROM reports WHERE source_id=?', (source_id,)).fetchone()
            if existing:
                return existing['id']
        cursor = db.execute('INSERT INTO reports(cycle_id,user_id,front,text,category,audio_path,processing,source,source_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (cycle_id, user['id'], front, text, category, audio_path, processing, source, source_id, stamp))
        report_id = cursor.lastrowid
        for task in tasks or []:
            db.execute('INSERT INTO tasks(report_id,title,responsible,due_date,updated_at) VALUES(?,?,?,?,?)',
                       (report_id, task['title'], task.get('responsible', ''), task.get('due_date', ''), stamp))
        return report_id


def snapshot(db, cycle):
    reports = [dict(r) for r in db.execute('SELECT r.id,r.text,r.category,r.front,r.processing,r.created_at,u.name AS author FROM reports r JOIN users u ON u.id=r.user_id WHERE cycle_id=? ORDER BY r.created_at,r.id', (cycle['id'],))]
    # Includes open tasks from earlier periods of the same specialty; they do not disappear on handover.
    tasks = [dict(r) for r in db.execute('''SELECT t.*,c.id AS cycle_id,u.name AS author FROM tasks t
        JOIN reports r ON r.id=t.report_id JOIN cycles c ON c.id=r.cycle_id JOIN users u ON u.id=r.user_id
        WHERE c.project=? AND c.specialty=? AND c.start_date<=? AND (t.status!='done' OR c.id=?) ORDER BY t.id''',
        (cycle['project'], cycle['specialty'], cycle['start_date'], cycle['id']))]
    clarifications = [dict(r) for r in db.execute('''SELECT q.* FROM clarifications q JOIN reports r ON r.id=q.report_id
        WHERE r.cycle_id=? ORDER BY q.id''', (cycle['id'],))]
    return {'cycle': dict(cycle), 'reports': reports, 'tasks': tasks, 'clarifications': clarifications}


def generate_due_handovers(at=None):
    instant = at or now()
    with connection() as db:
        cycles = db.execute('SELECT * FROM cycles WHERE end_at<=? AND id NOT IN (SELECT cycle_id FROM handovers)', (instant,)).fetchall()
        for cycle in cycles:
            db.execute('INSERT OR IGNORE INTO handovers(cycle_id,snapshot,created_at) VALUES(?,?,?)',
                       (cycle['id'], json.dumps(snapshot(db, cycle), ensure_ascii=False), instant))
        return len(cycles)
