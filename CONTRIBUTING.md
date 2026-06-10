# Contributing

Спасибо за интерес к проекту! Этот документ описывает, как помочь.

## Что приветствуется

- 🐞 **Багрепорты** — открывайте issue с описанием шагов воспроизведения, версией Python, ОС, логом ошибки.
- ✨ **Новые backend'ы LLM** — кроме `sdk` / `cli` / `ollama` можно добавить vLLM, LM Studio, OpenAI-совместимые сервисы.
- 🌐 **Поддержка других диктофонов** — кроме Plaud (Otter.ai, Plaud Note, аналоги). Архитектура pull → pipeline даёт хорошую возможность для расширения.
- 🎓 **Новые типы вывода** — кроме «summary встречи», «глава книги», «YouTube Shorts»: статья в блог, презентация, заметка в Obsidian, и т.д.
- 📝 **Документация и переводы** — улучшения README, гайдов в `docs/`, переводы на английский/другие языки.
- 🧪 **Тесты** — сейчас тестов почти нет, любые приветствуются.

## Перед PR

1. **Безопасность** — прочитайте [SECURITY.md](SECURITY.md). Никаких реальных токенов, ключей, путей с именем пользователя, записей в коммитах.
2. **Сухой запуск** — проверьте, что ваш скрипт не падает на пустой папке / отсутствии конфига. Дружелюбные сообщения об ошибках.
3. **Опциональность** — если добавляете тяжёлую зависимость (например, ML-фреймворк) — сделайте её опциональной импортом внутри функции, чтобы базовая установка оставалась лёгкой.
4. **Идемпотентность** — все pipeline-скрипты должны корректно перезапускаться (пропускать уже сделанное).
5. **Запустите верификацию:**
   ```bash
   # Найти что-нибудь похожее на секрет
   git diff --cached | grep -E "sk-ant-|bearer ey|eyJhbGciOi" && echo "STOP — секрет" || echo "OK"
   ```

## Стиль кода

- Python 3.11+, type hints приветствуются, но не обязательны.
- Сообщения в логе — на русском (это пользовательская аудитория проекта), комментарии и docstrings — на русском или английском.
- UTF-8 везде, на Windows форсируем через `sys.stdout.reconfigure(encoding="utf-8")` в начале скрипта.
- Длинные операции — фоновые, с прогресс-логом.

## Архитектурные принципы

См. [docs/05-architecture.md](docs/05-architecture.md). Кратко:

1. **Pull → Transcribe → Summarise → Render → Index.** Каждая фаза — отдельный скрипт, можно перезапустить независимо.
2. **Folder-based state.** Идемпотентность через файлы-маркеры (`.processed.json`, `SUCCESS`, `-transcript.txt` рядом с аудио).
3. **Auto-detect backends.** LLM-backend, Whisper-engine — выбираются по доступности, не по жёсткой настройке.
4. **No vendor lock.** SDK Anthropic — лишь один из путей. CLI и Ollama — равноправные альтернативы.

## Get started

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python init.py            # настройка путей, появится user-config.json (не коммитим)
```

Запустить smoke test:
```bash
python bulk_export/process_local_audio.py --help
python bulk_export/process_lectures.py --help
python bulk_export/process_shorts.py --help
```

## Code of Conduct

Будьте уважительны. Конструктивная критика > эмоции. Если кому-то некомфортно — `Issues → New → I'm being harassed`.
