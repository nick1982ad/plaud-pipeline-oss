#!/usr/bin/env python3
"""
process_local_audio.py
======================

End-to-end pipeline for a LOCAL folder of audio files (wav/mp3/m4a/...).
No Plaud, no internet pull — just point it at a folder and it:

  1. Snapshots what's already been processed (.processed.json in source dir).
  2. Finds new audio files (anything not in snapshot, with supported extension).
  3. Runs Whisper (transcribe.py --mode once) for new files.
  4. Calls Claude API (claude-sonnet-4-6) → (title, summary_md) per file.
  5. Renders summary → PDF via pdf_render.render_md_to_pdf.
  6. Drops renamed artefacts (audio + pdf + md + transcript) into
        <output>/Записи с DD.MM.YYYY по DD.MM.YYYY/
  7. Generates INDEX.md.
  8. Writes SUCCESS marker.

CLI:
  python process_local_audio.py --source "C:\\AudioRecordings"
  python process_local_audio.py --source "D:\\dictaphone" --output "D:\\Results"
  python process_local_audio.py --source ... --skip-whisper      # only Claude+PDF for files that already have <stem>-transcript.txt next to them
  python process_local_audio.py --source ... --resume new_batch_2026-05-23-130000
  python process_local_audio.py --source ... --limit 5           # process at most N

Reads ANTHROPIC_API_KEY from environment, or from .env in script's parent dir,
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

# UTF-8 stdout on Windows
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent              # bulk_export
PORTABLE_ROOT = ROOT.parent                          # plaud-pipeline-portable
TRANSCRIBER = PORTABLE_ROOT / "audio_transcriber"
TRANSCRIBER_INBOX = TRANSCRIBER / "inbox"
TRANSCRIBER_DONE = TRANSCRIBER / "done"
LOG_ROOT = ROOT / "_logs"
USER_CONFIG_PATH = PORTABLE_ROOT / "user-config.json"


def load_user_config() -> dict:
    """Read user-config.json if present; return defaults otherwise."""
    base = {
        "audio_source":   r"C:\AudioRecordings",
        "output_dir":     r"C:\_Dictophone",
        "plaud_mirror":   r"C:\_Dictophone\plaud",
        "backend":        "auto",
        "whisper_model":  "small",
        "ollama_model":   "qwen2.5:7b-instruct",
        "ollama_url":     "http://localhost:11434",
        "anthropic_api_key": "",
    }
    if USER_CONFIG_PATH.exists():
        try:
            return {**base, **json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return base


USER_CONFIG = load_user_config()
DEFAULT_SOURCE = Path(USER_CONFIG["audio_source"])
DEFAULT_OUTPUT = Path(USER_CONFIG["output_dir"])

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TRANSCRIPT_CHARS = 200_000
CLAUDE_MAX_TOKENS = 4096

# Local LLM via Ollama (offline backend) — env > user-config > defaults
OLLAMA_URL = os.environ.get("OLLAMA_URL", USER_CONFIG.get("ollama_url", "http://localhost:11434"))
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", USER_CONFIG.get("ollama_model", "qwen2.5:7b-instruct"))
OLLAMA_TIMEOUT_SEC = 600

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac",
              ".mp4", ".aac", ".wma", ".webm"}

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_MMDD_RE = re.compile(r"^(\d{2})-(\d{2})[\s_]")
_YMD_RE  = re.compile(r"^(\d{4})[-_](\d{2})[-_](\d{2})")


# ---------- API key / backend selection ----------------------------

def load_api_key() -> bool:
    """Tries to find ANTHROPIC_API_KEY (env or .env file). Returns True if found."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    candidates = [
        PORTABLE_ROOT / ".env",
        ROOT / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value and not value.startswith("PASTE_") and not value.startswith("sk-ant-api03-PASTE"):
                    os.environ["ANTHROPIC_API_KEY"] = value
                    return True
    return False


def find_claude_cli():
    """Find `claude` CLI executable. Returns absolute path or None."""
    for name in ("claude.cmd", "claude.exe", "claude"):
        p = shutil.which(name)
        if p:
            return p
    return None


