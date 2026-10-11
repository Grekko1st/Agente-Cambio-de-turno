import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import connection, init_db, now, password_hash
from app.domain import generate_due_handovers
from app.main import app

PASSWORD = 'password-used-only-in-tests'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('BITACORA_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('TRANSCRIPTION_API_KEY', raising=False)
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    init_db()
    with connection() as db:
        for username, role, project, specialty in [
            ('admin', 'admin', 'Obra A', 'Coordinación'),
            ('coordinator', 'coordinator', 'Obra A', 'Coordinación'),
            ('electric', 'supervisor', 'Obra A', 'Electricidad'),
            ('replacement', 'supervisor', 'Obra A', 'Electricidad'),
            ('civil', 'supervisor', 'Obra A', 'Civil'),
            ('external', 'coordinator', 'Obra B', 'Coordinación')]:
            db.execute('INSERT INTO users(username,name,password,role,project,specialty,created_at) VALUES(?,?,?,?,?,?,?)',
                       (username, username, password_hash(PASSWORD), role, project, specialty, now()))
    with TestClient(app) as client:
        yield client


def login(client, name):
    response = client.post('/api/login', json={'username': name, 'password': PASSWORD})
    assert response.status_code == 200
    return response.json()


def create_period(client, username='electric', start=None):
    login(client, username)
    response = client.post('/api/cycles', json={'start_date': start or (date.today() - timedelta(days=1)).isoformat(), 'close_time': '18:00', 'timezone': 'America/Santiago'})
    assert response.status_code == 201, response.text
    return response.json()['id']


def note(client, cycle_id, text='Instalación en curso; falta conectar tablero.', tasks=None):
    response = client.post('/api/reports', json={'cycle_id': cycle_id, 'text': text, 'category': 'ongoing', 'tasks': tasks or []})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_invitation_role_is_assigned_by_admin_and_used_once(client):
    login(client, 'admin')
    code = client.post('/api/invitations', json={'specialty': 'Mecánica', 'role': 'supervisor'}).json()['invitation']
    payload = {'username': 'new', 'name': 'Nueva persona', 'password': PASSWORD, 'invitation': code, 'role': 'admin'}
    assert client.post('/api/register', json=payload).status_code == 201
    assert client.post('/api/register', json={**payload, 'username': 'another'}).status_code == 403
    login(client, 'new')
    assert client.get('/api/me').json()['role'] == 'supervisor'
    assert client.post('/api/invitations', json={'specialty': 'Otra'}).status_code == 403


def test_login_logout_and_anonymous_access(client):
    assert client.get('/api/reports').status_code == 401
    assert client.post('/api/login', json={'username': 'electric', 'password': 'wrong'}).status_code == 401
    login(client, 'electric')
    assert client.get('/api/me').json()['username'] == 'electric'
    client.post('/api/logout')
    assert client.get('/api/me').status_code == 401


def test_specialty_and_project_permissions_include_audio_and_questions(client):
    cycle = create_period(client)
    report = note(client, cycle)
    login(client, 'civil')
    assert client.get('/api/reports').json() == []
    assert client.post(f'/api/reports/{report}/questions', json={'question': 'Horas?'}).status_code == 403
    assert client.get(f'/api/reports/{report}/audio').status_code == 404
    login(client, 'external')
    assert client.get('/api/reports').json() == []
    assert client.post(f'/api/reports/{report}/questions', json={'question': 'Horas?'}).status_code == 404
    login(client, 'coordinator')
    assert len(client.get('/api/reports').json()) == 1
    assert client.post(f'/api/reports/{report}/questions', json={'question': 'Horas?'}).status_code == 201
    login(client, 'replacement')
    assert len(client.get('/api/reports').json()) == 1
    assert client.post('/api/reports', json={'cycle_id': cycle, 'text': 'Otra nota'}).status_code == 403


def test_coordinator_questions_retain_answers_and_do_not_invent_hours(client):
    cycle = create_period(client)
    report = note(client, cycle)
    login(client, 'coordinator')
    question = client.post(f'/api/reports/{report}/questions', json={'question': '¿Qué interferencia hubo?'}).json()['id']
    assert client.post(f'/api/questions/{question}/answer', json={'response': 'Respuesta ajena'}).status_code == 403
    login(client, 'electric')
    assert client.post(f'/api/questions/{question}/answer', json={'response': 'Faltó liberar el sector.'}).status_code == 200
    assert client.post(f'/api/questions/{question}/answer', json={'response': 'Cambio'}).status_code == 409
    row = client.get('/api/reports').json()[0]['clarifications'][0]
    assert row['hours'] is None and row['people'] is None
    assert row['response'] == 'Faltó liberar el sector.'
    assert client.patch(f'/api/questions/{question}/classification', json={'status': 'additional'}).status_code == 403
    login(client, 'coordinator')
    assert client.patch(f'/api/questions/{question}/classification', json={'status': 'additional'}).status_code == 200


def test_eight_day_close_review_receipt_and_task_continuity(client):
    start = date.today() - timedelta(days=1)
    cycle = create_period(client, start=start.isoformat())
    report = note(client, cycle, tasks=[{'title': 'Conectar tablero', 'responsible': 'Equipo eléctrico'}])
    row = client.get('/api/cycles').json()[0]
    assert row['next_start_date'] == (start + timedelta(days=14)).isoformat()
    end = datetime.fromisoformat(row['end_at'])
    assert generate_due_handovers((end - timedelta(seconds=1)).isoformat()) == 0
    assert generate_due_handovers(end.isoformat()) == 1
    assert generate_due_handovers(end.isoformat()) == 0
    handover = client.get('/api/handovers').json()[0]
    assert handover['snapshot']['tasks'][0]['title'] == 'Conectar tablero'
    assert handover['reviewed_at'] is None
    login(client, 'replacement')
    assert client.post(f"/api/handovers/{handover['id']}/receive").status_code == 409
    login(client, 'electric')
    assert client.post(f"/api/handovers/{handover['id']}/review").status_code == 200
    login(client, 'replacement')
    assert client.post(f"/api/handovers/{handover['id']}/receive").status_code == 200
    assert client.post(f"/api/handovers/{handover['id']}/receive").status_code == 409
    assert client.get('/api/tasks').json()[0]['status'] == 'pending'
    task = client.get('/api/tasks').json()[0]['id']
    assert client.patch(f'/api/tasks/{task}', json={'status': 'done'}).status_code == 200
    with connection() as db:
        assert db.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == 1
    login(client, 'electric')
    assert client.post(f'/api/reports/{report}/correct', json={'text': 'Cambio posterior'}).status_code == 409
    # Frozen delivery remains the record that was received, even after pending tasks evolve.
    assert client.get('/api/handovers').json()[0]['snapshot']['tasks'][0]['status'] == 'pending'


def test_corrections_preserve_original_and_invalidate_review(client):
    cycle = create_period(client)
    report = note(client, cycle, 'Texto original')
    with connection() as db:
        end = db.execute('SELECT end_at FROM cycles WHERE id=?', (cycle,)).fetchone()['end_at']
    generate_due_handovers(end)
    handover = client.get('/api/handovers').json()[0]['id']
    client.post(f'/api/handovers/{handover}/review')
    assert client.post(f'/api/reports/{report}/correct', json={'text': 'Texto corregido'}).status_code == 200
    report = client.get('/api/reports').json()[0]
    assert report['revisions'][0]['previous_text'] == 'Texto original'
    assert report['text'] == 'Texto corregido'
    assert client.get('/api/handovers').json()[0]['reviewed_at'] is None


def test_audio_is_saved_without_credentials_and_protected(client):
    cycle = create_period(client)
    response = client.post(f'/api/reports/audio?cycle_id={cycle}', files={'file': ('nota.ogg', b'fake-audio-for-storage-test', 'audio/ogg')})
    assert response.status_code == 201
    assert response.json()['saved'] is True and response.json()['processing'] == 'pending'
    report = response.json()['id']
    assert client.get(f'/api/reports/{report}/audio').content == b'fake-audio-for-storage-test'
    login(client, 'civil')
    assert client.get(f'/api/reports/{report}/audio').status_code == 404
    login(client, 'electric')
    assert client.post(f'/api/reports/audio?cycle_id={cycle}', files={'file': ('bad.exe', b'x')}).status_code == 422
    with connection() as db:
        end = db.execute('SELECT end_at FROM cycles WHERE id=?', (cycle,)).fetchone()['end_at']
    generate_due_handovers(end)
    h = client.get('/api/handovers').json()[0]['id']
    assert client.post(f'/api/handovers/{h}/review').status_code == 409


def test_transcription_retry_preserves_text_after_extraction_failure(client, monkeypatch):
    from app import integrations
    cycle = create_period(client)
    report = client.post(f'/api/reports/audio?cycle_id={cycle}', files={'file': ('nota.ogg', b'test')}).json()['id']
    monkeypatch.setenv('TRANSCRIPTION_API_KEY', 'fake-test-key')
    calls = []
    async def transcription(path):
        calls.append(path)
        return 'Queda pendiente el tablero.'
    async def failed_extract(text):
        raise RuntimeError('Simulated provider failure')
    monkeypatch.setattr(integrations, 'transcribe', transcription)
    monkeypatch.setattr(integrations, 'extract', failed_extract)
    assert client.post(f'/api/reports/{report}/transcribe').json()['processing'] == 'transcribed'
    assert client.get('/api/reports').json()[0]['text'] == 'Queda pendiente el tablero.'
    async def good_extract(text):
        return {'category': 'pending', 'tasks': [{'title': 'Conectar tablero', 'responsible': '', 'due_date': ''}]}
    monkeypatch.setattr(integrations, 'extract', good_extract)
    assert client.post(f'/api/reports/{report}/transcribe').json()['processing'] == 'complete'
    assert client.post(f'/api/reports/{report}/transcribe').json()['processing'] == 'complete'
    assert len(calls) == 1
    assert len(client.get('/api/tasks').json()) == 1


def test_invalid_cycles_and_future_notes_are_rejected(client):
    login(client, 'electric')
    assert client.post('/api/cycles', json={'start_date': 'bad', 'timezone': 'No/Zone'}).status_code == 422
    future = create_period(client, start=(date.today() + timedelta(days=4)).isoformat())
    assert client.post('/api/reports', json={'cycle_id': future, 'text': 'Anticipado'}).status_code == 409
    assert client.post('/api/cycles', json={'start_date': (date.today() + timedelta(days=5)).isoformat()}).status_code == 409


def test_persistent_storage_survives_new_connection(client):
    cycle = create_period(client)
    report = note(client, cycle, 'Registro persistente')
    init_db()
    with connection() as db:
        assert db.execute('SELECT text FROM reports WHERE id=?', (report,)).fetchone()['text'] == 'Registro persistente'
    assert client.get('/api/health').json() == {'database': 'ok'}
    assert client.get('/').status_code == 200


def test_csrf_origin_rejected(client):
    login(client, 'electric')
    assert client.post('/api/logout', headers={'Origin': 'https://untrusted.example'}).status_code == 403


def test_backup_contains_database_and_audio(client, tmp_path):
    import sqlite3
    import tarfile
    from app.backup import backup
    cycle = create_period(client)
    report = client.post(f'/api/reports/audio?cycle_id={cycle}', files={'file': ('nota.ogg', b'backup-test-audio')}).json()['id']
    archive = backup(tmp_path / 'copy.tar.gz')
    folder = tmp_path / 'restored'
    folder.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(folder, filter='data')
    with sqlite3.connect(folder / 'bitacora.sqlite3') as db:
        path = db.execute('SELECT audio_path FROM reports WHERE id=?', (report,)).fetchone()[0]
    from pathlib import Path
    assert (folder / 'audio' / Path(path).name).read_bytes() == b'backup-test-audio'


def test_query_without_key_and_cross_project_scope(client):
    cycle = create_period(client)
    note(client, cycle, 'Información de la obra A')
    login(client, 'coordinator')
    assert client.post('/api/query', json={'question': '¿Qué pasó?'}).status_code == 503
    login(client, 'external')
    result = client.post('/api/query', json={'question': '¿Qué pasó?'}).json()
    assert result['report_ids'] == []
    login(client, 'electric')
    assert client.post('/api/query', json={'question': '¿Qué pasó?'}).status_code == 403


def test_ai_provider_adapter_uses_structured_response_and_authorized_records(client, monkeypatch):
    import httpx
    import json
    from app import integrations
    cycle = create_period(client)
    report = note(client, cycle, 'Información de la obra A')
    monkeypatch.setenv('TRANSCRIPTION_API_KEY', 'fake-test-key')
    real_client = httpx.AsyncClient
    bodies = []
    def provider(request):
        payload = json.loads(request.content)
        bodies.append(payload)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'answer': 'Se registró información de la obra A.', 'report_ids': [report]})}}]})
    monkeypatch.setattr(integrations.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs))
    login(client, 'coordinator')
    result = client.post('/api/query', json={'question': '¿Qué pasó?'}).json()
    assert result['report_ids'] == [report]
    context = json.loads(bodies[0]['messages'][1]['content'])
    assert all(r['specialty'] == 'Electricidad' for r in context['records'])
    assert bodies[0]['response_format']['json_schema']['strict'] is True


