# Codex Telegram Agent

MVP Telegram bot для управления локальным Codex Desktop на Mac.

## Что делает

- Показывает проекты кнопками.
- Показывает и создает чаты внутри проекта.
- Автоматически подтягивает уже существующие Codex-сессии из `~/.codex/sessions`.
- Принимает текст, фото, документы, видео, аудио и voice как запрос к Codex.
- Ставит запрос в SQLite-очередь.
- Локальный worker запускает Codex Desktop `app-server` через stdio.
- После завершения задачи бот делает background refresh `codex://threads/<session_id>` через `open -g`, чтобы Desktop подтянул новые сообщения без переключения окна.
- `codex exec` для Telegram-задач не используется.
- Для выбранного Codex-чата бот ждет завершение в `~/.codex/sessions` и возвращает финальный ответ в Telegram.
- Возвращает итоговый ответ Codex в Telegram.
- Длинные ответы режет на части по 3500 символов.
- Пускает только Telegram user id из `TELEGRAM_ALLOWED_USER_IDS`.
- В итоговых ответах показывает, из какого проекта и чата пришел ответ.
- Внизу Telegram закрепляется постоянное меню: проекты, чаты, новый чат, модель, статус, помощь.
- Позволяет выбрать модель GPT и скорость рассуждения для новых задач.

MVP сохраняет историю jobs и вложения в SQLite/`data/attachments`. Для старых Codex-сессий сохраняется `codex_session_id`, чтобы отправлять задачи и уведомления через Codex Desktop/App.

Новый чат, созданный в Telegram, получает новый Codex Desktop `thread` при первой задаче. Уже существующие Codex Desktop чаты импортируются из `~/.codex/sessions`.

## Старые проекты и чаты Codex

При старте и при открытии `/projects` бот сканирует:

```text
~/.codex/sessions
```

Из каждой найденной сессии он берет:

- `session_meta.id` как `codex_session_id`;
- `session_meta.cwd` как проект;
- первый нормальный пользовательский запрос как название чата.

Проекты из `config/projects.json` показываются первыми. Остальные проекты добавляются автоматически по найденным `cwd`.
Такие автоматически найденные проекты показываются по имени папки.

## 1. Создать Telegram bot через BotFather

1. Открой Telegram.
2. Найди `@BotFather`.
3. Отправь:

```text
/newbot
```

4. Задай имя и username бота.
5. BotFather выдаст token. Вставь его в `.env` как `TELEGRAM_BOT_TOKEN`.

Не отправляй token в чат и не коммить `.env`.

## 2. Узнать свой Telegram user id

Варианты:

- Напиши `@userinfobot` в Telegram.
- Или временно напиши своему боту и посмотри update через Telegram API, если уже знаешь token.

В `.env` укажи:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

Если нужно несколько id:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

## 3. Заполнить `.env`

Создай файл:

```bash
cd /path/to/codex-telegram-agent
cp .env.example .env
```

Пример:

```env
TELEGRAM_BOT_TOKEN=put_real_token_here
TELEGRAM_ALLOWED_USER_IDS=123456789
CODEX_BINARY=codex
CODEX_AGENT_DB=/path/to/codex-telegram-agent/data/agent.sqlite
CODEX_AGENT_LOG=/path/to/codex-telegram-agent/logs/agent.log
MAX_PARALLEL_JOBS=1
CODEX_DESKTOP_TIMEOUT_SECONDS=7200
```

`CODEX_BINARY` оставлен для совместимости, но Telegram-задачи отправляются только в Codex Desktop.

## 4. Заполнить `config/projects.json`

Создай локальный файл из примера:

```bash
cp config/projects.example.json config/projects.json
```

Формат:

```json
{
  "projects": [
    {
      "id": "example_project",
      "title": "Example Project",
      "cwd": "/absolute/path/to/your/project",
      "enabled": true
    }
  ]
}
```

`config/projects.json` содержит локальные пути и не должен попадать в git.

При старте агент проверяет, что файл существует, каждый enabled `cwd` существует, и Codex Desktop `app-server` доступен.

## 5. Запустить агента

```bash
cd /path/to/codex-telegram-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.main
```

Лог:

```text
/path/to/codex-telegram-agent/logs/agent.log
```

SQLite:

```text
/path/to/codex-telegram-agent/data/agent.sqlite
```

## 6. Проверить работу в Telegram

1. Напиши боту:

```text
/start
```

2. Выбери проект.
3. Выбери уже существующий Codex Desktop чат или нажми `Новый чат`.
4. При необходимости нажми `Модель` и выбери GPT/скорость.
5. Отправь тестовый запрос:

```text
Скажи коротко, в какой папке ты работаешь.
```

Можно отправить фото или документ с подписью. Вложения передаются в Codex Desktop как локальные пути в prompt.

Ожидаемый поток:

```text
Задача поставлена в очередь: #1
В работе в Codex Desktop: #1
Проект: ...
Чат: ...
Готово: #1
...
```

## 7. Автозапуск через launchd на macOS

Создай файл:

```text
~/Library/LaunchAgents/com.tim.codex-telegram-agent.plist
```

Содержимое:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tim.codex-telegram-agent</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/codex-telegram-agent/.venv/bin/python</string>
    <string>-m</string>
    <string>src.main</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/path/to/codex-telegram-agent</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/path/to/codex-telegram-agent/logs/launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>/path/to/codex-telegram-agent/logs/launchd.err.log</string>
</dict>
</plist>
```

Загрузить:

```bash
launchctl load ~/Library/LaunchAgents/com.tim.codex-telegram-agent.plist
```

Остановить:

```bash
launchctl unload ~/Library/LaunchAgents/com.tim.codex-telegram-agent.plist
```

Проверить:

```bash
launchctl list | grep com.tim.codex-telegram-agent
tail -f /path/to/codex-telegram-agent/logs/agent.log
```

## Команды бота

- `/start` - выбрать проект.
- `/projects` - выбрать проект.
- `/chats` - выбрать чат активного проекта.
- `/newchat` - создать чат.
- `/status` - активный проект, активный чат и последняя задача.
- `/cancel` - отменить queued задачу.
- `/watch` - включить уведомления о завершении для активного Codex-чата.
- `/unwatch` - выключить уведомления для активного Codex-чата.
- `/help` - краткая справка.

## Вложения

Бот скачивает вложения в:

```text
/path/to/codex-telegram-agent/data/attachments
```

Поддерживается MVP:

- фото;
- документы;
- видео;
- аудио;
- voice.

В Desktop-only режиме вложения добавляются в prompt как локальные пути, чтобы Codex Desktop мог открыть их с этого Mac.

## Уведомления о задачах из Codex Desktop/App

Когда выбираешь Codex-чат в Telegram, бот автоматически включает наблюдение за этой локальной Codex-сессией.

Если потом запустить работу прямо из Codex Desktop/App в этом же чате, агент увидит завершение в `~/.codex/sessions` и отправит в Telegram проект, чат и финальный ответ Codex.

Формат уведомления:

```text
Проект: <project title>
Чат: <chat title>

<финальный ответ Codex>
```

Если Codex-сессия не относится к проекту из `config/projects.json`, проект показывается как:

```text
<имя папки>
```

Команды:

```text
/watch
/unwatch
```
