import base64
import json
import os
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

os.environ['MEETING_DATA_DIR'] = tempfile.mkdtemp(prefix='meeting_test_')
os.environ['APP_AUTH_REQUIRED'] = 'false'
import app as mod


def test_summary_json_roundtrip():
    mid = 'test123'
    with mod.connect() as conn:
        conn.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?)',
                     (mid,'meeting.mp4','2026-11-22','Загружено вручную','Готово',None,
                      'Обсуждали работу',json.dumps({'brief':'Коротко','decisions':[],
                        'tasks':['Проверить'], 'questions':[]}, ensure_ascii=False)))
    with mod.connect() as conn:
        row = conn.execute('SELECT * FROM meetings WHERE id=?',(mid,)).fetchone()
    item = mod.row_to_dict(row)
    assert item['summary']['tasks'] == ['Проверить']
    assert item['summary']['decisions'] == []


def test_status_update():
    mod.update('test123',status='AI-анализ')
    with mod.connect() as conn:
        assert conn.execute('SELECT status FROM meetings WHERE id=?',('test123',)).fetchone()[0] == 'AI-анализ'


def test_explicit_decision_examples_use_structured_decisions(monkeypatch):
    transcript = (
        'Принято решение управляющей компанией ввести в штат управляющего по паркингам и техников. '
        'Было принято решение разделить один пост на два паркинга.'
    )
    expected = [
        'Принято решение управляющей компанией ввести в штат управляющего по паркингам и техников.',
        'Было принято решение разделить один пост на два паркинга.',
    ]
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps({
                'brief': 'Два решения зафиксированы',
                'decisions': expected,
                'tasks': [],
                'questions': [],
            }, ensure_ascii=False)}]}]}

    class Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers=None, json=None):
            captured['url'] = url
            captured['payload'] = json
            return Response()

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setattr(mod.httpx, 'Client', Client)
    result = mod.call_openai_summary(transcript)

    assert result['decisions'] == expected
    assert captured['url'].endswith('/v1/responses')
    assert captured['payload']['input'][1]['content'] == transcript
    instructions = captured['payload']['input'][0]['content']
    assert 'решения, принятые ранее' in instructions
    assert 'предложения, идеи, намерения' in instructions
    assert 'исполнителя и срок' in instructions


def test_resummarize_uses_saved_transcript(monkeypatch):
    mid = 'resummary-check'
    transcript = 'Принято решение разделить один пост на два паркинга.'
    with mod.connect() as conn:
        conn.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?)', (
            mid, 'saved.mp4', '2026-11-22', 'Загружено вручную', 'Готово', None,
            transcript, json.dumps({'brief': 'Старый конспект', 'decisions': [], 'tasks': [], 'questions': []}, ensure_ascii=False)))

    seen = []

    def fake_summary(value):
        seen.append(value)
        return {'brief': 'Новый конспект', 'decisions': [transcript], 'tasks': [], 'questions': []}

    monkeypatch.setattr(mod, 'call_openai_summary', fake_summary)
    with TestClient(mod.app) as client:
        response = client.post(f'/api/meetings/{mid}/resummarize')
        assert response.status_code == 202
        for _ in range(20):
            item = client.get(f'/api/meetings/{mid}').json()
            if item['status'] == 'Готово':
                break
            time.sleep(0.05)

    assert seen == [transcript]
    assert item['summary']['decisions'] == [transcript]
    assert item['transcript'] == transcript


def test_google_drive_download_is_streamed_and_size_limited(monkeypatch):
    class Response:
        status_code = 200
        headers = {'content-type': 'video/mp4', 'content-length': '9', 'content-disposition': 'attachment; filename="lesson.mp4"'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b'video-data'

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, method, url, headers):
            assert method == 'GET'
            assert 'drive.usercontent.google.com' in url
            return Response()

    monkeypatch.setattr(mod.httpx, 'Client', Client)
    target = Path(tempfile.mktemp(suffix='.cloud'))
    try:
        file_id, filename = mod.download_google_drive(
            'https://drive.google.com/file/d/1Test_file-id/view', target, 32,
        )
        assert file_id == '1Test_file-id'
        assert filename == 'lesson.mp4'
        assert target.read_bytes() == b'video-data'
    finally:
        target.unlink(missing_ok=True)


