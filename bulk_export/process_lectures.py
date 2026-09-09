#!/usr/bin/env python3
"""
process_lectures.py
===================

Pipeline для **видео-лекций** → главы будущей книги.

Берёт mp4/mkv/mov/... из заданной папки, извлекает аудио через ffmpeg,
прогоняет через Whisper (faster-whisper или transcribe_openvino.py),
затем зовёт Claude с *учебным* промптом (глубокий конспект, не summary
встречи). Кладёт в папку результата:

    <safe_title>.<ext>            ← оригинал видео (копия)
    <safe_title>-transcript.txt   ← полный транскрипт
    <safe_title>.md               ← глава в Markdown
    <safe_title>.pdf              ← глава, отрендеренная в PDF
    BOOK_INDEX.md                 ← растущее оглавление будущей книги

CLI:
  python process_lectures.py --source "C:\\Lectures" --output "C:\\Book"
  python process_lectures.py --source ...  --openvino           # Intel iGPU
  python process_lectures.py --source ...  --book-title "Курс ИИ" --book-author "Имя Автора"
  python process_lectures.py --source ...  --backend ollama     # full offline
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# UTF-8
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = Path(__file__).resolve().parent
PORTABLE_ROOT = ROOT.parent
TRANSCRIBER = PORTABLE_ROOT / "audio_transcriber"
INBOX = TRANSCRIBER / "inbox"
DONE = TRANSCRIBER / "done"
LOG_ROOT = ROOT / "_logs"
USER_CONFIG_PATH = PORTABLE_ROOT / "user-config.json"

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TRANSCRIPT_CHARS = 200_000
CLAUDE_MAX_TOKENS = 8192
# Chapters run longer than meeting summaries. Override with CLAUDE_CLI_TIMEOUT.
CLI_TIMEOUT_SEC = int(os.environ.get("CLAUDE_CLI_TIMEOUT", "900"))

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv",
              ".flv", ".3gp", ".ts", ".mts"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".opus", ".ogg", ".flac", ".aac", ".wma"}

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_MMDD_RE = re.compile(r"^(\d{2})-(\d{2})[\s_]")
_YMD_RE  = re.compile(r"^(\d{4})[-_](\d{2})[-_](\d{2})")


# ---------- User config -------------------------------------------

def load_user_config() -> dict:
    base = {
        "video_source":  r"C:\Lectures",
        "book_output":   r"C:\Book",
        "backend":       "auto",
        "ollama_model":  "qwen2.5:7b-instruct",
        "ollama_url":    "http://localhost:11434",
        "anthropic_api_key": "",
    }
    if USER_CONFIG_PATH.exists():
        try:
            return {**base, **json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return base


UC = load_user_config()


# ---------- API key / backend -------------------------------------

def load_api_key() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    candidates = [
        PORTABLE_ROOT / ".env",
        ROOT / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists(): continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value and not value.startswith("PASTE_"):
                    os.environ["ANTHROPIC_API_KEY"] = value
                    return True
    if UC.get("anthropic_api_key"):
        os.environ["ANTHROPIC_API_KEY"] = UC["anthropic_api_key"]
        return True
    return False


def find_claude_cli():
    for name in ("claude.cmd", "claude.exe", "claude"):
        p = shutil.which(name)
        if p: return p
    return None


def find_ollama():
    try:
        import urllib.request
        url = UC.get("ollama_url", "http://localhost:11434")
        with urllib.request.urlopen(f"{url}/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        target = UC.get("ollama_model", "qwen2.5:7b-instruct")
        tags = [m.get("name", "") for m in data.get("models", [])]
        if target in tags: return target
        for t in tags:
            if t.startswith(target.split(":")[0]): return t
    except Exception:
        pass
    return None


def pick_backend(log, prefer: str = "auto"):
    have_key = load_api_key()
    if prefer in (None, "auto", "sdk") and have_key:
        try:
            import anthropic
            log("[backend] Anthropic SDK")
            return "sdk", anthropic.Anthropic()
        except ImportError: pass
    if prefer in (None, "auto", "cli"):
        cli = find_claude_cli()
        if cli:
            log(f"[backend] Claude CLI subscription: {cli}")
            return "cli", cli
    if prefer in (None, "auto", "ollama"):
        m = find_ollama()
        if m:
            log(f"[backend] LOCAL Ollama model={m}")
            return "ollama", m
    sys.exit("Нет доступного LLM backend (ставь ANTHROPIC_API_KEY, claude CLI или Ollama)")


# ---------- Logging ------------------------------------------------

class Log:
    def __init__(self, log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = log_path.open("a", encoding="utf-8")
    def __call__(self, msg=""):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        print(line, flush=True); self.fp.write(line + "\n"); self.fp.flush()
    def close(self): self.fp.close()


# ---------- ffmpeg audio extract ----------------------------------

def get_ffmpeg() -> str:
    """Try imageio-ffmpeg first, fallback to system ffmpeg."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    if p: return p
    sys.exit("ffmpeg не найден. Установи: pip install imageio-ffmpeg")


