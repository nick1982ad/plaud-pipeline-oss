# Plaud Pipeline

**Превращайте часы аудио и видео в структурированные документы, главы книги и YouTube-метаданные за минуты.**

Локальный pipeline, который:
1. Скачивает записи из вашего диктофона **[Plaud](https://www.plaud.ai/)** (или берёт `.wav/.mp3/.mov` из любой папки) — через личный кабинет, без облачных посредников.
2. Расшифровывает их через **Whisper** локально (CPU, NVIDIA CUDA, или Intel iGPU через OpenVINO).
3. Прогоняет транскрипт через LLM (**Claude API**, **Claude Code subscription** или локальная **Ollama**) — получает структурированный summary, главу книги или YouTube-метаданные.
4. Складывает результат в `.pdf + .md + transcript.txt` по тематическим папкам.

Никаких облачных хранилищ ваших записей. Никаких внешних агентов с вашими данными. Только ваш ноутбук и тот LLM, который выбрали вы.

---

## ⚡ Быстрый старт (5 минут)

```bash
git clone https://github.com/<you>/plaud-pipeline.git
cd plaud-pipeline

# 1. Установка зависимостей
python -m pip install -r requirements.txt

# 2. Интерактивная настройка путей и backend'а
python init.py

# 3a. Если у вас есть Plaud:
#     Получите токен (см. ниже "Подключение к Plaud") и положите в bulk_export/config.json
python bulk_export/process_new_plaud_batch.py --account 2

# 3b. Или если у вас просто папка с аудио/видео:
python bulk_export/process_local_audio.py --source "C:\путь\к\записям"

# 3c. Видео-лекции → главы книги:
python bulk_export/process_lectures.py --source "C:\Lectures" --output "C:\Book"

# 3d. YouTube Shorts → метаданные для загрузки:
python bulk_export/process_shorts.py --source "C:\Shorts" --topic "О чём ваша серия"
```

---

## 🔌 Подключение к личному кабинету Plaud

> Если вы НЕ используете Plaud-диктофон — пропустите этот раздел и переходите к [📂 Pipeline для локальной папки](#-pipeline-1-локальная-папка-аудиовидео).

Plaud Web (https://web.plaud.ai) хранит ваши записи в облаке. Pipeline ходит туда напрямую через их официальный API, используя **bearer-токен из вашей собственной сессии**. Ни логин, ни пароль ни на каком этапе не отправляются третьим сторонам — токен берётся прямо из вашего браузера.

### Шаг 1. Войдите в Plaud Web

Откройте https://web.plaud.ai в **Chrome / Edge / Firefox** и залогиньтесь как обычно (email/пароль, Google, Apple — что угодно).

### Шаг 2. Откройте DevTools

Нажмите **F12** (или `Ctrl+Shift+I`, или ПКМ → Inspect).

### Шаг 3. Найдите токен

1. Перейдите на вкладку **Application** (в Firefox — **Storage**).
2. Слева раскройте **Storage → Local Storage → https://web.plaud.ai**.
3. В списке ключей найдите **`tokenstr`**.

   ![DevTools location](docs/img/devtools-tokenstr.png) <!-- placeholder, см. docs/img/ -->

4. Кликните по нему — справа увидите значение, начинающееся с `bearer eyJ...` или просто `eyJ...`.
5. **Скопируйте всё значение** (двойной клик → Ctrl+A → Ctrl+C).

> ⚠ **Берегите этот токен как пароль.** Он даёт полный доступ к вашему аккаунту Plaud на ~10 месяцев. Не делитесь, не коммитьте, не загружайте в чаты с ИИ-ассистентами.

### Шаг 4. Положите токен в конфиг

```bash
cd bulk_export
cp config.example.json config.json
```

Откройте `config.json` в редакторе и замените `PASTE_YOUR_JWT_TOKEN_HERE` на ваш токен. Также при необходимости поправьте `region`:

```json
{
  "token": "bearer eyJhbGciOi...",
  "region": "us",
  "output_dir": "./output",
  "formats": ["transcript", "summary"],
  "mp3_mode": "all"
}
```

- **`region`** — `"us"` для североамериканских аккаунтов, `"eu"` для европейских. Если ошиблись — скрипт сам переключится при первой попытке.
- **`mp3_mode`** — `"all"` качает все аудио (десятки гигабайт), `"untranscribed"` только сырые без Plaud-транскрипта, `"off"` — только текст.

### Шаг 5. Проверьте, что работает

```bash
python bulk_export/plaud_export.py --config config.json
```

Должны увидеть `Got N files` и список скачиваний. Готово — вы подключены.

### Если у вас две учётки Plaud

Скопируйте `config_2.example.json` → `config_2.json`, заполните свой второй токен. Запускайте раздельно:

```bash
python bulk_export/process_new_plaud_batch.py --account 1   # config.json
python bulk_export/process_new_plaud_batch.py --account 2   # config_2.json
```

### Если токен протух (HTTP 401)

Через 8-10 месяцев JWT истекает. Просто повторите шаги 1-4 и положите новый токен в тот же `config.json`. Скрипт идемпотентен — ничего не сломается.

---

## 🧠 LLM backend

Скрипты пайплайна нуждаются в LLM для генерации summary/глав/метаданных. Поддерживаются три бэкенда — выбирается **автоматически**, или принудительно через `--backend`:

| Backend | Сеть | Биллинг | Скорость | Качество | Что нужно |
|---|---|---|---|---|---|
| **`sdk`** | да | по токенам | ~2 сек/файл | топ | `ANTHROPIC_API_KEY` |
| **`cli`** | да | в подписке Claude Code | ~5 сек/файл | топ | `claude` CLI в PATH |
| **`ollama`** | **нет** | **бесплатно** | ~30-90 сек/файл (CPU) | хорошее | Ollama + модель |

### Как настроить

**SDK (Anthropic API):**
```bash
cp .env.example .env
# Откройте .env, впишите ANTHROPIC_API_KEY=sk-ant-...
```

**CLI (Claude Code subscription):**
```bash
# Установите Claude Code (https://docs.claude.com/en/docs/claude-code)
npm install -g @anthropic-ai/claude-code
claude    # один раз залогиниться
```
Скрипт сам найдёт `claude` в PATH.

**Ollama (полностью локально):**
```bash
# Скачайте https://ollama.com/download
ollama pull qwen2.5:7b-instruct      # 4.5 GB, поддерживает русский
ollama serve                          # запустится в трее
# Готово, скрипт найдёт по http://localhost:11434
```

Можно форсировать выбор: `python process_local_audio.py --backend ollama` (или `sdk`/`cli`).

---

## 🎙 Whisper: расшифровка аудио

Тоже выбирается автоматически или через флаги:

| Engine | Hardware | Скорость | Что нужно |
|---|---|---|---|
| **faster-whisper** | CPU | 0.3-3× realtime | `pip install faster-whisper imageio-ffmpeg` |
| **faster-whisper + CUDA** | NVIDIA GPU | 15-30× realtime | + `pip install torch --index-url https://download.pytorch.org/whl/cu124` |
| **OpenVINO** (`transcribe_openvino.py`) | Intel iGPU/CPU | 5-10× realtime | `pip install openvino-genai librosa` |

По умолчанию используется faster-whisper. Для Intel Arc / iGPU добавьте `--openvino`:

```bash
python bulk_export/process_lectures.py --openvino --source "C:\Lectures"
```

См. [docs/06-whisper-performance.md](docs/06-whisper-performance.md) для бенчмарков.

---

## 📂 Pipeline 1: Локальная папка аудио/видео

Для тех, у кого записи в обычной папке (а не в Plaud):

```bash
python bulk_export/process_local_audio.py --source "D:\AudioRecordings" --output "C:\_Results"
```

**Что делает:**
1. Сканирует source-папку на новые аудио (`.wav .mp3 .m4a .opus .ogg .flac` и аудио-дорожки видео).
2. Запоминает обработанные в `.processed.json` рядом с источниками.
3. Если рядом с `recording.wav` уже есть `recording-transcript.txt` — берёт его готовым. Иначе → Whisper.
4. Claude генерирует `title` + summary по фиксированной схеме (Тема / Тезисы / Договорённости и задачи / Открытые вопросы / Участники).
5. Складывает в `<output>/Записи с DD.MM.YYYY по DD.MM.YYYY/<safe_title>.{pdf,md,mp3,transcript.txt}` + `INDEX.md`.

См. [docs/02-local-audio.md](docs/02-local-audio.md).

---

## 📚 Pipeline 2: Видео-лекции → главы книги

```bash
python bulk_export/process_lectures.py --openvino \
   --source "C:\Lectures\AI" \
   --output "C:\Book\AI" \
   --book-title "Курс ИИ для университетов" \
   --book-author "Имя Автора"
```

Каждое видео → отдельная **глава учебника**: Введение / Основные идеи (5-12 разделов с раскрытием) / Примеры / Ключевые термины / Выводы / Вопросы для самопроверки. Плюс растущий `BOOK_INDEX.md` — оглавление будущей книги.

См. [docs/03-lectures.md](docs/03-lectures.md).

---

## 🎬 Pipeline 3: YouTube Shorts → метаданные

```bash
python bulk_export/process_shorts.py \
   --source "C:\Shorts\AI" \
   --topic "Как работает ChatGPT" \
   --series "Истории в баре" \
   --openvino
```

Для каждого видео — `<stem>-youtube.md` с готовыми полями для YouTube:
- Hook (первая фраза)
- Title + 4 альтернативы (для A/B-тестов в VidIQ)
- Описание (с CTA и хэштегами)
- Tags (для поля Tags)
- Hashtags (с `#Shorts` в начале)
- Закреплённый комментарий

См. [docs/04-shorts.md](docs/04-shorts.md).

---

## 🏗 Архитектура

```
                ┌──────────────────────────────────────────────────────┐
                │  Source: Plaud Web  /  Local folder  /  Lectures     │
                └────────────────────────┬─────────────────────────────┘
                                         ▼
            ┌──────────────────────────────────────────────────────────┐
            │  Pull / Discover                                         │
            │    plaud_export.py  |  process_local_audio.py  |  ...    │
            └────────────────────────┬─────────────────────────────────┘
                                     ▼
            ┌──────────────────────────────────────────────────────────┐
            │  Whisper (transcribe.py / transcribe_openvino.py)        │
            │  audio_transcriber/{inbox,processing,done}               │
            └────────────────────────┬─────────────────────────────────┘
                                     ▼
            ┌──────────────────────────────────────────────────────────┐
            │  LLM (Claude SDK | Claude CLI | Ollama)                  │
            │  → title + summary/chapter/youtube-meta                  │
            └────────────────────────┬─────────────────────────────────┘
                                     ▼
            ┌──────────────────────────────────────────────────────────┐
            │  Render: pdf_render.py → ReportLab + Calibri TTF         │
            └────────────────────────┬─────────────────────────────────┘
                                     ▼
            ┌──────────────────────────────────────────────────────────┐
            │  Output: <safe_title>.{pdf,md,transcript.txt,mp3/.mov}  │
            │          + INDEX.md / BOOK_INDEX.md                      │
            └──────────────────────────────────────────────────────────┘
```

Полное описание — [docs/05-architecture.md](docs/05-architecture.md).

---

## 📋 Slash-команды для Claude Code

Если установлен `claude` CLI, скопируйте команды в `~/.claude/commands/`:

```bash
mkdir -p ~/.claude/commands
cp claude-commands/*.md ~/.claude/commands/
```

Доступные:

| Команда | Действие |
|---|---|
| `/plaud` | Полный pipeline Plaud (pull → Whisper → LLM → PDF) |
| `/plaud-status` | Статус последнего batch'а в виде таблицы |
| `/local` | Обработка локальной папки `.wav/.mp3` |
| `/lectures` | Видео-лекции → главы книги |

---

## 🤝 Contributing

См. [CONTRIBUTING.md](CONTRIBUTING.md).

## 🔒 Безопасность

См. [SECURITY.md](SECURITY.md). Никаких реальных токенов и записей в коммитах.

## 📜 Лицензия

MIT — см. [LICENSE](LICENSE).

## 🙏 Использует

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — Whisper inference
- [OpenVINO](https://github.com/openvinotoolkit/openvino) — Intel iGPU acceleration
- [Anthropic Claude](https://www.anthropic.com/) — LLM
- [Ollama](https://ollama.com/) — локальные LLM
- [ReportLab](https://www.reportlab.com/) — PDF rendering