def test_google_drive_rejects_unsafe_link():
    for url in ('http://127.0.0.1/file', 'https://example.com/file', 'https://drive.google.com:444/file/d/id/view'):
        with TestClient(mod.app) as client:
            response = client.post('/api/import-cloud', json={'url': url})
        assert response.status_code == 400
        assert 'обычную загрузку видео' in response.json()['detail']


def test_cloud_import_saves_source_starts_pipeline_and_blocks_duplicate(monkeypatch):
    started = []

    def fake_pipeline(meeting_id, source_url, video_path):
        started.append((meeting_id, source_url, video_path))

    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(mod, 'run_cloud_import', fake_pipeline)
    monkeypatch.setattr(mod.threading, 'Thread', ImmediateThread)
    url = 'https://drive.google.com/file/d/1Cloud_test_record/view'
    with TestClient(mod.app) as client:
        first = client.post('/api/import-cloud', json={'url': url})
        second = client.post('/api/import-cloud', json={'url': url})

    assert first.status_code == 202
    assert first.json()['status'] == 'Получение файла'
    assert second.status_code == 409
    assert len(started) == 1
    with mod.connect() as conn:
        row = conn.execute('SELECT source, status FROM meetings WHERE id=?', (first.json()['id'],)).fetchone()
        cloud = conn.execute('SELECT source_ref FROM cloud_imports WHERE meeting_id=?', (first.json()['id'],)).fetchone()
    assert tuple(row) == ('Из облака', 'Получение файла')
    assert cloud[0] == '1Cloud_test_record'


def test_cloud_pipeline_calls_existing_analysis(monkeypatch):
    mid = 'cloud-pipeline-check'
    with mod.connect() as conn:
        conn.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?)', (
            mid, 'Google Drive source', '2026-11-22', 'Из облака', 'Получение файла', None, None, None))

    called = []

    def fake_download(url, destination, max_bytes):
        destination.write_bytes(b'video')
        return '1Pipeline_record', 'lesson.webm'

    def fake_validate(path):
        assert path.exists()

    def fake_analysis(meeting_id, path):
        called.append((meeting_id, path))

    monkeypatch.setattr(mod, 'download_google_drive', fake_download)
    monkeypatch.setattr(mod, 'validate_cloud_video', fake_validate)
    monkeypatch.setattr(mod, 'run_analysis', fake_analysis)
    path = Path(tempfile.mktemp(suffix='.cloud'))
    mod.run_cloud_import(mid, 'https://drive.google.com/file/d/1Pipeline_record/view', path)

    assert called == [(mid, path)]
    assert not path.exists()
    with mod.connect() as conn:
        row = conn.execute('SELECT name, status FROM meetings WHERE id=?', (mid,)).fetchone()
    assert tuple(row) == ('lesson.webm', 'Распознавание речи')


def test_authentication_protects_app_but_not_healthcheck(monkeypatch):
    monkeypatch.setenv('APP_AUTH_REQUIRED', 'true')
    monkeypatch.setenv('APP_AUTH_USER', 'render-user')
    monkeypatch.setenv('APP_AUTH_PASSWORD', 'render-password')
    token = base64.b64encode(b'render-user:render-password').decode()
    with TestClient(mod.app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/api/meetings').status_code == 401
        assert client.get('/api/meetings', headers={'Authorization': 'Basic invalid'}).status_code == 401
        response = client.get('/api/meetings', headers={'Authorization': f'Basic {token}'})
        assert response.status_code == 200
