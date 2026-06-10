# Архитектура

## Принципы

1. **Pull → Transcribe → Summarise → Render → Index.** Каждая фаза — отдельный скрипт, перезапускается независимо.
2. **Folder-based state.** Идемпотентность через файлы-маркеры (`.processed.json`, `SUCCESS`, `-transcript.txt`).
3. **Auto-detect backends.** LLM выбирается по доступности: SDK → CLI → Ollama. Whisper — faster-whisper или OpenVINO.
4. **No vendor lock.** Anthropic — лишь один из путей. Можно полностью оффлайн через Ollama + локальный Whisper.
5. **No data leakage.** Записи, транскрипты, токены — никогда не попадают в git (см. `.gitignore`).

## Слои

### 1. Pull / Discover
Получение исходных файлов и их учёт.

| Скрипт | Источник |
|---|---|
| `bulk_export/plaud_export.py` | Plaud Web API |
| `bulk_export/process_local_audio.py` (discover-фаза) | Локальная папка |
| `bulk_export/process_lectures.py` (discover-фаза) | Локальная папка с видео |
| `bulk_export/process_shorts.py` (discover-фаза) | Локальная папка с шортсами |

Состояние: `.processed.json` / `.lectures_processed.json` рядом с источниками. `output_account*/` для Plaud.

### 2. Transcribe (Whisper)

| Скрипт | Backend |
|---|---|
| `audio_transcriber/transcribe.py` | faster-whisper (CPU + CUDA) |
| `audio_transcriber/transcribe_openvino.py` | OpenVINO (Intel iGPU/CPU) |

Структура:
```
audio_transcriber/
├── inbox/         ← кладёте аудио сюда
├── processing/    ← скрипт переносит в работу
└── done/          ← готовые transcripts: <stem>-transcript.txt + <stem>-meta.json
```

Запуск: `transcribe.py --mode once` (один прогон) или `--mode watch` (демон).

### 3. Summarise (LLM)

Универсальная функция `call_claude(backend, ...)` в каждом pipeline-скрипте. Три реализации:

```python
def pick_backend(log, prefer="auto"):
    if prefer in (None, "auto", "sdk") and have_anthropic_key():
        return "sdk", anthropic.Anthropic()
    if prefer in (None, "auto", "cli") and which("claude"):
        return "cli", which("claude")
    if prefer in (None, "auto", "ollama") and ollama_reachable():
        return "ollama", ollama_model_name()
    sys.exit("no backend available")
```

Каждый backend получает один и тот же `SYSTEM_PROMPT` и `user_prompt`, отдаёт JSON `{title, summary_md}` (или `{title, chapter_md}` для лекций, или полную структуру YouTube-метаданных для шортсов).

### 4. Render (PDF)

`bulk_export/pdf_render.py` — reportlab-based рендер с Calibri TTF из `C:\Windows\Fonts` для надёжной кириллицы. Поддерживает H1-H4, code blocks, inline code, fenced code, lists, tables, blockquotes, hr.

### 5. Index

После пакета — обновляется индекс:
- `INDEX.md` для пакетов аудио-встреч.
- `BOOK_INDEX.md` для лекций.

Индексы — markdown, можно открыть в Obsidian / VS Code / любом маркдаун-просмотрщике.

## Маршруты данных

```
   ┌─ Plaud Web ──────► plaud_export.py ──┐
   │                                       │
   ├─ Local folder ──► discover() ────────┤
   │                                       │
   └─ Video lecture ► ffmpeg extract ────►│
                                          │
                            ┌─────────────▼─────────────┐
                            │  audio_transcriber/inbox  │
                            └─────────────┬─────────────┘
                                          ▼
                            ┌──────────────────────────┐
                            │ Whisper (small/medium)   │
                            │ → done/<stem>-transcript │
                            └─────────────┬────────────┘
                                          ▼
                            ┌──────────────────────────┐
                            │ Claude/Ollama call_*()   │
                            │ → JSON {title, body_md}  │
                            └─────────────┬────────────┘
                                          ▼
                            ┌──────────────────────────┐
                            │ pdf_render → PDF/MD/txt  │
                            └─────────────┬────────────┘
                                          ▼
                            ┌──────────────────────────┐
                            │ output/<batch>/          │
                            │   ├ <safe_title>.pdf     │
                            │   ├ <safe_title>.md      │
                            │   ├ <safe_title>.<ext>   │
                            │   ├ <safe_title>-tr.txt  │
                            │   └ INDEX.md             │
                            └──────────────────────────┘
```

## Идемпотентность — где что записывается

| Уровень | Файл-маркер | Защищает от |
|---|---|---|
| Pull от Plaud | `existing_dir` skip-set | Повторного скачивания |
| Whisper | `done/<stem>-transcript.txt` | Двойной расшифровки |
| Claude (audio batch) | `<batch>/.checkpoint/<stem>.json` | Дублей API-вызовов |
| Lectures | `.lectures_processed.json` в source | Перегенерации главы |
| Shorts | наличие `<stem>-youtube.md` | Перегенерации метаданных |
| Batch success | `<batch>/SUCCESS` | Запуска тех же шагов после Done |

## Расширение

Хотите добавить:
- **Новый источник** (Otter.ai, Plaud Note, локальный диктофон через Bluetooth)
- **Новый LLM backend** (OpenAI, Mistral API, vLLM локально)
- **Новый формат вывода** (статья в блог, презентация, заметка в Obsidian)

— смотрите [CONTRIBUTING.md](../CONTRIBUTING.md). Архитектура pull/transcribe/summarise/render позволяет добавлять модули без рисков для существующих.
