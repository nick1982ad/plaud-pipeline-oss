---
description: "Видео-лекции → главы будущей книги (Whisper извлекает аудио + Claude пишет главу)"
allowed-tools: Bash, Read, Monitor
argument-hint: "[--source ... --output ... --openvino --book-title ... --book-author ...]"
---

Запусти лекционный pipeline. Скрипт — `bulk_export/process_lectures.py` рядом со скриптом `process_local_audio.py`. Найди один из путей:

1. **Portable**: `<любая>\plaud-pipeline-portable\bulk_export\process_lectures.py`
2. **Original**: `C:\AI Native University\_Archive_versions\_to_delete\empty_dirs\fromPlaud\bulk_export\process_lectures.py`

Если `$ARGUMENTS` пустые — скрипт берёт `video_source` и `book_output` из `user-config.json`. Если конфиг ещё не создан — сообщи «запусти `python init.py`».

Минимальный запуск:
```bash
python "<path>/bulk_export/process_lectures.py" --openvino
```

С аргументами:
```bash
python "<path>/bulk_export/process_lectures.py" --openvino --source "C:\Lectures\AI" --output "C:\Book\AI" --book-title "Курс ИИ для университетов" --book-author "Имя Автора"
```

Запускай **в фоне** (`run_in_background: true`), затем Monitor на свежий `bulk_export/_logs/lectures_<TS>.log` (события: `[extract]`, `[whisper]`, `tokens in=`, `[book-index]`, `=== Done`).

**Что делает**:
1. Сканирует source-папку на видео (`.mp4 .mkv .mov .webm .avi .m4v .wmv .flv .3gp .ts .mts`) и аудио (`.mp3 .wav .m4a ...`).
2. Для каждого нового файла: извлекает аудио ffmpeg'ом в 16k mono WAV → копия в `audio_transcriber/inbox/`.
3. Прогоняет через Whisper (по умолчанию OpenVINO, опционально CPU).
4. Получает у Claude **полноценную главу книги** (Введение / Основные идеи / Примеры / Ключевые термины / Выводы / Вопросы для самопроверки) — не короткое summary.
5. Кладёт в `book_output/`: оригинал видео, transcript, .md, .pdf главы.
6. Обновляет `BOOK_INDEX.md` — растущее оглавление книги.

**Идемпотентность**: `.lectures_processed.json` в source — повторный запуск возьмёт только новые видео.