def extract_audio(video_path: Path, wav_path: Path, log) -> bool:
    """Извлекает audio из видео в 16k mono WAV для Whisper."""
    ffmpeg = get_ffmpeg()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-i", str(video_path),
           "-vn", "-ar", "16000", "-ac", "1",
           "-c:a", "pcm_s16le",
           "-loglevel", "warning",
           str(wav_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=3600)
        if proc.returncode != 0:
            log(f"  ! ffmpeg exit {proc.returncode}: {proc.stderr[-300:]}")
            return False
        return wav_path.exists() and wav_path.stat().st_size > 0
    except subprocess.TimeoutExpired:
        log("  ! ffmpeg timeout")
        return False
    except Exception as e:
        log(f"  ! ffmpeg error: {e}")
        return False


# ---------- State (idempotency) -----------------------------------

def load_state(source: Path) -> dict:
    p = source / ".lectures_processed.json"
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}


def save_state(source: Path, state: dict):
    p = source / ".lectures_processed.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_new(source: Path, state: dict) -> list:
    new_items = []
    for f in sorted(source.iterdir()):
        if not f.is_file(): continue
        ext = f.suffix.lower()
        if ext not in VIDEO_EXTS and ext not in AUDIO_EXTS: continue
        if f.stem in state: continue
        new_items.append((f.stem, f))
    return new_items


# ---------- Date detection ----------------------------------------

def detect_date(stem: str, src: Path):
    m = _YMD_RE.match(stem)
    if m:
        try: return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError: pass
    m = _MMDD_RE.match(stem)
    if m:
        try:
            month, day = int(m.group(1)), int(m.group(2))
            d = datetime(datetime.now().year, month, day)
            if (d - datetime.now()).days > 60:
                d = datetime(datetime.now().year - 1, month, day)
            return d
        except ValueError: pass
    try: return datetime.fromtimestamp(src.stat().st_mtime)
    except OSError: return None


# ---------- Whisper -----------------------------------------------

def run_whisper(stems_with_wav: list, openvino: bool, device: str, log):
    """stems_with_wav: list of (stem, wav_path_in_inbox)."""
    if not stems_with_wav:
        log("[whisper] nothing to do")
        return
    log(f"[whisper] starting on {len(stems_with_wav)} files "
        f"({'OpenVINO ' + device if openvino else 'faster-whisper CPU'})")
    if openvino:
        cmd = [sys.executable, "-u", str(TRANSCRIBER / "transcribe_openvino.py"),
               "--mode", "once", "--device", device]
    else:
        cmd = [sys.executable, "-u", str(TRANSCRIBER / "transcribe.py"), "--mode", "once"]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(cmd, cwd=str(TRANSCRIBER),
                                stdout=subprocess.PIPE, stderr=None,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, env=env)
        for line in proc.stdout:
            if line.strip():
                log(f"[whisper]  {line.rstrip()}")
        proc.wait()
    except Exception as e:
        log(f"[whisper] error: {e}")


# ---------- Claude: chapter from transcript -----------------------

