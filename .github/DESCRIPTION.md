# GitHub Description / About / Topics

Готовые варианты описаний для разных полей GitHub. Скопируйте нужное в **Settings → About**.

---

## 🇷🇺 Description (русская версия)

Поле «Description» под названием репозитория. Max ~350 символов.

### Самый короткий (160 знаков)

```
Локальный pipeline: записи Plaud / .wav / видеолекции → транскрипты Whisper → саммари, главы книги или YouTube-метаданные через Claude/Ollama. PDF + Markdown.
```

### Стандартный (260 знаков)

```
Self-hosted pipeline для обработки аудио и видео. Скачивает записи из Plaud, расшифровывает локальным Whisper (CPU/CUDA/Intel iGPU), и через Claude/Ollama превращает в структурированные PDF — саммари встреч, главы книги или YouTube-метаданные.
```

### Расширенный (340 знаков)

```
Self-hosted pipeline для пакетной обработки аудио и видеозаписей. Pull из Plaud Web (через ваш JWT), либо локальная папка. Whisper расшифровка на CPU / NVIDIA / Intel iGPU. Затем Claude API / Claude Code subscription / Ollama превращает транскрипт в PDF — саммари встреч, главы книги или YouTube Shorts метаданные. Идемпотентно, без облака для ваших данных.
```

---

## 🇬🇧 Description (English version)

For international discoverability — recommended to keep English alongside Russian.

### Short (160 chars)

```
Self-hosted pipeline: Plaud / .wav / video → local Whisper transcripts → Claude/Ollama-generated meeting summaries, book chapters, or YouTube metadata. PDF + Markdown.
```

### Standard (260 chars)

```
Local pipeline that pulls recordings from Plaud (via your own JWT), transcribes them with Whisper (CPU/CUDA/Intel iGPU), then turns transcripts into beautifully-rendered PDF summaries, book chapters, or YouTube Shorts metadata via Claude API or local Ollama. Idempotent. Privacy-first.
```

---

## 🏷 Topics (GitHub Settings → About → Topics)

Введите по одному. До 20 тегов. Помогает находить ваш репо.

### Главные (must have)

- `whisper`
- `claude`
- `transcription`
- `audio-processing`
- `local-llm`
- `ollama`

### Источники / форматы

- `plaud`
- `meeting-summarizer`
- `lecture-notes`
- `youtube-shorts`
- `podcast-pipeline`

### Архитектура / стек

- `python`
- `pipeline`
- `self-hosted`
- `privacy-first`
- `reportlab`
- `openvino`
- `faster-whisper`

### Применение

- `note-taking`
- `productivity`

---

## 📝 About — длинный текст (если использовать README как About)

GitHub использует README.md как About по умолчанию — отдельно настраивать не нужно.

Если хотите вставить альтернативный текст в About-блок (правая колонка на странице репо), используйте «Расширенный» Description выше.

---

## 🔗 Website (Settings → About → Website)

Если у вас есть GitHub Pages, личный блог или Notion-страница с дополнительными материалами — укажите. Иначе оставьте пустым.

Рекомендуемое поле:
```
https://<your-username>.github.io/plaud-pipeline/
```
(работает только если включите GitHub Pages в Settings → Pages → Deploy from `main` → `/docs/`)

---

## 🎨 Social preview image

GitHub предлагает картинку 1280×640 для socials (когда ссылку на репо постят в Twitter/LinkedIn).

Заготовка для дизайна:
- Левая половина: ASCII-диаграмма архитектуры (из intro.md)
- Правая половина: пример PDF-главы крупным планом
- Логотип / название «Plaud Pipeline» жирно сверху

Закидывается в **Settings → Social preview**.

---

## 📋 Pinned issue / Discussion

Полезно создать сразу после публикации:

### Pinned issue: «Roadmap»

```markdown
## Текущие фичи
- [x] Plaud pull через JWT
- [x] Whisper CPU/CUDA/OpenVINO
- [x] Claude SDK / CLI / Ollama backends
- [x] Меетинг-саммари
- [x] Главы книги
- [x] YouTube Shorts метаданные

## Планы

### v0.2
- [ ] Speaker diarization (pyannote.audio)
- [ ] Перевод (на английский для русских записей)
- [ ] Docker-образ

### v0.3
- [ ] OpenAI / Mistral backends
- [ ] Otter.ai source connector
- [ ] Obsidian-vault output format

Голосуйте 👍 за то, что нужно вам первым делом.
```

### Pinned discussion: «Show & Tell»

> Делитесь, как вы используете Plaud Pipeline. Скриншоты глав книги, кейсы по YouTube, странные форматы — всё интересно.
```