def find_ollama() -> str | None:
    """Probe Ollama at OLLAMA_URL. Returns model name if reachable AND model is loaded, else None."""
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        tags = [m.get("name", "") for m in data.get("models", [])]
        # exact match first, then prefix (e.g. "qwen2.5:7b-instruct" vs "qwen2.5:7b-instruct-q4_K_M")
        if OLLAMA_MODEL in tags:
            return OLLAMA_MODEL
        for t in tags:
            if t.startswith(OLLAMA_MODEL.split(":")[0]):
                return t
        return None
    except Exception:
        return None


def pick_backend(log, prefer: str | None = None):
    """Decide how to call Claude. Returns (kind, payload).
    kind ∈ {'sdk', 'cli', 'ollama'}
    payload: SDK client / CLI exe path / Ollama model name

    Order (default auto):
      1. ANTHROPIC_API_KEY → Anthropic SDK (cloud, billed)
      2. claude CLI in PATH → Claude Code subscription (cloud, no API key)
      3. Ollama at OLLAMA_URL with desired model → local LLM on CPU/GPU
    Use --backend to force one specific path.
    """
    have_key = load_api_key()
    if prefer in (None, "auto", "sdk") and have_key:
        try:
            import anthropic
            client = anthropic.Anthropic()
            log("[backend] using Anthropic SDK (ANTHROPIC_API_KEY found)")
            return "sdk", client
        except ImportError:
            log("[backend] anthropic package not installed; trying next backend")
    if prefer in (None, "auto", "cli"):
        cli = find_claude_cli()
        if cli:
            log(f"[backend] using Claude Code CLI subscription: {cli}")
            return "cli", cli
        if prefer == "cli":
            sys.exit("claude CLI not found in PATH. Install via: npm install -g @anthropic-ai/claude-code")
    if prefer in (None, "auto", "ollama"):
        model = find_ollama()
        if model:
            log(f"[backend] using LOCAL Ollama: {OLLAMA_URL} model={model}  (CPU/GPU offline, no cloud)")
            return "ollama", model
        if prefer == "ollama":
            sys.exit(
                f"Ollama not reachable at {OLLAMA_URL} (or model '{OLLAMA_MODEL}' not pulled).\n"
                f"Install: https://ollama.com/download/windows\n"
                f"Then: ollama pull {OLLAMA_MODEL}; ollama serve"
            )
    sys.exit(
        "No backend available. Options:\n"
        "  1. set ANTHROPIC_API_KEY (or put it in .env), OR\n"
        "  2. install Claude Code CLI: npm install -g @anthropic-ai/claude-code, OR\n"
        f"  3. install Ollama + pull a model: https://ollama.com  →  ollama pull {OLLAMA_MODEL}"
    )


# ---------- Logging ------------------------------------------------

class Log:
    def __init__(self, log_path: Path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = log_path.open("a", encoding="utf-8")
        self.path = log_path

    def __call__(self, msg=""):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        print(line, flush=True)
        self.fp.write(line + "\n")
        self.fp.flush()

    def close(self):
        self.fp.close()


# ---------- Snapshot of processed audio ----------------------------

def load_state(source: Path) -> dict:
    p = source / ".processed.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(source: Path, state: dict):
    p = source / ".processed.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_audio(source: Path, state: dict) -> list:
    """Return [(stem, audio_path)] for files in source not in state."""
    new_items = []
    for f in sorted(source.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in AUDIO_EXTS:
            continue
        if f.stem in state:
            continue
        new_items.append((f.stem, f))
    return new_items


# ---------- Local Whisper ------------------------------------------

def run_whisper_once(items: list, log) -> int:
    """items = [(stem, audio_path)]. Copy mp3/wav to inbox/, run transcribe.py --once."""
    if not items:
        log("[whisper] no files queued")
        return 0
    TRANSCRIBER_INBOX.mkdir(parents=True, exist_ok=True)
    queued = 0
    for stem, audio in items:
        target = TRANSCRIBER_INBOX / audio.name
        if (TRANSCRIBER_DONE / f"{stem}-transcript.txt").exists():
            log(f"[whisper]   = already in done, skip ({stem})")
            continue
        if not target.exists():
            shutil.copy2(audio, target)
        queued += 1
    log(f"[whisper] queued {queued} files → audio_transcriber/inbox")
    if queued == 0:
        return 0
    log("[whisper] running transcribe.py --mode once (this can take a while)")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(TRANSCRIBER / "transcribe.py"), "--mode", "once"],
            cwd=str(TRANSCRIBER),
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log(f"[whisper]   {line}")
        rc = proc.wait()
        if rc != 0:
            log(f"[whisper] ! exit {rc}")
            return 0
    except Exception as e:
        log(f"[whisper] ! error: {e}")
        return 0
    return queued


# ---------- Date detection -----------------------------------------

def detect_date(stem: str, audio_path: Path) -> datetime | None:
    m = _YMD_RE.match(stem)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _MMDD_RE.match(stem)
    if m:
        try:
            month, day = int(m.group(1)), int(m.group(2))
            today = datetime.now()
            year = today.year
            try:
                d = datetime(year, month, day)
            except ValueError:
                return None
            if (d - today).days > 60:
                d = datetime(year - 1, month, day)
            return d
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(audio_path.stat().st_mtime)
    except OSError:
        return None


def fmt_batch_folder(min_d, max_d) -> str:
    if not min_d and not max_d:
        return f"Записи (без даты) {datetime.now():%Y-%m-%d %H-%M}"
    if not min_d: min_d = max_d
    if not max_d: max_d = min_d
    if min_d.date() == max_d.date():
        return f"Записи за {min_d:%d.%m.%Y}"
    return f"Записи с {min_d:%d.%m.%Y} по {max_d:%d.%m.%Y}"


def ensure_unique_dir(parent: Path, name: str) -> Path:
    target = parent / name
    if not target.exists():
        target.mkdir(parents=True, exist_ok=False)
        return target
    for i in range(2, 100):
        cand = parent / f"{name} ({i})"
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=False)
            return cand
    raise RuntimeError(f"Cannot find unique name under {parent}")


