# AI-анализатор видеовстреч — Lite + PRO

Учебное веб-приложение для анализа коротких встреч:

- Lite: загрузка MP4/WebM, извлечение аудио через FFmpeg, реальная транскрибация, AI-конспект и история встреч в SQLite.
- PRO: импорт публичного видео по HTTPS-ссылке Google Диска без OAuth, проверка домена, перенаправлений, размера и формата, защита от повторного импорта одной записи.
- Конспект содержит краткое содержание, явно упомянутые решения, задачи и открытые вопросы.
- Конспект можно пересоздать из сохранённой расшифровки без повторной загрузки и транскрибации видео.
- Интерфейс сохраняет раскрытую расшифровку при автоматическом обновлении статусов.

## Технологии

- Python 3.11+
- FastAPI и Uvicorn
- SQLite
- FFmpeg/FFprobe
- `httpx` для OpenAI API и безопасного получения публичных файлов Google Диска
- OpenAI Audio Transcriptions (`whisper-1` по умолчанию)
- OpenAI Responses API с JSON Schema (`gpt-4o-mini` по умолчанию)
- Vanilla HTML/CSS/JavaScript без API-ключа во frontend

## Локальный запуск

Установите Python 3.11+ и FFmpeg, затем в PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Задайте `OPENAI_API_KEY` только в окружении текущего процесса или в локальном `.env`. Файл `.env` игнорируется Git и не должен публиковаться.

```powershell
$env:OPENAI_API_KEY = "ваш_ключ_только_локально"
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Откройте <http://127.0.0.1:8000>.

Поддерживаемые переменные:

```dotenv
OPENAI_API_KEY=replace_me
OPENAI_TRANSCRIBE_MODEL=whisper-1
OPENAI_SUMMARY_MODEL=gpt-4o-mini
MAX_VIDEO_MB=120
```

Для PRO вставьте публичную HTTPS-ссылку Google Диска вида `https://drive.google.com/file/d/.../view`. Файл должен быть доступен по ссылке, иметь формат MP4/WebM и не превышать лимит `MAX_VIDEO_MB`.

## Тесты без платных API-вызовов

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node test_frontend.cjs
```

Тесты используют mock-ответы и не требуют `OPENAI_API_KEY`. Учебные и пользовательские видео, расшифровки, результаты обработки и SQLite-базы намеренно не входят в Git-репозиторий.
