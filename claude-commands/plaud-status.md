---
description: "Таблица статуса последнего Plaud-batch'а: что в Whisper, что ждёт Claude, что готово (PDF)"
allowed-tools: Bash, Read
argument-hint: "[all|history] — без аргументов показывает только последнюю партию"
---

Выведи статус последнего Plaud-batch'а в виде таблицы. Запусти:

```bash
python "~/.claude/plans/_status_table.py"
```

Если в `$ARGUMENTS` есть `all` или `history` — дополнительно покажи список всех batch-папок в `C:/AI Native University/_Archive_versions/_to_delete/empty_dirs/fromPlaud/bulk_export/_logs/` с маркером `SUCCESS` или `running`:

```bash
ls -t "C:/AI Native University/_Archive_versions/_to_delete/empty_dirs/fromPlaud/bulk_export/_logs/" | grep "^new_batch_" | while read d; do
  if [ -f "C:/AI Native University/_Archive_versions/_to_delete/empty_dirs/fromPlaud/bulk_export/_logs/$d/SUCCESS" ]; then
    echo "✓ $d"
  else
    echo "⚙ $d (running/incomplete)"
  fi
done
```

Если в текущей сессии запущен фоновый pipeline-процесс — отметь это.

Никаких дополнительных действий. Только показать таблицу, никаких побочных эффектов.
