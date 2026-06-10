---
description: "Обработать папку с локальными .wav/.mp3: Whisper → Claude → PDF → папка с диапазоном дат"
allowed-tools: Bash, Read, Monitor
argument-hint: "--source \"C:\\path\\to\\audio\" [--output ...] [--skip-whisper] [--limit N]"
---

Запусти пайплайн локальной обработки аудио. Скрипт лежит в `bulk_export/process_local_audio.py` рядом со скриптом `process_new_plaud_batch.py`. Найди его — есть две возможные локации:

1. **Portable** (рекомендуется на чужой машине): `<любая>\plaud-pipeline-portable\bulk_export\`
2. **Original** (исходная машина): `C:\AI Native University\_Archive_versions\_to_delete\empty_dirs\fromPlaud\bulk_export\`

Если `$ARGUMENTS` пустые — НЕ переспрашивай. Скрипт сам подхватит дефолты из `user-config.json` (создаётся при `python init.py` или `setup.bat`). Если файла нет — сообщи пользователю «запусти `python init.py` для первичной настройки путей».

Минимальный запуск (все пути из user-config.json):
```bash
python "<path-to>/bulk_export/process_local_audio.py"
```

Разовое переопределение:
```bash
python "<path-to>/bulk_export/process_local_audio.py" --source "C:\путь\к\аудио" --output "C:\другая_папка"
```

Запускай **в фоне** (`run_in_background: true`). Включи Monitor на свежий `bulk_export/_logs/local_batch_<TS>/log.txt` (события: `[whisper]`, `tokens in=`, `[range]`, `[dest]`, `[index]`, `[verify]`, `=== Done`).

По завершении (SUCCESS файл в batch-папке) выведи итог: количество обработанных, путь к папке с PDF, ошибки.

Идемпотентность: pipeline хранит `.processed.json` в source-папке — повторный запуск возьмёт только новые файлы.