CHAPTER_SYSTEM = """Ты — редактор, готовящий учебник на основе видеолекций.
Получаешь полный транскрипт одной лекции и должен превратить его в главу будущей книги.

Верни СТРОГО валидный JSON:

{
  "title": "<заголовок главы, 5-15 слов на русском>",
  "chapter_md": "<полная глава в Markdown>"
}

ТРЕБОВАНИЯ К title:
- 5-15 слов, на русском, передаёт тему лекции.
- Без даты, без эмодзи, без "Запись", "Лекция", "Видео".
- ЗАПРЕЩЕНЫ Windows-FS символы: < > : " / \\ | ? *
- Без точки в конце.

ТРЕБОВАНИЯ К chapter_md (СТРОГО следуй шаблону):

## Введение
2-4 абзаца: о чём глава, почему важно, что читатель узнает в конце.

## Основные идеи
5-12 идей, каждая раскрыта в 2-5 абзацах. Используй ### подзаголовки для идей:

### Идея 1: <название>
Подробное раскрытие. Логика, нюансы, контекст. Без воды.

### Идея 2: <название>
...

## Примеры и иллюстрации
2-5 конкретных примеров из лекции (если в речи их называли). Если примеров нет — пропусти секцию.

## Ключевые термины
- **Термин 1** — определение.
- **Термин 2** — определение.
(5-15 терминов; если в лекции терминов нет — пропусти секцию.)

## Выводы
3-7 пунктов: главные тезисы, которые читатель должен запомнить.

## Вопросы для самопроверки
3-7 вопросов, проверяющих понимание ключевых идей. Если применимо.

ПРАВИЛА:
- Полнота важнее краткости. Это не summary встречи, это глава книги.
- Сохраняй уникальные формулировки лектора (можно цитировать в кавычках).
- Реструктурируй порядок изложения, если в речи он скачет — читателю должно быть логично.
- Если в лекции были отвлечения / болтовня / технические заминки — игнорируй.
- Если транскрипт обрывочный — честно отметь в "## Введение".

ФОРМАТ ОТВЕТА: только JSON, без markdown-обёрток ```json"""


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m: return None
    try: return json.loads(m.group(0))
    except json.JSONDecodeError: return None



def _cli_error_detail(proc) -> str:
    """Human-readable reason a `claude --print` run failed.

    With --output-format json the CLI reports API failures in STDOUT, as an
    envelope carrying is_error / api_error_status / result, and leaves stderr
    empty. Logging stderr alone therefore drops the actual cause — expired
    OAuth token, rate limit, unknown model — and shows a bare exit code.
    """
    parts = []
    raw = (proc.stdout or "").strip()
    if raw:
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            parts.append(f"stdout: {raw[:300]}")
        else:
            status = env.get("api_error_status")
            if status:
                parts.append(f"API {status}")
            msg = env.get("result") or env.get("error") or ""
            if msg:
                parts.append(re.sub(r"\s+", " ", str(msg))[:300])
    err = (proc.stderr or "").strip()
    if err:
        parts.append(f"stderr: {err[:200]}")
    return " | ".join(parts) or "(no detail in either stdout or stderr)"

def call_claude(backend, model: str, stem: str, transcript: str, log):
    kind, payload = backend
    if kind == "sdk":
        return _claude_sdk(payload, model, stem, transcript, log)
    if kind == "cli":
        return _claude_cli(payload, model, stem, transcript, log)
    if kind == "ollama":
        return _claude_ollama(payload, stem, transcript, log)