# ---------- Claude API ---------------------------------------------

SYSTEM_PROMPT = """Ты — ассистент-редактор личного аудиоархива. Твоя задача — по транскрипту встречи или лекции вернуть СТРОГО валидный JSON следующего вида и больше ничего:

{
  "title": "<короткое говорящее имя файла, 5-12 слов на русском>",
  "summary_md": "<markdown-саммари по шаблону>"
}

ТРЕБОВАНИЯ К title:
- 5-12 слов на русском, передаёт суть разговора.
- Без даты в начале, без эмодзи.
- Допустимы кириллица, латиница, цифры, пробел, дефис, скобки, тире, точки внутри.
- ЗАПРЕЩЕНО: символы Windows-FS: < > : " / \\ | ? * и управляющие символы.
- Не начинать со слов "Запись", "Аудио", "Транскрипт".
- Стиль — фраза-резюме, как заголовок документа (без точки в конце).

ТРЕБОВАНИЯ К summary_md (СТРОГО следуй шаблону, не добавляй разделы):

## Тема
Одной-двумя строками — о чём встреча.

## Ключевые тезисы
- 5-10 пунктов — главные факты, идеи, решения.

## Договорённости и задачи
- 0-10 пунктов: кто, что, кому, когда. Если ничего не зафиксировано — пиши "- (не зафиксировано)".

## Открытые вопросы
- 0-5 пунктов — что осталось нерешённым.

## Участники
- Имена/роли. Если неясно — "- (не идентифицированы)".

ПРАВИЛА: лаконично, без воды, без выдумок. Если транскрипт обрывочный — честно отметь в "## Тема" и заполни остальные секции пометкой "(нет данных)".

ФОРМАТ ОТВЕТА: только JSON, без markdown code-fence. Без полей кроме title и summary_md."""


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def call_claude(backend, model: str, stem: str, transcript: str, log):
    """Universal LLM caller. `backend` = ('sdk'|'cli'|'ollama', payload)."""
    kind, payload = backend
    if kind == "sdk":
        return _call_claude_sdk(payload, model, stem, transcript, log)
    if kind == "cli":
        return _call_claude_cli(payload, model, stem, transcript, log)
    if kind == "ollama":
        return _call_ollama(payload, stem, transcript, log)
    raise ValueError(f"unknown backend: {kind}")


