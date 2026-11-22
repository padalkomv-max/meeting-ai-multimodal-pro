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
$env:APP_AUTH_USER = "local-user"
$env:APP_AUTH_PASSWORD = "локальный-пароль"
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Откройте <http://127.0.0.1:8000> и введите учётные данные Basic Auth. Для полностью локального режима без авторизации можно перед запуском задать `APP_AUTH_REQUIRED=false`; для публичного сервиса так делать нельзя.

Поддерживаемые переменные:

```dotenv
OPENAI_API_KEY=replace_me
OPENAI_TRANSCRIBE_MODEL=whisper-1
OPENAI_SUMMARY_MODEL=gpt-4o-mini
MAX_VIDEO_MB=120
APP_AUTH_REQUIRED=true
APP_AUTH_USER=replace_me
APP_AUTH_PASSWORD=replace_me
MEETING_DATA_DIR=data
```

Для PRO вставьте публичную HTTPS-ссылку Google Диска вида `https://drive.google.com/file/d/.../view`. Файл должен быть доступен по ссылке, иметь формат MP4/WebM и не превышать лимит `MAX_VIDEO_MB`.

## Тесты без платных API-вызовов

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node test_frontend.cjs
```

Тесты используют mock-ответы и не требуют `OPENAI_API_KEY`. Учебные и пользовательские видео, расшифровки, результаты обработки и SQLite-базы намеренно не входят в Git-репозиторий.

## Подготовка к Render

В репозитории есть `Dockerfile`, который устанавливает FFmpeg/FFprobe в образ, и `render.yaml` для одного Docker Web Service на плане Free. Приложение слушает `0.0.0.0` и порт Render из переменной `PORT`; проверка доступности выполняется через публичный `/healthz`.

### Что защищено

Все страницы и API, кроме `/healthz`, требуют HTTP Basic Auth. На Render задайте вручную сильные уникальные значения `APP_AUTH_USER` и `APP_AUTH_PASSWORD`, а `APP_AUTH_REQUIRED` оставьте `true`. `OPENAI_API_KEY` также задаётся только в настройках окружения Render и никогда не помещается в Git. Это не меняет Lite/PRO-логику, но не позволяет посторонним загружать видео, запускать API-вызовы или читать расшифровки.

### Важное ограничение Render Free

`MEETING_DATA_DIR=/var/data` выбран как единый путь данных для локального запуска и будущего диска Render, но на Free файловая система временная. Поэтому SQLite, загруженные видео и промежуточные файлы могут исчезнуть после перезапуска, повторного развёртывания или остановки сервиса. Free-режим подходит для демонстрации, но не гарантирует сохранность встреч.

Для сохранения SQLite нужен платный Web Service с Persistent Disk, смонтированным в `/var/data`, либо отдельное внешнее хранилище/база и доработка приложения. Текущий проект SQLite не мигрирует автоматически в Postgres. Даже с диском фоновые задачи на daemon-потоках могут быть прерваны перезапуском; надёжный production-вариант требует очереди и отдельного Background Worker.

### Ручной порядок настройки

1. В Render выберите New → Blueprint или подключите приватный репозиторий `padalkomv-max/meeting-ai-multimodal-pro`, ветку `main` и файл `render.yaml`.
2. Для демонстрации оставьте `plan: free`. Не добавляйте платный диск, пока он не нужен.
3. В Environment задайте `OPENAI_API_KEY`, `APP_AUTH_USER` и `APP_AUTH_PASSWORD` как секретные значения. Остальные переменные уже описаны в `render.yaml`.
4. После ручной проверки доступа откройте сервис по HTTPS. Реальный запуск обработки видео расходует лимиты OpenAI и выполняется только по вашему решению.

Публикация и развёртывание на Render этим репозиторием не выполняются автоматически. Официальные ограничения: <https://render.com/docs/free>, постоянные диски: <https://render.com/docs/disks>, Docker Web Services: <https://render.com/docs/web-services>.