def _claude_sdk(client, model, stem, transcript, log):
    user = (f"Транскрипт лекции (имя файла: {stem}):\n\n"
            f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
            "Сделай главу книги. Верни JSON с title и chapter_md.")
    messages = [{"role": "user", "content": user}]
    for attempt in (1, 2, 3):
        try:
            resp = client.messages.create(
                model=model, max_tokens=CLAUDE_MAX_TOKENS,
                system=[{"type": "text", "text": CHAPTER_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=messages,
            )
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                log(f"  ! rate-limit attempt {attempt}, sleep 30s"); time.sleep(30); continue
            log(f"  ! API error attempt {attempt}: {e}"); time.sleep(5); continue
        usage = resp.usage
        log(f"  tokens in={usage.input_tokens} cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
            f"out={usage.output_tokens}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = _extract_json(text)
        if data and "title" in data and "chapter_md" in data:
            return data
        log(f"  ! invalid JSON attempt {attempt}; head: {text[:120]!r}")
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user",
                         "content": "Верни ТОЛЬКО валидный JSON с полями title и chapter_md."})
    return None


def _claude_cli(claude_path, model, stem, transcript, log):
    user = (f"Транскрипт лекции (имя файла: {stem}):\n\n"
            f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
            "Сделай главу книги. Верни JSON с title и chapter_md.")
    for attempt in (1, 2, 3):
        try:
            proc = subprocess.run(
                [claude_path, "--print", "--model", model,
                 "--system-prompt", CHAPTER_SYSTEM,
                 "--output-format", "json",
                 "--no-session-persistence"],
                input=user, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            log(f"  ! CLI did not answer within {CLI_TIMEOUT_SEC}s (attempt {attempt}); "
                f"raise CLAUDE_CLI_TIMEOUT to allow longer"); continue
        if proc.returncode != 0:
            log(f"  ! CLI exit {proc.returncode}: {_cli_error_detail(proc)}"); time.sleep(5); continue
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            log(f"  ! CLI envelope not JSON attempt {attempt}"); continue
        if env.get("is_error"):
            log(f"  ! CLI reported an error: {_cli_error_detail(proc)}"); time.sleep(5); continue
        text = env.get("result", "")
        usage = env.get("usage", {})
        if usage: log(f"  tokens in={usage.get('input_tokens','?')} out={usage.get('output_tokens','?')}")
        data = _extract_json(text)
        if data and "title" in data and "chapter_md" in data:
            return data
        log(f"  ! invalid JSON in CLI result attempt {attempt}")
    return None


def _claude_ollama(model_name, stem, transcript, log):
    import urllib.request, urllib.error
    user = (f"Транскрипт лекции (имя файла: {stem}):\n\n"
            f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
            "Сделай главу книги. Верни JSON с title и chapter_md.")
    body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": CHAPTER_SYSTEM},
            {"role": "user", "content": user},
        ],
        "stream": False, "format": "json",
        "options": {"temperature": 0.2, "num_ctx": 16384, "num_predict": 4096},
    }
    url = UC.get("ollama_url", "http://localhost:11434")
    for attempt in (1, 2, 3):
        try:
            req = urllib.request.Request(
                f"{url}/api/chat", data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=1800) as r:
                resp = json.loads(r.read().decode("utf-8"))
            dt = time.time() - t0
            content = resp.get("message", {}).get("content", "")
            log(f"  ollama: in={resp.get('prompt_eval_count','?')} "
                f"out={resp.get('eval_count','?')} time={dt:.1f}s")
            data = _extract_json(content)
            if data and "title" in data and "chapter_md" in data:
                return data
        except Exception as e:
            log(f"  ! Ollama error attempt {attempt}: {e}"); time.sleep(3)
    return None


# ---------- Output paths ------------------------------------------

def sanitise_title(raw: str, fallback_stem: str) -> str:
    s = INVALID_FS.sub(" ", raw or "").strip()
    s = re.sub(r"\s+", " ", s).rstrip(". ").strip()
    if len(s) > 130: s = s[:130].rstrip()
    if not s:
        s = INVALID_FS.sub(" ", fallback_stem)[:60].rstrip(". ") or "untitled"
    return s


def unique_in(parent: Path, base: str) -> str:
    for suff in ["", " (2)", " (3)", " (4)", " (5)"]:
        candidate = f"{base}{suff}"
        if not any((parent / f"{candidate}{e}").exists()
                   for e in (".pdf", ".md", "-transcript.txt", ".mp4", ".mkv", ".mp3")):
            return candidate
    raise RuntimeError(f"no unique name for {base} in {parent}")


# ---------- Book index --------------------------------------------