def _call_claude_sdk(client, model, stem, transcript, log):
    user_prompt = (
        f"Транскрипт записи (имя исходного файла: {stem}):\n\n"
        f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
        "Верни JSON с title и summary_md по правилам системного промпта."
    )
    messages = [{"role": "user", "content": user_prompt}]
    for attempt in (1, 2, 3):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=messages,
            )
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower():
                log(f"  ! rate-limit, sleep 30s (attempt {attempt})")
                time.sleep(30)
                continue
            log(f"  ! API error attempt {attempt}: {e}")
            time.sleep(5)
            continue
        usage = resp.usage
        log(
            f"  tokens in={usage.input_tokens}, "
            f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)}, "
            f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)}, "
            f"out={usage.output_tokens}"
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = _extract_json(text)
        if data and "title" in data and "summary_md" in data:
            return data
        log(f"  ! invalid JSON (attempt {attempt}); head: {text[:120]!r}")
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": ("Твой предыдущий ответ не парсится как JSON. "
                        "Верни ТОЛЬКО валидный JSON с полями title и summary_md."),
        })
    return None


def _call_ollama(ollama_model: str, stem: str, transcript: str, log):
    """Call local Ollama for offline summary. No network, no billing."""
    import urllib.request, urllib.error
    user_prompt = (
        f"Транскрипт записи (имя исходного файла: {stem}):\n\n"
        f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
        "Верни JSON с title и summary_md по правилам системного промпта."
    )
    body = {
        "model": ollama_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",            # nudge to JSON-only output
        "options": {
            "temperature": 0.2,
            "num_ctx": 8192,         # context for transcripts ~5-10k chars
            "num_predict": 2048,
        },
    }
    payload = json.dumps(body).encode("utf-8")
    for attempt in (1, 2, 3):
        try:
            req = urllib.request.Request(
                f"{OLLAMA_URL}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SEC) as r:
                resp = json.loads(r.read().decode("utf-8"))
            dt = time.time() - t0
            content = resp.get("message", {}).get("content", "")
            in_tokens = resp.get("prompt_eval_count", "?")
            out_tokens = resp.get("eval_count", "?")
            log(f"  ollama: in={in_tokens} out={out_tokens} time={dt:.1f}s")
            data = _extract_json(content)
            if data and "title" in data and "summary_md" in data:
                return data
            log(f"  ! invalid JSON from ollama (attempt {attempt}); head: {content[:120]!r}")
        except urllib.error.HTTPError as e:
            log(f"  ! Ollama HTTP {e.code} attempt {attempt}: {e.read()[:200]!r}")
            time.sleep(3)
        except Exception as e:
            log(f"  ! Ollama error attempt {attempt}: {e}")
            time.sleep(3)
    return None


def _call_claude_cli(claude_path: str, model: str, stem: str, transcript: str, log):
    """Call Claude through `claude --print` (uses Claude Code subscription)."""
    user_prompt = (
        f"Транскрипт записи (имя исходного файла: {stem}):\n\n"
        f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
        "Верни JSON с title и summary_md по правилам системного промпта."
    )
    for attempt in (1, 2, 3):
        try:
            proc = subprocess.run(
                [claude_path,
                 "--print",
                 "--model", model,
                 "--system-prompt", SYSTEM_PROMPT,
                 "--output-format", "json",
                 "--no-session-persistence"],
                input=user_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            log(f"  ! claude CLI timeout (attempt {attempt})")
            continue
        except Exception as e:
            log(f"  ! CLI launch error attempt {attempt}: {e}")
            time.sleep(5)
            continue
        if proc.returncode != 0:
            log(f"  ! claude CLI exit {proc.returncode}: {proc.stderr[:300]}")
            time.sleep(5)
            continue
        raw = proc.stdout.strip()
        try:
            cli_envelope = json.loads(raw)
        except json.JSONDecodeError:
            log(f"  ! CLI returned non-JSON envelope (attempt {attempt}); head: {raw[:200]!r}")
            continue
        text = cli_envelope.get("result", "")
        usage = cli_envelope.get("usage", {})
        if usage:
            log(f"  tokens in={usage.get('input_tokens', '?')}, out={usage.get('output_tokens', '?')}")
        else:
            log(f"  (CLI ok, no usage info)")
        data = _extract_json(text)
        if data and "title" in data and "summary_md" in data:
            return data
        log(f"  ! invalid JSON in CLI result (attempt {attempt}); head: {text[:120]!r}")
    return None


def sanitise_title(raw: str, fallback_stem: str) -> str:
    s = INVALID_FS.sub(" ", raw or "").strip()
    s = re.sub(r"\s+", " ", s).rstrip(". ").strip()
    if len(s) > 120:
        s = s[:120].rstrip()
    if not s:
        s = INVALID_FS.sub(" ", fallback_stem)[:60].rstrip(". ") or "untitled"
    return s


def unique_in(parent: Path, base: str, ext: str) -> Path:
    candidates = ["", " (2)", " (3)", " (4)", " (5)", " (6)", " (7)", " (8)", " (9)"]
    for suff in candidates:
        cand = parent / f"{base}{suff}{ext}"
        family = [parent / f"{base}{suff}{e}"
                  for e in (".mp3", ".wav", ".m4a", ".pdf", ".md", "-transcript.txt")]
        if not any(p.exists() for p in family):
            return cand
    raise RuntimeError(f"Cannot find unique name for {base} in {parent}")


# ---------- Main ---------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Process a local folder of audio files (wav/mp3/...) into a renamed batch folder with PDF summaries"
    )
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help=f"Folder with audio files (default from user-config.json: {DEFAULT_SOURCE})")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT),
                    help=f"Where to create the batch folder (default: {DEFAULT_OUTPUT})")
    ap.add_argument("--skip-whisper", action="store_true",
                    help="Skip Whisper — only files with existing <stem>-transcript.txt next to audio")
    ap.add_argument("--backend", choices=["auto", "sdk", "cli", "ollama"],
                    default=USER_CONFIG.get("backend", "auto"),
                    help=f"LLM backend (default from user-config.json: {USER_CONFIG.get('backend', 'auto')})")
    ap.add_argument("--resume", default=None, help="Resume an existing batch (folder in _logs/)")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N new files")
    args = ap.parse_args()

    # If user-config has API key, populate env for SDK
    if USER_CONFIG.get("anthropic_api_key") and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = USER_CONFIG["anthropic_api_key"]

    source = Path(args.source).resolve()
    output = Path(args.output)
    if not source.is_dir():
        sys.exit(f"--source not a directory: {source}")
    output.mkdir(parents=True, exist_ok=True)

    if args.resume:
        batch_dir = LOG_ROOT / args.resume
        if not batch_dir.exists():
            sys.exit(f"resume target not found: {batch_dir}")
    else:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        batch_dir = LOG_ROOT / f"local_batch_{ts}"
        batch_dir.mkdir(parents=True, exist_ok=False)
    (batch_dir / ".checkpoint").mkdir(exist_ok=True)
    log = Log(batch_dir / "log.txt")

    log(f"=== process_local_audio START ===")
    log(f"source:    {source}")
    log(f"output:    {output}")
    log(f"batch_dir: {batch_dir}")
    log(f"resume:    {bool(args.resume)}")

    backend = pick_backend(log, prefer=args.backend)

    # Find new audio files
    state = load_state(source)
    log(f"[state] {len(state)} files previously processed in source")
    new_items = discover_audio(source, state)
    log(f"[discover] {len(new_items)} new audio file(s)")
    if args.limit:
        new_items = new_items[: args.limit]
        log(f"[discover] limited to first {len(new_items)}")
    if not new_items:
        log("=== nothing to do ===")
        log.close()
        return

    # Determine which need Whisper (no <stem>-transcript.txt next to audio AND no Whisper-done)
    needs_whisper = []
    for stem, audio in new_items:
        sidecar_t = audio.with_name(f"{stem}-transcript.txt")
        whisper_t = TRANSCRIBER_DONE / f"{stem}-transcript.txt"
        if sidecar_t.exists() or whisper_t.exists():
            continue
        needs_whisper.append((stem, audio))

    log(f"[classify] {len(new_items) - len(needs_whisper)} have transcript, "
        f"{len(needs_whisper)} need Whisper")

    if needs_whisper and not args.skip_whisper:
        run_whisper_once(needs_whisper, log)

    # Resolve transcript source per stem
    transcript_path = {}
    for stem, audio in new_items:
        sidecar = audio.with_name(f"{stem}-transcript.txt")
        whisper_t = TRANSCRIBER_DONE / f"{stem}-transcript.txt"
        if sidecar.exists():
            transcript_path[stem] = ("sidecar", sidecar)
        elif whisper_t.exists():
            transcript_path[stem] = ("whisper", whisper_t)
        else:
            transcript_path[stem] = ("missing", None)

    # Date range → batch folder
    dates = {}
    for stem, audio in new_items:
        d = detect_date(stem, audio)
        if d:
            dates[stem] = d
    min_d = min(dates.values()) if dates else None
    max_d = max(dates.values()) if dates else None
    folder_name = fmt_batch_folder(min_d, max_d)
    log(f"[range] {folder_name}")

    dest_record = batch_dir / "dest_folder.txt"
    if args.resume and dest_record.exists():
        dest = Path(dest_record.read_text(encoding="utf-8").strip())
        dest.mkdir(parents=True, exist_ok=True)
        log(f"[dest] reusing {dest}")
    else:
        dest = ensure_unique_dir(output, folder_name)
        dest_record.write_text(str(dest), encoding="utf-8")
        log(f"[dest] {dest}")

    sys.path.insert(0, str(ROOT))
    from pdf_render import render_md_to_pdf

    failures = {}
    processed = []
    for i, (stem, audio) in enumerate(new_items, 1):
        src_kind, tpath = transcript_path[stem]
        log(f"\n[{i}/{len(new_items)}] {stem}  (transcript: {src_kind})")
        if tpath is None:
            failures[stem] = "no transcript"
            log("  ! no transcript, skip")
            continue

        ckpt_path = batch_dir / ".checkpoint" / f"{stem}.json"
        if ckpt_path.exists():
            ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
            log("  = checkpoint exists, reusing")
        else:
            transcript_text = tpath.read_text(encoding="utf-8", errors="replace")
            if len(transcript_text.strip()) < 50:
                log(f"  ! transcript too short ({len(transcript_text)} chars)")
                ckpt = {
                    "stem": stem,
                    "title": stem[:60].rstrip(". "),
                    "summary_md": (
                        "## Тема\n(транскрипт пуст или слишком короткий — Claude API не вызывался)\n\n"
                        "## Ключевые тезисы\n- (нет данных)\n\n"
                        "## Договорённости и задачи\n- (нет данных)\n\n"
                        "## Открытые вопросы\n- (нет данных)\n\n"
                        "## Участники\n- (нет данных)\n"
                    ),
                    "source": src_kind,
                    "processed_at": datetime.now().isoformat(),
                }
            else:
                trunc = ""
                if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
                    trunc = (f"\n\n_(транскрипт длиннее {MAX_TRANSCRIPT_CHARS} символов "
                             f"— саммари основано на первой части)_\n")
                data = call_claude(backend, CLAUDE_MODEL, stem, transcript_text, log)
                if not data:
                    failures[stem] = "Claude API failed"
                    log("  ! Claude API failed")
                    continue
                ckpt = {
                    "stem": stem,
                    "title": data["title"],
                    "summary_md": data["summary_md"] + trunc,
                    "source": src_kind,
                    "processed_at": datetime.now().isoformat(),
                }
            ckpt_path.write_text(json.dumps(ckpt, ensure_ascii=False, indent=2), encoding="utf-8")

        safe = sanitise_title(ckpt["title"], stem)
        base_path = unique_in(dest, safe, ".pdf")
        base = base_path.stem
        log(f"  -> {base}")

        # copy audio
        audio_dst = dest / f"{base}{audio.suffix.lower()}"
        if not audio_dst.exists():
            shutil.copy2(audio, audio_dst)

        # transcript
        t_dst = dest / f"{base}-transcript.txt"
        if not t_dst.exists():
            shutil.copy2(tpath, t_dst)

        # md
        md_dst = dest / f"{base}.md"
        if not md_dst.exists():
            md_dst.write_text(ckpt["summary_md"], encoding="utf-8")

        # pdf
        date_part = dates.get(stem)
        date_str = date_part.strftime("%d.%m.%Y") if date_part else "дата не определена"
        try:
            size_mb = audio.stat().st_size / (1024 * 1024)
            subtitle = f"{date_str} · transcript: {src_kind} · {size_mb:.1f} MB"
        except OSError:
            subtitle = f"{date_str} · transcript: {src_kind}"
        footer = (f"Сгенерировано Claude {CLAUDE_MODEL} · "
                  f"{datetime.now():%Y-%m-%d %H:%M} · transcript: {src_kind}")
        pdf_dst = dest / f"{base}.pdf"
        if not pdf_dst.exists():
            try:
                render_md_to_pdf(
                    ckpt["summary_md"], pdf_dst,
                    header={"title": ckpt["title"], "subtitle": subtitle, "footer": footer},
                )
            except Exception as e:
                log(f"  ! PDF render failed: {e}")
                failures[stem] = f"PDF render failed: {e}"
                continue

        processed.append({
            "stem": stem, "base": base, "title": ckpt["title"],
            "summary_md": ckpt["summary_md"], "source": src_kind,
            "date": date_part, "audio_ext": audio.suffix.lower(),
        })

    # Failures
    if failures:
        (batch_dir / "failures.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"\n[failures] {len(failures)} stem(s) failed")

    # INDEX.md
    write_index(dest, folder_name, processed, failures, log)

    # Update state — mark processed stems
    for p in processed:
        state[p["stem"]] = {"title": p["title"],
                            "processed_at": datetime.now().isoformat()}
    save_state(source, state)
    log(f"[state] saved {len(state)} stems to {source / '.processed.json'}")

    # Verify
    log("")
    verify_batch(dest, processed, failures, log)

    (batch_dir / "SUCCESS").write_text(
        f"completed at {datetime.now().isoformat()}\n"
        f"dest: {dest}\nprocessed: {len(processed)}\nfailures: {len(failures)}\n",
        encoding="utf-8")

    sidecar_n = sum(1 for p in processed if p["source"] == "sidecar")
    whisper_n = sum(1 for p in processed if p["source"] == "whisper")
    log(f"\n=== Done ===")
    log(f"  New audio files:      {len(new_items)}")
    log(f"  Sidecar transcripts:  {sidecar_n}")
    log(f"  Whisper transcripts:  {whisper_n}")
    log(f"  PDFs generated:       {len(processed)}")
    log(f"  Failures:             {len(failures)}")
    log(f"  Batch folder:         {dest}")
    log.close()


def _first_paragraph_after_topic(md: str) -> str:
    m = re.search(r"##\s*Тема\s*\n+([^\n#].+?)(\n\n|\n##|\Z)", md, re.DOTALL)
    if not m: return ""
    blurb = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(blurb) > 220:
        blurb = blurb[:220].rstrip() + "…"
    return blurb


def write_index(dest, folder_name, processed, failures, log):
    lines = [f"# {folder_name}", "",
             f"**Сгенерировано:** {datetime.now():%Y-%m-%d %H:%M}",
             f"**Всего записей:** {len(processed)} обработано, {len(failures)} с ошибкой",
             ""]
    sidecar_n = sum(1 for p in processed if p["source"] == "sidecar")
    whisper_n = sum(1 for p in processed if p["source"] == "whisper")
    lines.append(f"**Источник транскриптов:** {sidecar_n} sidecar, {whisper_n} Whisper")
    lines.append(""); lines.append("---"); lines.append("")
    items = sorted(processed, key=lambda p: (p["date"] or datetime.max, p["base"]))
    for p in items:
        date_str = p["date"].strftime("%d.%m") if p["date"] else "??"
        lines.append(f"## {date_str} — {p['title']}")
        lines.append("")
        lines.append(f"[🎧 audio]({p['base']}{p['audio_ext']}) · "
                     f"[📄 PDF]({p['base']}.pdf) · "
                     f"[📝 MD]({p['base']}.md) · "
                     f"[📰 транскрипт]({p['base']}-transcript.txt)")
        lines.append("")
        blurb = _first_paragraph_after_topic(p["summary_md"])
        if blurb:
            lines.append(f"> {blurb}"); lines.append("")
        lines.append(f"_Источник:_ {p['source']}  ·  _Исходное имя:_ `{p['stem']}`")
        lines.append("")
    if failures:
        lines.append("---"); lines.append(""); lines.append("## ⚠ Не обработано"); lines.append("")
        for stem, reason in failures.items():
            lines.append(f"- `{stem}` — {reason}")
    (dest / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"[index] INDEX.md written ({len(items)} entries)")


def verify_batch(dest, processed, failures, log):
    ok = True
    if not dest.exists():
        log(f"[verify] ✗ dest does not exist"); return
    for p in processed:
        for ext in (".pdf", ".md", "-transcript.txt", p["audio_ext"]):
            f = dest / f"{p['base']}{ext}"
            if not f.exists() or f.stat().st_size == 0:
                log(f"[verify] ✗ missing/empty: {f.name}")
                ok = False
    if not (dest / "INDEX.md").exists():
        log("[verify] ✗ INDEX.md missing")
        ok = False
    if ok:
        log(f"[verify] ✓ all {len(processed)} artefact families OK in {dest}")
    else:
        log("[verify] ✗ verification failed")


if __name__ == "__main__":
    main()
