---
description: "Качает новые записи из Plaud acc2, делает Whisper-транскрипцию (если нужно), Claude-summary + PDF, складывает в C:\\_Dictophone\\Записи с ДД.ММ.ГГГГ по ДД.ММ.ГГГГ\\"
allowed-tools: Bash, Read, Monitor, TodoWrite
argument-hint: "[--account 1|2 (default 2)] [--skip-download] [--skip-whisper]"
---

Запусти стандартный пайплайн скачивания и обработки новых записей Plaud:

```bash
cd "C:/AI Native University/_Archive_versions/_to_delete/empty_dirs/fromPlaud/bulk_export" && python process_new_plaud_batch.py $ARGUMENTS
```

Если `$ARGUMENTS` пустые — добавь `--account 2` (default для вашей основной учётки).

Запускай **в фоне** (`run_in_background: true`) и сразу включи мониторинг файла `bulk_export/_logs/new_batch_<TS>/log.txt` через Monitor (поймай ключевые события: [whisper], tokens in=, [range], [dest], [index], [mirror], [verify], === Done).

Параллельно настрой каждые 10 минут вывод таблицы статусов:
```
python "~/.claude/plans/_status_table.py"
```
(скрипт уже умеет сам находить актуальный batch по существованию SUCCESS-маркера).

По завершении (когда появится файл `SUCCESS` в batch-папке) выведи финальный отчёт: какие записи переименованы, путь к папке с PDF, статистика (Plaud vs Whisper transcripts, общее количество PDF, ошибки).

Не задавай уточняющих вопросов — пайплайн полностью идемпотентный и сам разбирается с новыми/старыми записями.