def update_book_index(output: Path, book_title: str, book_author: str, log):
    # Подтянем source_path из .lectures_processed.json (если есть)
    state_files = list(output.parent.glob("**/.lectures_processed.json"))
    source_map = {}  # title -> path
    for sf in state_files:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            for stem, info in data.items():
                if isinstance(info, dict) and "title" in info and "source_path" in info:
                    source_map[info["title"]] = info["source_path"]
        except Exception:
            continue

    chapters = []
    for f in sorted(output.glob("*.md")):
        if f.name in ("BOOK_INDEX.md",): continue
        try:
            head = f.read_text(encoding="utf-8")[:2500]
        except OSError: continue
        m = re.search(r"^##\s+(?:Введение|Тема)\s*\n+([^\n#].+?)(\n\n|\n##|\Z)",
                      head, re.DOTALL)
        blurb = ""
        if m:
            blurb = re.sub(r"\s+", " ", m.group(1)).strip()[:220]
        # source_path из .md (плашка "Исходник лекции:")
        ms = re.search(r"_Исходник лекции:_\s*`([^`]+)`", head)
        src_path = ms.group(1) if ms else source_map.get(f.stem, "")
        chapters.append((f.stem, blurb, src_path))

    lines = [f"# {book_title}", "", f"_{book_author}_", "",
             f"**Обновлено:** {datetime.now():%Y-%m-%d %H:%M}", "",
             f"**Глав:** {len(chapters)}", "", "---", ""]
    for i, (title, blurb, src_path) in enumerate(chapters, 1):
        lines.append(f"## Глава {i}. {title}")
        lines.append("")
        lines.append(f"[📄 PDF]({title}.pdf) · [📝 MD]({title}.md) · [📰 транскрипт]({title}-transcript.txt)")
        lines.append("")
        if src_path:
            lines.append(f"🎬 _Исходник:_ `{src_path}`")
            lines.append("")
        if blurb:
            lines.append(f"> {blurb}")
            lines.append("")
    (output / "BOOK_INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"[book-index] {output / 'BOOK_INDEX.md'} (глав: {len(chapters)})")


# ---------- Main ---------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Видеолекции → главы книги (Whisper + Claude)")
    ap.add_argument("--source", default=UC.get("video_source"),
                    help=f"Папка с видео (default: {UC.get('video_source')})")
    ap.add_argument("--output", default=UC.get("book_output"),
                    help=f"Куда складывать главы (default: {UC.get('book_output')})")
    ap.add_argument("--book-title", default="Курс лекций",
                    help="Название книги (для BOOK_INDEX.md)")
    ap.add_argument("--book-author", default="Автор",
                    help="Автор книги")
    ap.add_argument("--openvino", action="store_true",
                    help="Использовать Intel iGPU через OpenVINO")
    ap.add_argument("--device", default="GPU",
                    help="OpenVINO device: GPU | CPU")
    ap.add_argument("--backend", choices=["auto", "sdk", "cli", "ollama"],
                    default=UC.get("backend", "auto"))
    ap.add_argument("--skip-whisper", action="store_true",
                    help="Не запускать Whisper (для re-run с уже готовыми transcripts)")
    ap.add_argument("--copy-video", action="store_true",
                    help="Скопировать оригинал видео в book_output (по умолч. — НЕ копируем, "
                         "видео может весить десятки GB; в главе и BOOK_INDEX будет ссылка на исходный путь)")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if not source.is_dir(): sys.exit(f"--source не папка: {source}")
    output.mkdir(parents=True, exist_ok=True)

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"lectures_{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log = Log(log_path)

    log(f"=== process_lectures START ===")
    log(f"source: {source}")
    log(f"output: {output}")
    log(f"openvino: {args.openvino}")
    log(f"backend: {args.backend}")
    log(f"log: {log_path}")

    backend = pick_backend(log, prefer=args.backend)
    sys.path.insert(0, str(ROOT))
    from pdf_render import render_md_to_pdf

    state = load_state(source)
    new_items = discover_new(source, state)
    if args.limit:
        new_items = new_items[: args.limit]
    log(f"[scan] новых файлов: {len(new_items)}")

    if not new_items:
        log("=== нечего обрабатывать ===")
        log.close(); return

    # Извлечение аудио из видео → inbox
    log("\n[extract] извлекаю audio из видео…")
    INBOX.mkdir(parents=True, exist_ok=True)
    stems_for_whisper = []
    for stem, src in new_items:
        # Если transcript уже в done — пропускаем extract
        if (DONE / f"{stem}-transcript.txt").exists():
            log(f"  = already in done: {stem}")
            continue
        if src.suffix.lower() in AUDIO_EXTS:
            # это уже audio — просто копируем в inbox
            target = INBOX / src.name
            if not target.exists():
                shutil.copy2(str(src), str(target))
            stems_for_whisper.append((stem, target))
            log(f"  · audio {stem}")
            continue
        # это video — извлекаем
        wav = INBOX / f"{stem}.wav"
        if wav.exists() and wav.stat().st_size > 0:
            log(f"  = wav exists: {stem}.wav")
            stems_for_whisper.append((stem, wav))
            continue
        log(f"  · извлекаю аудио из {src.name}…")
        ok = extract_audio(src, wav, log)
        if ok:
            stems_for_whisper.append((stem, wav))
        else:
            log(f"  ! не удалось извлечь audio из {src.name}")

    if stems_for_whisper and not args.skip_whisper:
        run_whisper(stems_for_whisper, args.openvino, args.device, log)

    # Process each item — Claude + PDF
    processed = []
    failures = {}
    for i, (stem, src) in enumerate(new_items, 1):
        log(f"\n[{i}/{len(new_items)}] {stem}")
        t_path = DONE / f"{stem}-transcript.txt"
        if not t_path.exists() or t_path.stat().st_size < 100:
            log("  ! transcript missing — skip")
            failures[stem] = "no transcript"
            continue
        try:
            transcript_text = t_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log(f"  ! read error: {e}"); failures[stem] = str(e); continue

        if len(transcript_text.strip()) < 200:
            log("  ! transcript too short — skip")
            failures[stem] = "transcript too short"
            continue

        log("  -> Claude: chapter…")
        data = call_claude(backend, CLAUDE_MODEL, stem, transcript_text, log)
        if not data:
            failures[stem] = "Claude failed"
            log("  ! Claude failed")
            continue

        title = data["title"]
        chapter_md = data["chapter_md"]
        safe = sanitise_title(title, stem)
        base = unique_in(output, safe)
        log(f"  -> {base}")

        # копия видео/аудио оригинала (по умолчанию НЕ копируем — экономим место)
        if args.copy_video:
            dst_src = output / f"{base}{src.suffix.lower()}"
            if not dst_src.exists():
                try: shutil.copy2(str(src), str(dst_src))
                except OSError as e: log(f"  ! не скопирован оригинал: {e}")

        # transcript
        t_dst = output / f"{base}-transcript.txt"
        if not t_dst.exists(): shutil.copy2(str(t_path), str(t_dst))

        # md (с приставкой-плашкой "Исходник: <путь>")
        md_dst = output / f"{base}.md"
        src_note = f"_Исходник лекции:_ `{src}`\n\n---\n\n"
        md_dst.write_text(src_note + chapter_md, encoding="utf-8")

        # PDF
        pdf_dst = output / f"{base}.pdf"
        date_part = detect_date(stem, src)
        date_str = date_part.strftime("%d.%m.%Y") if date_part else ""
        subtitle = f"{date_str}  ·  глава курса"
        footer = (f"Сгенерировано Claude {CLAUDE_MODEL} · "
                  f"{datetime.now():%Y-%m-%d %H:%M}")
        try:
            render_md_to_pdf(chapter_md, pdf_dst,
                             header={"title": title, "subtitle": subtitle, "footer": footer})
        except Exception as e:
            log(f"  ! PDF render failed: {e}")
            failures[stem] = f"PDF: {e}"

        processed.append({"stem": stem, "base": base, "title": title,
                          "source_path": str(src)})
        state[stem] = {"title": title, "processed_at": datetime.now().isoformat(),
                       "source_path": str(src)}
        save_state(source, state)  # инкрементально

    # Финальный BOOK_INDEX.md
    update_book_index(output, args.book_title, args.book_author, log)

    log(f"\n=== Done ===")
    log(f"  Обработано лекций:    {len(processed)}")
    log(f"  PDF создано:          {len(processed) - sum(1 for p in processed if p['stem'] in failures)}")
    log(f"  Ошибок:               {len(failures)}")
    log(f"  Папка книги:          {output}")
    if failures:
        log("  Failures:")
        for s, r in failures.items():
            log(f"    · {s}: {r}")
    log.close()


if __name__ == "__main__":
    main()