def test_telegram_requires_private_linked_user_and_deduplicates(client, monkeypatch):
    import httpx
    import json
    from app import integrations
    cycle = create_period(client)
    with connection() as db:
        db.execute("UPDATE users SET telegram_id=123 WHERE username='electric'")
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake-test-bot')
    sends = []
    def transport(request):
        payload = json.loads(request.content)
        sends.append(payload)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
            update = {'update_id': 99, 'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, 'text': 'Pendiente liberar el frente norte.'}}
            await integrations.handle_telegram(remote, update)
            await integrations.handle_telegram(remote, update)
            group = {'update_id': 100, 'message': {'from': {'id': 123}, 'chat': {'id': -1, 'type': 'group'}, 'text': 'No registrar desde grupos'}}
            await integrations.handle_telegram(remote, group)
    asyncio.run(run())
    reports = client.get('/api/reports').json()
    assert len(reports) == 1
    assert reports[0]['source'] == 'telegram'
    assert len(sends) == 2


def test_telegram_terminal_demo_link_is_limited_and_single_use(client, monkeypatch):
    import httpx
    import json
    from app.telegram_setup import prepare_link
    from app.integrations import handle_telegram
    first = prepare_link(demo=True)
    second = prepare_link(demo=True)
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE username='prueba_telegram'").fetchone()
        assert user['role'] == 'supervisor'
        assert user['project'] == 'Obra de prueba Telegram'
        assert db.execute('SELECT COUNT(*) FROM cycles WHERE user_id=?', (user['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM telegram_links WHERE user_id=?', (user['id'],)).fetchone()[0] == 1
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake-test-bot')
    sent = []
    def transport(request):
        sent.append(json.loads(request.content)['text'])
        return httpx.Response(200, json={'ok': True, 'result': {}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as remote:
            for i, command in enumerate([first, second, second]):
                await handle_telegram(remote, {'update_id': i, 'message': {'from': {'id': 987}, 'chat': {'id': 987, 'type': 'private'}, 'text': command}})
    asyncio.run(run())
    assert sent[0].startswith('Código vencido')
    assert sent[1].startswith('Cuenta vinculada')
    assert sent[2].startswith('Código vencido')


def test_telegram_read_commands_respect_project_and_specialty(client):
    from app.integrations import telegram_view
    cycle = create_period(client)
    note(client, cycle, 'Reporte privado de electricidad', [{'title': 'Conectar tablero'}])
    with connection() as db:
        users = {row['username']: dict(row) for row in db.execute('SELECT * FROM users')}
    assert 'Reporte privado de electricidad' in telegram_view(users['replacement'], '/bitacora')
    assert 'Conectar tablero' in telegram_view(users['coordinator'], '/pendientes')
    assert 'Reporte privado de electricidad' not in telegram_view(users['civil'], '/bitacora')
    assert 'Conectar tablero' not in telegram_view(users['external'], '/pendientes')
