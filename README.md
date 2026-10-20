# Lumo

Веб-сервис для скачивания видео и аудио по ссылке. FastAPI + yt-dlp + FFmpeg, фронтенд на Alpine.js без сборки

Пользователь вставляет ссылку и выбирает качество (или MP3), сервер скачивает файл и передаёт прогресс через SSE, после чего файл можно забрать. Файлы хранятся час, затем удаляются

Приватный, платный и защищённый DRM контент не поддерживается

## Запуск

```bash
python3.12 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Нужен `ffmpeg` в PATH (или `FFMPEG_LOCATION` в `.env`), без него не будет MP3 и склейки видео+аудио

Тесты: `pytest -q`. Для вёрстки без интернета есть `python scripts/ui_server.py` (фейковый воркер)

## Как устроено

```
app/
  main.py              создание приложения
  api/routes.py        API + SSE
  core/config.py       настройки из .env
  core/netguard.py     проверка ссылок (SSRF)
  core/security.py     cookie сессии, rate limit, заголовки
  services/media.py    анализ ссылки
  services/formats.py  форматы yt-dlp -> варианты для UI
  services/jobs.py     очередь заданий
  services/runner.py   запуск воркера
  services/worker.py   сам yt-dlp (отдельный процесс)
```

yt-dlp запускается отдельным процессом (`python -m app.services.worker`), запрос идёт в stdin, события JSON-строками из stdout. Так можно нормально отменить загрузку (убить процесс вместе с ffmpeg) и повесить защиту на сокеты только в воркере

Состояние в памяти, поэтому uvicorn строго в один воркер. При рестарте незаконченные задания теряются

### API

| | | |
|---|---|---|
| GET | `/api/health` | статус |
| POST | `/api/analyze` | `{"url"}` -> инфо + варианты |
| POST | `/api/jobs` | `{"analysis_id", "option_id"}` -> задание |
| GET | `/api/jobs/{id}` | статус задания |
| GET | `/api/jobs/{id}/events` | SSE |
| POST | `/api/jobs/{id}/cancel` | отмена |
| GET | `/api/jobs/{id}/file` | файл |

### Про безопасность

Сервис качает по произвольной ссылке, так что главное здесь SSRF. Проверяется два раза: перед запуском (все IP из DNS должны быть публичными) и внутри воркера на каждом `connect()`, это закрывает редиректы и DNS rebinding. Дополнительно в systemd-юните стоит `IPAddressDeny` на частные сети

Клиент присылает только id варианта, сами параметры yt-dlp собираются на сервере. Задания привязаны к cookie сессии

## Деплой (Ubuntu)

```bash
sudo apt install -y python3.12 python3.12-venv ffmpeg nginx
sudo useradd --system --home /opt/lumo --shell /usr/sbin/nologin lumo
sudo mkdir -p /opt/lumo /var/lib/lumo/downloads && sudo chown -R lumo:lumo /opt/lumo /var/lib/lumo

sudo -u lumo git clone <repo> /opt/lumo/app
sudo -u lumo python3.12 -m venv /opt/lumo/venv
sudo -u lumo /opt/lumo/venv/bin/pip install -r /opt/lumo/app/requirements.txt
sudo -u lumo cp /opt/lumo/app/.env.example /opt/lumo/app/.env

sudo cp /opt/lumo/app/deploy/lumo.service /etc/systemd/system/
sudo systemctl enable --now lumo

sudo cp /opt/lumo/app/deploy/nginx-lumo.conf /etc/nginx/sites-available/lumo
sudo ln -s /etc/nginx/sites-available/lumo /etc/nginx/sites-enabled/
sudo certbot --nginx -d your-domain
```

После HTTPS поставить `COOKIE_SECURE=true`. Для SSE в nginx выключена буферизация, иначе прогресс приходит только в конце

yt-dlp надо периодически обновлять, сайты регулярно меняют разметку и API:

```bash
sudo -u lumo /opt/lumo/venv/bin/pip install -U "yt-dlp[default]" && sudo systemctl restart lumo
```

Если YouTube отвечает «проверка на робота», это блок IP сервера. Помогает прокси (`YTDLP_PROXY`)
