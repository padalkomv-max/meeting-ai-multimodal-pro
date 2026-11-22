import base64
import binascii
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv('MEETING_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'meetings.db'
app = FastAPI(title='AI-анализатор встреч')
MAX_REDIRECTS = 5
GOOGLE_DRIVE_HOSTS = {'drive.google.com', 'www.drive.google.com'}
GOOGLE_DOWNLOAD_HOSTS = GOOGLE_DRIVE_HOSTS | {'drive.usercontent.google.com'}


def _auth_required():
    return os.getenv('APP_AUTH_REQUIRED', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}


@app.middleware('http')
async def protect_application(request, call_next):
    if request.url.path == '/healthz' or not _auth_required():
        return await call_next(request)

    expected_user = os.getenv('APP_AUTH_USER', '').strip()
    expected_password = os.getenv('APP_AUTH_PASSWORD', '')
    if not expected_user or not expected_password:
        return PlainTextResponse(
            'Сервис не настроен: задайте APP_AUTH_USER и APP_AUTH_PASSWORD.',
            status_code=503,
        )

    authorization = request.headers.get('authorization', '')
    scheme, _, encoded = authorization.partition(' ')
    if scheme.lower() != 'basic' or not encoded:
        return PlainTextResponse(
            'Требуется авторизация.',
            status_code=401,
            headers={'WWW-Authenticate': 'Basic realm="meeting_ai"'},
        )
    try:
        decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
        username, separator, password = decoded.partition(':')
    except (binascii.Error, UnicodeDecodeError):
        username, separator, password = '', '', ''
    if (not separator or not secrets.compare_digest(username, expected_user)
            or not secrets.compare_digest(password, expected_password)):
        return PlainTextResponse(
            'Неверные учётные данные.',
            status_code=401,
            headers={'WWW-Authenticate': 'Basic realm="meeting_ai"'},
        )
    return await call_next(request)


def connect():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


with connect() as conn:
    conn.execute('''CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY, name TEXT, created_at TEXT, source TEXT,
        status TEXT, error TEXT, transcript TEXT, summary TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS cloud_imports (
        source_ref TEXT PRIMARY KEY,
        meeting_id TEXT NOT NULL UNIQUE,
        source_url TEXT NOT NULL,
        FOREIGN KEY(meeting_id) REFERENCES meetings(id)
    )''')


def update(meeting_id, **fields):
    with connect() as conn:
        conn.execute(
            'UPDATE meetings SET ' + ', '.join(f'{key}=?' for key in fields) + ' WHERE id=?',
            [*fields.values(), meeting_id],
        )


def row_to_dict(row):
    d = dict(row)
    d['summary'] = json.loads(d['summary']) if d['summary'] else None
    return d


def _validated_url(raw_url: str, allowed_hosts: set[str]):
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError('Укажите ссылку Google Диска. Если ссылка недоступна, используйте обычную загрузку видео.')
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError:
        raise ValueError('Некорректная ссылка. Используйте обычную загрузку видео.')
    if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts:
        raise ValueError('Разрешены только публичные HTTPS-ссылки Google Диска. Если ссылка недоступна, используйте обычную загрузку видео.')
    if parsed.username or parsed.password or (port is not None and port != 443):
        raise ValueError('Ссылка содержит небезопасные параметры. Используйте обычную загрузку видео.')
    return parsed


def google_drive_file_id(raw_url: str):
    parsed = _validated_url(raw_url, GOOGLE_DRIVE_HOSTS)
    match = re.search(r'/file/d/([A-Za-z0-9_-]+)', parsed.path)
    if not match:
        match = re.search(r'/d/([A-Za-z0-9_-]+)', parsed.path)
    if not match:
        query_id = parse_qs(parsed.query).get('id', [None])[0]
        if query_id and re.fullmatch(r'[A-Za-z0-9_-]+', query_id):
            return query_id
    if not match:
        raise ValueError('Не удалось найти ID файла в ссылке Google Диска. Используйте обычную загрузку видео.')
    return match.group(1)


def _redirect_url(current_url: str, location: str):
    target = _validated_url(urljoin(current_url, location), GOOGLE_DOWNLOAD_HOSTS)
    return target.geturl()


def _filename_from_headers(headers, file_id: str):
    value = headers.get('content-disposition', '')
    match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", value, re.IGNORECASE)
    if match:
        name = Path(unquote(match.group(1) or match.group(2))).name
        if name:
            return name[:200]
    return f'Google Drive {file_id}.mp4'


def download_google_drive(raw_url: str, destination: Path, max_bytes: int):
    file_id = google_drive_file_id(raw_url)
    current_url = f'https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t'
    headers = {'User-Agent': 'meeting_ai/1.0'}
    timeout = httpx.Timeout(connect=15, read=60, write=15, pool=15)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream('GET', current_url, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get('location')
                        if not location:
                            raise RuntimeError('Google Диск вернул небезопасное перенаправление. Используйте обычную загрузку видео.')
                        current_url = _redirect_url(current_url, location)
                        continue
                    if response.status_code != 200:
                        raise RuntimeError('Не удалось получить файл из Google Диска. Проверьте доступ по ссылке или используйте обычную загрузку видео.')
                    content_type = response.headers.get('content-type', '').split(';', 1)[0].lower()
                    if content_type == 'text/html' or content_type.startswith('text/'):
                        raise RuntimeError('Google Диск не отдал видео без авторизации. Откройте доступ «Все, у кого есть ссылка» или используйте обычную загрузку видео.')
                    content_length = response.headers.get('content-length')
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError as exc:
                            raise RuntimeError('Google Диск вернул некорректный размер файла. Используйте обычную загрузку видео.') from exc
                        if declared_size > max_bytes:
                            raise RuntimeError(f'Файл превышает лимит {max_bytes // (1024 * 1024)} МБ.')
                    written = 0
                    with destination.open('wb') as out:
                        for chunk in response.iter_bytes(1024 * 1024):
                            written += len(chunk)
                            if written > max_bytes:
                                raise RuntimeError(f'Файл превышает лимит {max_bytes // (1024 * 1024)} МБ.')
                            out.write(chunk)
                    if not written:
                        raise RuntimeError('Google Диск вернул пустой файл. Используйте обычную загрузку видео.')
                    return file_id, _filename_from_headers(response.headers, file_id)
            raise RuntimeError('Слишком много перенаправлений при получении файла. Используйте обычную загрузку видео.')
    except httpx.HTTPError as exc:
        raise RuntimeError('Не удалось подключиться к Google Диску. Проверьте доступ по ссылке или используйте обычную загрузку видео.') from exc


def validate_cloud_video(path: Path):
    check = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration,format_name', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=20,
    )
    if check.returncode or not check.stdout.strip():
        raise RuntimeError('Ссылка должна вести на видео MP4 или WebM. Если ссылка недоступна, используйте обычную загрузку видео.')
    try:
        info = json.loads(check.stdout)['format']
        duration = float(info['duration'])
        formats = info.get('format_name', '').split(',')
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError('Не удалось проверить формат видео. Используйте обычную загрузку видео.') from exc
    if not any('mp4' in item or 'webm' in item for item in formats):
        raise RuntimeError('Поддерживаются только видео MP4 и WebM. Используйте обычную загрузку видео.')
    if duration > 360:
        raise RuntimeError('Максимальная длительность учебного видео — 6 минут.')


def run_cloud_import(meeting_id: str, source_url: str, video_path: Path):
    try:
        update(meeting_id, status='Получение файла', error=None)
        _, filename = download_google_drive(
            source_url, video_path, int(os.getenv('MAX_VIDEO_MB', '120')) * 1024 * 1024,
        )
        validate_cloud_video(video_path)
        update(meeting_id, name=filename, status='Распознавание речи', error=None)
        run_analysis(meeting_id, video_path)
    except Exception as exc:
        update(meeting_id, status='Ошибка', error=str(exc)[:350])
    finally:
        video_path.unlink(missing_ok=True)


@app.get('/api/meetings')
def list_meetings():
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute('SELECT * FROM meetings ORDER BY created_at DESC')]


