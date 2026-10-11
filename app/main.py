import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import connection, data_dir, init_db, now, password_hash, password_matches, public_user, token_hash
from .domain import CATEGORIES, add_report, create_cycle, cycle_access, generate_due_handovers, report_access, scope, snapshot

logger = logging.getLogger('bitacora')


async def maintenance():
    from .integrations import poll_telegram
    from .backup import daily_backup
    while True:
        for job in (generate_due_handovers, daily_backup):
            try:
                await asyncio.to_thread(job)
            except Exception:
                logger.warning('El cierre o respaldo necesita atención; se reintentará.')
        if os.environ.get('TELEGRAM_BOT_TOKEN'):
            try:
                await poll_telegram()
            except Exception:
                # Do not log credential-bearing Telegram URLs or provider payloads.
                logger.warning('Telegram necesita atención; se reintentará.')
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    init_db()
    with connection() as db:
        db.execute("UPDATE reports SET processing='pending' WHERE processing='processing'")
    generate_due_handovers()
    worker = asyncio.create_task(maintenance())
    yield
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


app = FastAPI(title='Bitácora de cambio de turno', lifespan=lifespan)
static = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=static), name='static')


@app.middleware('http')
async def security(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return Response('Origen no permitido', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def current_user(request: Request):
    token = request.cookies.get('session', '')
    with connection() as db:
        row = db.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires_at>?', (token_hash(token), now())).fetchone()
        if not row:
            raise HTTPException(401, 'Inicia sesión')
        return dict(row)


def coordinator(user):
    if user['role'] not in ('admin', 'coordinator'):
        raise HTTPException(403, 'Requiere permisos de coordinación')


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class Registration(Login):
    name: str = Field(min_length=1, max_length=120)
    invitation: str


class Invite(BaseModel):
    specialty: str = Field(min_length=1, max_length=100)
    role: str = 'supervisor'


class CycleInput(BaseModel):
    start_date: str
    close_time: str = '18:00'
    timezone: str = 'America/Santiago'


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    responsible: str = Field(default='', max_length=120)
    due_date: str = ''


class ReportInput(BaseModel):
    cycle_id: int
    text: str = Field(min_length=1, max_length=20000)
    category: str = 'progress'
    front: str = Field(default='', max_length=150)
    tasks: list[TaskInput] = Field(default_factory=list, max_length=50)


class QuestionInput(BaseModel):
    question: str = Field(min_length=1, max_length=3000)


class AnswerInput(BaseModel):
    response: str = Field(min_length=1, max_length=5000)
    hours: float | None = Field(default=None, ge=0, le=10000)
    people: int | None = Field(default=None, ge=0, le=10000)


class StatusInput(BaseModel):
    status: str


class CorrectionInput(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class QueryInput(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    day: str | None = None
    cycle_id: int | None = None
    specialty: str | None = None


@app.get('/')
def index():
    return FileResponse(static / 'index.html')


@app.get('/api/health')
def health():
    with connection() as db:
        db.execute('SELECT 1').fetchone()
    return {'database': 'ok'}


@app.post('/api/login')
def login(payload: Login, response: Response):
    with connection() as db:
        user = db.execute('SELECT * FROM users WHERE username=?', (payload.username,)).fetchone()
        if not user or not password_matches(payload.password, user['password']):
            raise HTTPException(401, 'Usuario o contraseña incorrectos')
        token = secrets.token_urlsafe(32)
        db.execute('DELETE FROM sessions WHERE expires_at<=?', (now(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?)', (token_hash(token), user['id'], (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()))
        response.set_cookie('session', token, httponly=True, samesite='strict', secure=os.environ.get('COOKIE_SECURE') == '1', max_age=43200)
        return public_user(user)


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with connection() as db:
        db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(request.cookies.get('session', '')),))
    response.delete_cookie('session')
    return {'ok': True}


@app.post('/api/invitations')
def invite(payload: Invite, user=Depends(current_user)):
    if user['role'] != 'admin':
        raise HTTPException(403, 'Solo administración puede asignar accesos')
    if payload.role not in ('supervisor', 'coordinator'):
        raise HTTPException(422, 'Rol inválido')
    token = secrets.token_urlsafe(24)
    with connection() as db:
        db.execute('INSERT INTO invites VALUES(?,?,?,?,NULL,?)', (token_hash(token), user['project'], payload.specialty, payload.role, (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()))
    return {'invitation': token, 'expires_in_hours': 48}


@app.post('/api/register', status_code=201)
def register(payload: Registration):
    if len(payload.password) < 12:
        raise HTTPException(422, 'Usa al menos 12 caracteres para la contraseña')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        invitation = db.execute('SELECT * FROM invites WHERE token_hash=? AND used_at IS NULL AND expires_at>?', (token_hash(payload.invitation), now())).fetchone()
        if not invitation:
            raise HTTPException(403, 'Invitación inválida o vencida')
        if db.execute('SELECT id FROM users WHERE username=?', (payload.username,)).fetchone():
            raise HTTPException(409, 'El usuario ya existe')
        db.execute('INSERT INTO users(username,name,password,role,project,specialty,created_at) VALUES(?,?,?,?,?,?,?)', (payload.username, payload.name, password_hash(payload.password), invitation['role'], invitation['project'], invitation['specialty'], now()))
        db.execute('UPDATE invites SET used_at=? WHERE token_hash=?', (now(), token_hash(payload.invitation)))
    return {'ok': True}


@app.get('/api/me')
def me(user=Depends(current_user)):
    result = public_user(user)
    result['voice_available'] = bool(os.environ.get('TRANSCRIPTION_API_KEY'))
    result['telegram_available'] = bool(os.environ.get('TELEGRAM_BOT_TOKEN'))
    return result


@app.post('/api/cycles', status_code=201)
def new_cycle(payload: CycleInput, user=Depends(current_user)):
    return {'id': create_cycle(user, payload.start_date, payload.close_time, payload.timezone)}


@app.get('/api/cycles')
def cycles(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute('SELECT c.*,u.name AS author FROM cycles c JOIN users u ON u.id=c.user_id WHERE c.project=? ORDER BY c.start_date DESC,c.id DESC', (user['project'],))
        result = [dict(r) for r in rows if scope(user, r['project'], r['specialty'])]
        for row in result:
            row['next_start_date'] = (date.fromisoformat(row['start_date']) + timedelta(days=14)).isoformat()
        return result


@app.post('/api/reports', status_code=201)
def new_report(payload: ReportInput, user=Depends(current_user)):
    for task in payload.tasks:
        if task.due_date:
            try:
                date.fromisoformat(task.due_date)
            except ValueError:
                raise HTTPException(422, 'Fecha del pendiente inválida')
    report_id = add_report(user, payload.cycle_id, payload.text, payload.category, payload.front, [t.model_dump() for t in payload.tasks])
    return {'id': report_id, 'saved': True}


@app.get('/api/reports')
def reports(day: str | None = None, specialty: str | None = None, cycle_id: int | None = None, user=Depends(current_user)):
    if day:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(422, 'Fecha inválida')
    with connection() as db:
        rows = db.execute('''SELECT r.*,c.project,c.specialty,c.timezone,u.name AS author FROM reports r
            JOIN cycles c ON c.id=r.cycle_id JOIN users u ON u.id=r.user_id WHERE c.project=? ORDER BY r.created_at DESC,r.id DESC''', (user['project'],))
        result = []
        for row in rows:
            if not scope(user, row['project'], row['specialty']):
                continue
            if specialty and row['specialty'] != specialty or cycle_id and row['cycle_id'] != cycle_id:
                continue
            if day and datetime.fromisoformat(row['created_at']).astimezone(ZoneInfo(row['timezone'])).date().isoformat() != day:
                continue
            item = dict(row)
            item['audio_available'] = bool(item.pop('audio_path'))
            item['tasks'] = [dict(t) for t in db.execute('SELECT * FROM tasks WHERE report_id=?', (row['id'],))]
            item['clarifications'] = [dict(q) for q in db.execute('SELECT q.*,u.name AS requester FROM clarifications q JOIN users u ON u.id=q.requested_by WHERE report_id=?', (row['id'],))]
            item['revisions'] = [dict(q) for q in db.execute('SELECT * FROM report_revisions WHERE report_id=? ORDER BY id', (row['id'],))]
            result.append(item)
        return result


@app.post('/api/reports/{report_id}/correct')
def correct_report(report_id: int, payload: CorrectionInput, user=Depends(current_user)):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        report = report_access(db, user, report_id)
        if report['user_id'] != user['id']:
            raise HTTPException(403, 'Solo el autor puede corregir su reporte')
        if report['processing'] == 'processing':
            raise HTTPException(409, 'Espera a que termine la transcripción')
        received = db.execute('SELECT received_at FROM handovers WHERE cycle_id=?', (report['cycle_id'],)).fetchone()
        if received and received['received_at']:
            raise HTTPException(409, 'La entrega ya fue recibida; registra una aclaración para conservar su contexto')
        db.execute('INSERT INTO report_revisions(report_id,user_id,previous_text,corrected_text,created_at) VALUES(?,?,?,?,?)',
                   (report_id, user['id'], report['text'], payload.text, now()))
        db.execute("UPDATE reports SET text=?,processing='complete' WHERE id=?", (payload.text, report_id))
        db.execute('UPDATE handovers SET reviewed_at=NULL WHERE cycle_id=?', (report['cycle_id'],))
    return {'saved': True}


@app.patch('/api/tasks/{task_id}')
def task_status(task_id: int, payload: StatusInput, user=Depends(current_user)):
    if payload.status not in ('pending', 'ongoing', 'done'):
        raise HTTPException(422, 'Estado inválido')
    with connection() as db:
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, 'Pendiente no encontrado')
        report_access(db, user, task['report_id'])
        db.execute('UPDATE tasks SET status=?,updated_at=? WHERE id=?', (payload.status, now(), task_id))
        db.execute('INSERT INTO task_events(task_id,user_id,status,created_at) VALUES(?,?,?,?)', (task_id, user['id'], payload.status, now()))
    return {'ok': True}


@app.get('/api/tasks')
def tasks(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute('''SELECT t.*,c.project,c.specialty,u.name AS author FROM tasks t JOIN reports r ON r.id=t.report_id
            JOIN cycles c ON c.id=r.cycle_id JOIN users u ON u.id=r.user_id WHERE c.project=? ORDER BY t.id DESC''', (user['project'],))
        return [dict(r) for r in rows if scope(user, r['project'], r['specialty'])]


@app.post('/api/reports/{report_id}/questions', status_code=201)
def ask(report_id: int, payload: QuestionInput, user=Depends(current_user)):
    coordinator(user)
    with connection() as db:
        report_access(db, user, report_id)
        row = db.execute('INSERT INTO clarifications(report_id,requested_by,question,created_at) VALUES(?,?,?,?)', (report_id, user['id'], payload.question, now()))
        return {'id': row.lastrowid}


@app.post('/api/questions/{question_id}/answer')
def answer(question_id: int, payload: AnswerInput, user=Depends(current_user)):
    with connection() as db:
        question = db.execute('SELECT * FROM clarifications WHERE id=?', (question_id,)).fetchone()
        if not question:
            raise HTTPException(404, 'Consulta no encontrada')
        report = report_access(db, user, question['report_id'])
        if report['user_id'] != user['id']:
            raise HTTPException(403, 'Solo el autor del reporte puede responder')
        if question['answered_at']:
            raise HTTPException(409, 'La respuesta ya está registrada; crea otra aclaración para corregirla')
        db.execute('UPDATE clarifications SET response=?,answered_by=?,hours=?,people=?,answered_at=? WHERE id=?', (payload.response, user['id'], payload.hours, payload.people, now(), question_id))
    return {'ok': True}


@app.patch('/api/questions/{question_id}/classification')
def classify(question_id: int, payload: StatusInput, user=Depends(current_user)):
    coordinator(user)
    if payload.status not in ('additional', 'interference', 'normal'):
        raise HTTPException(422, 'Clasificación inválida')
    with connection() as db:
        question = db.execute('SELECT * FROM clarifications WHERE id=?', (question_id,)).fetchone()
        if not question:
            raise HTTPException(404, 'Consulta no encontrada')
        report_access(db, user, question['report_id'])
        if question['classification']:
            raise HTTPException(409, 'Clasificación registrada; crea una nueva aclaración para corregirla')
        db.execute('UPDATE clarifications SET classification=?,classified_by=?,classified_at=? WHERE id=?', (payload.status, user['id'], now(), question_id))
    return {'ok': True}


@app.post('/api/query')
async def query(payload: QueryInput, user=Depends(current_user)):
    coordinator(user)
    from .integrations import query_reports
    records = reports(day=payload.day, specialty=payload.specialty, cycle_id=payload.cycle_id, user=user)
    if len(records) > 100:
        raise HTTPException(422, 'Acota la consulta por día, especialidad o período: máximo 100 reportes')
    return await query_reports(payload.question, records)


@app.get('/api/handovers')
def handovers(user=Depends(current_user)):
    generate_due_handovers()
    with connection() as db:
        rows = db.execute('''SELECT h.*,c.project,c.specialty,c.user_id,u.name AS author FROM handovers h
            JOIN cycles c ON c.id=h.cycle_id JOIN users u ON u.id=c.user_id WHERE c.project=? ORDER BY h.created_at DESC''', (user['project'],))
        result = []
        for row in rows:
            if scope(user, row['project'], row['specialty']):
                item = dict(row)
                item['snapshot'] = json.loads(item['snapshot'])
                result.append(item)
        return result


@app.post('/api/handovers/{handover_id}/review')
def review(handover_id: int, user=Depends(current_user)):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        handover = db.execute('SELECT * FROM handovers WHERE id=?', (handover_id,)).fetchone()
        if not handover:
            raise HTTPException(404, 'Entrega no encontrada')
        cycle = cycle_access(db, user, handover['cycle_id'])
        if cycle['user_id'] != user['id']:
            raise HTTPException(403, 'Solo el autor puede revisar su entrega')
        if handover['received_at']:
            raise HTTPException(409, 'Entrega recibida; agrega aclaraciones al reporte para conservar el historial')
        pending = db.execute("SELECT id FROM reports WHERE cycle_id=? AND processing!='complete' LIMIT 1", (cycle['id'],)).fetchone()
        if pending:
            raise HTTPException(409, 'Hay audios pendientes de transcripción; complétalos antes de revisar la entrega')
        db.execute('UPDATE handovers SET snapshot=?,reviewed_at=? WHERE id=?', (json.dumps(snapshot(db, cycle), ensure_ascii=False), now(), handover_id))
    return {'ok': True}


@app.post('/api/handovers/{handover_id}/receive')
def receive(handover_id: int, user=Depends(current_user)):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        handover = db.execute('SELECT * FROM handovers WHERE id=?', (handover_id,)).fetchone()
        if not handover:
            raise HTTPException(404, 'Entrega no encontrada')
        cycle = cycle_access(db, user, handover['cycle_id'])
        if cycle['specialty'] != user['specialty'] or cycle['user_id'] == user['id']:
            raise HTTPException(403, 'Debe recibirla otra persona de la misma especialidad')
        if not handover['reviewed_at']:
            raise HTTPException(409, 'El autor todavía no revisó esta entrega')
        if handover['received_at']:
            raise HTTPException(409, 'Esta entrega ya fue recibida')
        db.execute('UPDATE handovers SET received_at=?,received_by=? WHERE id=?', (now(), user['id'], handover_id))
    return {'ok': True}


@app.post('/api/reports/audio', status_code=201)
async def audio(cycle_id: int, file: UploadFile, user=Depends(current_user)):
    from .integrations import store_audio, process_audio
    content = await file.read(20 * 1024 * 1024 + 1)
    path = store_audio(content, file.filename or 'voice.webm')
    try:
        report_id = add_report(user, cycle_id, '', audio_path=str(path), processing='pending')
    except Exception:
        path.unlink(missing_ok=True)
        raise
    await process_audio(report_id)
    with connection() as db:
        state = db.execute('SELECT processing FROM reports WHERE id=?', (report_id,)).fetchone()['processing']
    return {'id': report_id, 'saved': True, 'processing': state}


@app.post('/api/reports/{report_id}/transcribe')
async def retry_transcription(report_id: int, user=Depends(current_user)):
    with connection() as db:
        report = report_access(db, user, report_id)
        if report['user_id'] != user['id']:
            raise HTTPException(403, 'Solo el autor puede procesar su audio')
    from .integrations import process_audio
    await process_audio(report_id)
    with connection() as db:
        return {'processing': db.execute('SELECT processing FROM reports WHERE id=?', (report_id,)).fetchone()['processing']}


@app.get('/api/reports/{report_id}/audio')
def original_audio(report_id: int, user=Depends(current_user)):
    with connection() as db:
        report = report_access(db, user, report_id)
        if not report['audio_path']:
            raise HTTPException(404, 'Audio no disponible')
        return FileResponse(report['audio_path'])


@app.post('/api/telegram/link')
def telegram_link(user=Depends(current_user)):
    if not os.environ.get('TELEGRAM_BOT_TOKEN'):
        raise HTTPException(503, 'Telegram todavía no está configurado')
    token = secrets.token_urlsafe(24)
    with connection() as db:
        db.execute('DELETE FROM telegram_links WHERE user_id=? OR expires_at<=?', (user['id'], now()))
        db.execute('INSERT INTO telegram_links VALUES(?,?,?)', (token_hash(token), user['id'], (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()))
    return {'command': '/vincular ' + token, 'expires_in_minutes': 15}