@app.get('/healthz')
def healthz():
    return {'status': 'ok'}


@app.get('/api/meetings/{meeting_id}')
def get_meeting(meeting_id: str):
    with connect() as conn:
        row = conn.execute('SELECT * FROM meetings WHERE id=?', (meeting_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Встреча не найдена')
    return row_to_dict(row)


SUMMARY_INSTRUCTIONS = '''Составь конспект встречи ТОЛЬКО по расшифровке. Пиши по-русски.

Поле decisions — это только явно упомянутые уже принятые решения:
- включай решения, принятые на этой встрече;
- включай решения, принятые ранее, если на встрече прямо сказано, что они уже приняты, действуют или были утверждены;
- распознавай явные маркеры: «принято решение», «было принято решение», «решили», «утвердили», «постановили», «договорились» и эквивалентные формулировки;
- сохраняй смысл решения, даже если фраза длинная или в ней несколько решений; при необходимости разделяй независимые решения на отдельные пункты.
- Например, «Принято решение управляющей компанией ввести в штат управляющего по паркингам и техников» и «Было принято решение разделить один пост на два паркинга» должны попасть в decisions.

Не помещай в decisions предложения, идеи, намерения или обсуждение вариантов без подтверждения принятия решения. Не путай решения с tasks: задачу указывай только в tasks, а решение — только в decisions. В tasks включай поручения и следующие действия только если они явно названы; исполнителя и срок указывай только если они есть в расшифровке. В questions включай открытые вопросы, сомнения и нерешённые варианты.

Ничего не выдумывай: не добавляй отсутствующих участников, исполнителей, сроков, причин или решений. Если раздел не подтверждается расшифровкой, верни пустой массив.''' 


SUMMARY_SCHEMA = {'type':'object','additionalProperties':False,'properties':{
    'brief': {'type':'string'},
    'decisions': {'type':'array','items':{'type':'string'}},
    'tasks': {'type':'array','items':{'type':'string'}},
    'questions': {'type':'array','items':{'type':'string'}}},
    'required':['brief','decisions','tasks','questions']}


def build_summary_payload(transcript: str):
    return {
        'model': os.getenv('OPENAI_SUMMARY_MODEL','gpt-4o-mini'),
        'input': [
            {'role':'system','content':SUMMARY_INSTRUCTIONS},
            {'role':'user','content':transcript},
        ],
        'text': {'format': {
            'type':'json_schema', 'name':'meeting_notes', 'strict':True,
            'schema':SUMMARY_SCHEMA,
        }},
    }


def call_openai_audio(audio_path: Path):
    key = os.getenv('OPENAI_API_KEY')
    if not key:
        raise RuntimeError('Не задан OPENAI_API_KEY на сервере')
    with httpx.Client(timeout=240) as client, audio_path.open('rb') as audio:
        r = client.post('https://api.openai.com/v1/audio/transcriptions',
                        headers={'Authorization': f'Bearer {key}'},
                        data={'model': os.getenv('OPENAI_TRANSCRIBE_MODEL', 'whisper-1')},
                        files={'file': ('audio.mp3', audio, 'audio/mpeg')})
        r.raise_for_status()
        return r.json()['text']


def call_openai_summary(transcript: str):
    key = os.getenv('OPENAI_API_KEY')
    if not key:
        raise RuntimeError('Не задан OPENAI_API_KEY на сервере')
    payload = build_summary_payload(transcript)
    with httpx.Client(timeout=120) as client:
        r = client.post('https://api.openai.com/v1/responses',
                        headers={'Authorization': f'Bearer {key}'}, json=payload)
        r.raise_for_status()
        data = r.json()
    parts = [c.get('text', '') for item in data.get('output', [])
             for c in item.get('content', []) if c.get('type') == 'output_text']
    if not parts:
        raise RuntimeError('Модель не вернула текст конспекта')
    return json.loads(''.join(parts))


def save_summary(meeting_id: str, transcript: str):
    update(meeting_id, status='AI-анализ', error=None)
    summary = call_openai_summary(transcript)
    update(meeting_id, status='Готово', error=None,
           summary=json.dumps(summary, ensure_ascii=False))


def run_resummary(meeting_id: str):
    try:
        with connect() as conn:
            row = conn.execute('SELECT transcript FROM meetings WHERE id=?', (meeting_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Встреча не найдена')
        if not row['transcript']:
            raise RuntimeError('Для повторного конспекта нет сохранённой расшифровки')
        save_summary(meeting_id, row['transcript'])
    except Exception as exc:
        msg = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            msg = f'Ошибка AI-сервиса: HTTP {exc.response.status_code}. Проверьте ключ, баланс и лимиты API.'
        update(meeting_id, status='Ошибка', error=msg[:350])


def run_analysis(meeting_id: str, video_path: Path):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / 'audio.mp3'
            update(meeting_id, status='Распознавание речи')
            subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',str(video_path),
                            '-vn','-ac','1','-ar','16000','-b:a','48k',str(audio)],
                           check=True, timeout=180)
            if audio.stat().st_size > 25_000_000:
                raise RuntimeError('Аудиозапись превышает лимит 25 МБ')
            transcript = call_openai_audio(audio)
            update(meeting_id, status='AI-анализ', transcript=transcript)
            save_summary(meeting_id, transcript)
    except Exception as exc:
        msg = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            msg = f'Ошибка AI-сервиса: HTTP {exc.response.status_code}. Проверьте ключ, баланс и лимиты API.'
        update(meeting_id, status='Ошибка', error=msg[:350])
    finally:
        video_path.unlink(missing_ok=True)


class CloudImportRequest(BaseModel):
    url: str


@app.post('/api/import-cloud', status_code=202)
def import_cloud(request: CloudImportRequest):
    try:
        source_ref = google_drive_file_id(request.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    meeting_id = uuid.uuid4().hex
    video_path = DATA / f'{meeting_id}.cloud'
    try:
        with connect() as conn:
            duplicate = conn.execute(
                '''SELECT meetings.id, meetings.status
                   FROM cloud_imports JOIN meetings ON meetings.id=cloud_imports.meeting_id
                   WHERE cloud_imports.source_ref=?''',
                (source_ref,),
            ).fetchone()
            if duplicate:
                raise HTTPException(
                    409,
                    f'Эта запись уже импортируется или была обработана (ID: {duplicate["id"]}, статус: {duplicate["status"]}).',
                )
            conn.execute(
                'INSERT INTO meetings (id,name,created_at,source,status,error,transcript,summary) VALUES (?,?,?,?,?,?,?,?)',
                (meeting_id, f'Google Drive {source_ref}', datetime.now(timezone.utc).isoformat(),
                 'Из облака', 'Получение файла', None, None, None),
            )
            conn.execute(
                'INSERT INTO cloud_imports (source_ref,meeting_id,source_url) VALUES (?,?,?)',
                (source_ref, meeting_id, request.url.strip()),
            )
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, 'Эта запись уже импортируется или была обработана. Используйте сохранённую встречу.') from exc

    threading.Thread(target=run_cloud_import, args=(meeting_id, request.url.strip(), video_path), daemon=True).start()
    return {'id': meeting_id, 'status': 'Получение файла'}


@app.post('/api/upload', status_code=202)
async def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in {'.mp4', '.webm'}:
        raise HTTPException(400, 'Допустимы только MP4 и WebM')
    max_bytes = int(os.getenv('MAX_VIDEO_MB','120')) * 1024 * 1024
    meeting_id = uuid.uuid4().hex
    path = DATA / f'{meeting_id}{suffix}'
    size = 0
    try:
        with path.open('wb') as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f'Файл превышает {max_bytes // (1024*1024)} МБ')
                out.write(chunk)
        if not size:
            raise HTTPException(400, 'Файл пустой')
        check = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                                '-of','default=nw=1:nk=1',str(path)],
                               capture_output=True, text=True, timeout=20)
        if check.returncode or not check.stdout.strip():
            raise HTTPException(400, 'Некорректный видеофайл')
        duration = float(check.stdout.strip())
        if duration > 360:
            raise HTTPException(400, 'Максимальная длительность учебного видео — 6 минут')
        with connect() as conn:
            conn.execute('INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?)',
                         (meeting_id, Path(file.filename).name[:200],
                          datetime.now(timezone.utc).isoformat(), 'Загружено вручную',
                          'Обрабатывается', None, None, None))
        threading.Thread(target=run_analysis, args=(meeting_id,path), daemon=True).start()
        return {'id':meeting_id,'status':'Обрабатывается'}
    except Exception:
        path.unlink(missing_ok=True)
        raise


@app.post('/api/meetings/{meeting_id}/resummarize', status_code=202)
def resummarize(meeting_id: str):
    with connect() as conn:
        row = conn.execute('SELECT id, transcript FROM meetings WHERE id=?', (meeting_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Встреча не найдена')
    if not row['transcript']:
        raise HTTPException(400, 'Для повторного конспекта нет сохранённой расшифровки')
    update(meeting_id, status='AI-анализ', error=None)
    threading.Thread(target=run_resummary, args=(meeting_id,), daemon=True).start()
    return {'id': meeting_id, 'status': 'AI-анализ'}


@app.get('/', response_class=HTMLResponse)
def home():
    return (ROOT / 'index.html').read_text(encoding='utf-8')
