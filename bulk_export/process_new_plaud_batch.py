#!/usr/bin/env python3
"""
process_new_plaud_batch.py
==========================

End-to-end pipeline for a single Plaud account (acc1 or acc2):

  1.  Snapshot existing stems in output_account*/_Unfiled/
  2.  Pull from Plaud via plaud_export.py
  3.  Diff -> new_stems
  4.  For stems without Plaud transcript: run local Whisper (transcribe.py --once)
  5.  For each new stem: call Claude API -> (title, summary_md)
  6.  Render summary -> PDF (via pdf_render.render_md_to_pdf)
  7.  Drop renamed artefacts (mp3 + pdf + md + transcript.txt) into
      C:\\_Dictophone\\Записи с DD.MM.YYYY по DD.MM.YYYY\\
  8.  Generate INDEX.md
  9.  Mirror raw files to C:\\_Dictophone\\plaud\\ and C:\\_Dictophone\\
 10.  Verify and write SUCCESS marker

CLI:
  python process_new_plaud_batch.py --account 2          # default acc2
  python process_new_plaud_batch.py --account 1
  python process_new_plaud_batch.py --skip-download      # only post-process existing
  python process_new_plaud_batch.py --skip-whisper       # don't run local Whisper
  python process_new_plaud_batch.py --resume new_batch_2026-05-20-130000

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

ROOT = Path(__file__).resolve().parent
DICT = Path(r"C:\_Dictophone")
DICT_PLAUD = DICT / "plaud"
FROMPLAUD = ROOT.parent           # bulk_export -> fromPlaud
TRANSCRIBER = FROMPLAUD / "audio_transcriber"
TRANSCRIBER_INBOX = TRANSCRIBER / "inbox"
TRANSCRIBER_DONE = TRANSCRIBER / "done"
LOG_ROOT = ROOT / "_logs"

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TRANSCRIPT_CHARS = 200_000
CLAUDE_MAX_TOKENS = 4096

CONFIGS = {
    1: ROOT / "config.json",
    2: ROOT / "config_2.json",
}

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


# ---------- API key loading ----------------------------------------

def load_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    if not ENV_FILE.exists():
        sys.exit(f"ANTHROPIC_API_KEY not set and {ENV_FILE} not found")
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("ANTHROPIC_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            os.environ["ANTHROPIC_API_KEY"] = value
            return
    sys.exit(f"ANTHROPIC_API_KEY not found in {ENV_FILE}")


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


# ---------- Snapshot / diff ----------------------------------------

def collect_stems(dir_path: Path) -> set:
    """Stems in dir (recursive): one per audio/transcript/summary file."""
    stems = set()
    if not dir_path.exists():
        return stems
    for f in dir_path.rglob("*"):
        if not f.is_file():
            continue
        name = f.name
        if name.endswith("-transcript.txt"):
            stems.add(name[:-len("-transcript.txt")])
        elif name.endswith("-Summary.txt"):
            stems.add(name[:-len("-Summary.txt")])
        elif name.endswith("-meta.json"):
            stems.add(name[:-len("-meta.json")])
        else:
            ext = f.suffix.lower()
            if ext in {".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac",
                       ".mp4", ".aac", ".wma", ".webm"}:
                stems.add(f.stem)
    return stems


# ---------- Plaud pull ---------------------------------------------

def run_plaud_export(config_path: Path, log) -> bool:
    log(f"[plaud] running plaud_export.py --config {config_path.name}")
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "plaud_export.py"),
             "--config", str(config_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=None,
        )
        if result.returncode != 0:
            log(f"[plaud] ! exit {result.returncode}")
            log(result.stdout[-1500:])
            log(result.stderr[-500:])
            return False
        # Print last few lines of stdout
        for line in result.stdout.splitlines()[-5:]:
            log(f"[plaud]   {line}")
        return True
    except subprocess.TimeoutExpired:
        log("[plaud] ! timeout")
        return False


# ---------- Local Whisper ------------------------------------------

def run_whisper_once(stems_to_transcribe: list, source_dir: Path, log) -> int:
    if not stems_to_transcribe:
        log("[whisper] no files queued (all have Plaud transcripts)")
        return 0
    TRANSCRIBER_INBOX.mkdir(parents=True, exist_ok=True)
    queued = 0
    for stem in stems_to_transcribe:
        mp3 = source_dir / f"{stem}.mp3"
        if not mp3.exists():
            log(f"[whisper]   ! mp3 not found for {stem!r}, skip")
            continue
        target = TRANSCRIBER_INBOX / mp3.name
        if (TRANSCRIBER_DONE / f"{stem}-transcript.txt").exists():
            log(f"[whisper]   = already in done, skip ({stem})")
            continue
        if not target.exists():
            shutil.copy2(mp3, target)
        queued += 1
    log(f"[whisper] queued {queued} mp3 → audio_transcriber/inbox")
    if queued == 0:
        return 0
    log("[whisper] running transcribe.py --mode once (this can take a while)")
    # Стримим stdout transcribe.py построчно → текстовый прогресс и русские
    # сообщения попадают в лог/монитор в реальном времени (а не пачкой в конце).
    # stderr НЕ перехватываем (inherit): дочерний процесс рисует туда живой
    # \r-прогресс-бар. Если пайплайн запущен в настоящем терминале — бар
    # анимируется прямо на экране; в фоне (нет TTY) он сам отключается.
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(TRANSCRIBER / "transcribe.py"), "--mode", "once"],
            cwd=str(TRANSCRIBER),
            stdout=subprocess.PIPE,
            stderr=None,  # наследуем терминал → живой бар виден на экране
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
        log(f"[whisper] ! ошибка запуска transcribe.py: {e}")
        return 0
    return queued


# ---------- Date detection -----------------------------------------

_MMDD_RE = re.compile(r"^(\d{2})-(\d{2})[\s_]")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def detect_date(stem: str, transcript_path: Path | None) -> datetime | None:
    m = _DATE_RE.match(stem)
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
            # If the parsed (month, day) is well in the future, assume it's last year.
            try:
                d = datetime(year, month, day)
            except ValueError:
                return None
            if (d - today).days > 60:
                d = datetime(year - 1, month, day)
            return d
        except ValueError:
            pass
    if transcript_path and transcript_path.exists():
        return datetime.fromtimestamp(transcript_path.stat().st_mtime)
    return None


def fmt_batch_folder(min_d: datetime | None, max_d: datetime | None) -> str:
    if not min_d and not max_d:
        return f"Записи (без даты) {datetime.now():%Y-%m-%d %H-%M}"
    if not min_d:
        min_d = max_d
    if not max_d:
        max_d = min_d
    if min_d.date() == max_d.date():
        return f"Записи за {min_d:%d.%m.%Y}"
    return f"Записи с {min_d:%d.%m.%Y} по {max_d:%d.%m.%Y}"


def ensure_unique_dir(parent: Path, name: str) -> Path:
    """Return the dated batch folder, reusing it if it already exists.

    Recordings for the same date range belong in one folder, so we never
    create "(2)"/"(3)" variants — we just merge into the existing folder.
    File-level collisions inside are handled separately by unique_in()."""
    target = parent / name
    target.mkdir(parents=True, exist_ok=True)
    return target


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


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def call_claude(client, model: str, stem: str, transcript: str, log) -> dict | None:
    """Returns dict {title, summary_md} or None on failure."""
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
        log(f"  ! invalid JSON (attempt {attempt}); raw head: {text[:120]!r}")
        # Add a corrective user message and retry
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": ("Твой предыдущий ответ не парсится как JSON либо не содержит "
                        "полей title и summary_md. Верни ТОЛЬКО валидный JSON с этими "
                        "двумя полями, без markdown-обёрток."),
        })
    return None


# ---------- Title sanitisation -------------------------------------

def sanitise_title(raw: str, fallback_stem: str) -> str:
    s = INVALID_FS.sub(" ", raw or "").strip()
    s = re.sub(r"\s+", " ", s).rstrip(". ").strip()
    if len(s) > 120:
        s = s[:120].rstrip()
    if not s:
        s = INVALID_FS.sub(" ", fallback_stem)[:60].rstrip(". ") or "untitled"
    return s


def unique_in(parent: Path, base: str, ext: str) -> Path:
    """Return parent / (base + ext) or "base (2)" + ext if collision (across all
    common artefact extensions to keep family consistent)."""
    candidates = ["", " (2)", " (3)", " (4)", " (5)", " (6)", " (7)", " (8)", " (9)"]
    for suff in candidates:
        cand = parent / f"{base}{suff}{ext}"
        family = [parent / f"{base}{suff}{e}"
                  for e in (".mp3", ".pdf", ".md", "-transcript.txt")]
        if not any(p.exists() for p in family):
            return cand
    raise RuntimeError(f"Cannot find unique name for {base} in {parent}")


# ---------- File mirror to _Dictophone -----------------------------

def mirror_to_dict(source_dir: Path, log):
    """Copy *-transcript.txt / *-Summary.txt to _Dictophone/plaud/
    and *.mp3 to _Dictophone/  (idempotent, size-based skip)."""
    DICT_PLAUD.mkdir(parents=True, exist_ok=True)
    DICT.mkdir(parents=True, exist_ok=True)
    copied_txt = copied_mp3 = 0
    for f in source_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.name.endswith("-transcript.txt") or f.name.endswith("-Summary.txt"):
            dst = DICT_PLAUD / f.name
            if dst.exists() and dst.stat().st_size == f.stat().st_size:
                continue
            shutil.copy2(f, dst)
            copied_txt += 1
        elif f.suffix.lower() == ".mp3":
            dst = DICT / f.name
            if dst.exists() and dst.stat().st_size == f.stat().st_size:
                continue
            shutil.copy2(f, dst)
            copied_mp3 += 1
    log(f"[mirror] copied {copied_txt} txt, {copied_mp3} mp3 to _Dictophone")


# ---------- Main orchestrator --------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Process new Plaud recordings into named batch folder with PDF summaries"
    )
    ap.add_argument("--account", type=int, choices=[1, 2], default=2,
                    help="Plaud account (default: 2)")
    ap.add_argument("--skip-download", action="store_true",
                    help="Skip plaud_export, work with existing output")
    ap.add_argument("--skip-whisper", action="store_true",
                    help="Skip local Whisper for files without Plaud transcript")
    ap.add_argument("--resume", default=None,
                    help="Resume an existing batch (folder name in _logs/, "
                         "e.g. new_batch_2026-05-20-130000)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only first N new stems (for testing)")
    args = ap.parse_args()

    config_path = CONFIGS[args.account]
    if not config_path.exists():
        sys.exit(f"config not found: {config_path}")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    output_dir = Path(cfg["output_dir"])

    # Resume or new batch
    if args.resume:
        batch_dir = LOG_ROOT / args.resume
        if not batch_dir.exists():
            sys.exit(f"resume target not found: {batch_dir}")
    else:
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        batch_dir = LOG_ROOT / f"new_batch_{ts}"
        batch_dir.mkdir(parents=True, exist_ok=False)
    (batch_dir / ".checkpoint").mkdir(exist_ok=True)
    log = Log(batch_dir / "log.txt")

    log(f"=== process_new_plaud_batch START (account={args.account}) ===")
    log(f"batch_dir:  {batch_dir}")
    log(f"output_dir: {output_dir}")
    log(f"resume:     {bool(args.resume)}")

    load_api_key()

    # Snapshot before (skip on resume - use saved snapshot)
    snap_before_path = batch_dir / "snapshot_before.json"
    if args.resume and snap_before_path.exists():
        before = set(json.loads(snap_before_path.read_text(encoding="utf-8")))
        log(f"[snapshot] loaded before-snapshot ({len(before)} stems)")
    else:
        before = collect_stems(output_dir)
        snap_before_path.write_text(
            json.dumps(sorted(before), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"[snapshot] before = {len(before)} stems")

    # Pull from Plaud
    if not args.skip_download and not args.resume:
        ok = run_plaud_export(config_path, log)
        if not ok:
            log("! plaud_export failed; continuing with current state")

    after = collect_stems(output_dir)
    (batch_dir / "snapshot_after.json").write_text(
        json.dumps(sorted(after), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    new_stems_set = after - before
    new_stems_path = batch_dir / "new_stems.json"
    if args.resume and new_stems_path.exists():
        new_stems = json.loads(new_stems_path.read_text(encoding="utf-8"))
        log(f"[diff] resuming with {len(new_stems)} new stems from saved file")
    else:
        new_stems = sorted(new_stems_set)
        new_stems_path.write_text(
            json.dumps(new_stems, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"[diff] new stems = {len(new_stems)}")
    if args.limit:
        new_stems = new_stems[: args.limit]
        log(f"[diff] limited to first {len(new_stems)}")

    if not new_stems:
        log("=== no new recordings; done ===")
        log.close()
        return

    # Determine which need Whisper
    unfiled = output_dir / "_Unfiled"
    needs_whisper = []
    for stem in new_stems:
        plaud_t = unfiled / f"{stem}-transcript.txt"
        if not plaud_t.exists():
            needs_whisper.append(stem)
    log(f"[classify] {len(new_stems) - len(needs_whisper)} have Plaud transcript, "
        f"{len(needs_whisper)} need Whisper")

    if needs_whisper and not args.skip_whisper:
        run_whisper_once(needs_whisper, unfiled, log)

    # Resolve transcripts
    transcript_source = {}  # stem -> ("plaud"|"whisper", Path)
    for stem in new_stems:
        plaud_t = unfiled / f"{stem}-transcript.txt"
        whisper_t = TRANSCRIBER_DONE / f"{stem}-transcript.txt"
        if plaud_t.exists():
            transcript_source[stem] = ("plaud", plaud_t)
        elif whisper_t.exists():
            transcript_source[stem] = ("whisper", whisper_t)
        else:
            transcript_source[stem] = ("missing", None)

    # Compute date range
    dates = {}
    for stem in new_stems:
        _, tpath = transcript_source[stem]
        mp3 = unfiled / f"{stem}.mp3"
        d = detect_date(stem, tpath or (mp3 if mp3.exists() else None))
        if d:
            dates[stem] = d
    if dates:
        min_d = min(dates.values())
        max_d = max(dates.values())
    else:
        min_d = max_d = None
    folder_name = fmt_batch_folder(min_d, max_d)
    log(f"[range] {folder_name}")

    # Create destination folder (idempotent on resume: reuse if matches saved)
    dest_record = batch_dir / "dest_folder.txt"
    if args.resume and dest_record.exists():
        dest = Path(dest_record.read_text(encoding="utf-8").strip())
        dest.mkdir(parents=True, exist_ok=True)
        log(f"[dest] reusing {dest}")
    else:
        dest = ensure_unique_dir(DICT, folder_name)
        dest_record.write_text(str(dest), encoding="utf-8")
        log(f"[dest] {dest}")

    # Claude client
    import anthropic
    client = anthropic.Anthropic()
    from pdf_render import render_md_to_pdf

    # Process each stem
    failures = {}
    processed = []
    for i, stem in enumerate(new_stems, 1):
        src_kind, tpath = transcript_source[stem]
        log(f"\n[{i}/{len(new_stems)}] {stem}  (transcript: {src_kind})")
        if tpath is None:
            failures[stem] = "no transcript (Plaud nor Whisper)"
            log(f"  ! no transcript available, skip")
            continue

        # Checkpoint
        ckpt_path = batch_dir / ".checkpoint" / f"{stem}.json"
        if ckpt_path.exists():
            ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
            log(f"  = checkpoint exists, reusing title/summary")
        else:
            transcript_text = tpath.read_text(encoding="utf-8", errors="replace")
            if len(transcript_text.strip()) < 50:
                log(f"  ! transcript too short ({len(transcript_text)} chars), "
                    f"using fallback summary")
                ckpt = {
                    "stem": stem,
                    "title": stem[:60].rstrip(". "),
                    "summary_md": (
                        "## Тема\n(транскрипт пуст или слишком короткий — Claude API "
                        "не вызывался)\n\n"
                        "## Ключевые тезисы\n- (нет данных)\n\n"
                        "## Договорённости и задачи\n- (нет данных)\n\n"
                        "## Открытые вопросы\n- (нет данных)\n\n"
                        "## Участники\n- (нет данных)\n"
                    ),
                    "source": src_kind,
                    "processed_at": datetime.now().isoformat(),
                }
            else:
                truncated_note = ""
                if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
                    truncated_note = (
                        f"\n\n_(транскрипт длиннее {MAX_TRANSCRIPT_CHARS} символов — "
                        f"саммари основано на первой части)_\n"
                    )
                data = call_claude(client, CLAUDE_MODEL, stem, transcript_text, log)
                if not data:
                    failures[stem] = "Claude API failed (no valid JSON)"
                    log(f"  ! Claude API failed")
                    continue
                ckpt = {
                    "stem": stem,
                    "title": data["title"],
                    "summary_md": data["summary_md"] + truncated_note,
                    "source": src_kind,
                    "processed_at": datetime.now().isoformat(),
                }
            ckpt_path.write_text(
                json.dumps(ckpt, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        safe = sanitise_title(ckpt["title"], stem)
        # Pick a unique base for the entire artefact family
        base_path = unique_in(dest, safe, ".pdf")
        base = base_path.stem  # e.g. "Title (2)"
        log(f"  -> {base}")

        # mp3
        mp3_src = unfiled / f"{stem}.mp3"
        mp3_dst = dest / f"{base}.mp3"
        has_mp3 = mp3_src.exists()
        if has_mp3 and not mp3_dst.exists():
            shutil.copy2(mp3_src, mp3_dst)

        # transcript copy
        t_dst = dest / f"{base}-transcript.txt"
        if not t_dst.exists():
            shutil.copy2(tpath, t_dst)

        # md
        md_dst = dest / f"{base}.md"
        if not md_dst.exists():
            md_dst.write_text(ckpt["summary_md"], encoding="utf-8")

        # pdf
        pdf_dst = dest / f"{base}.pdf"
        date_part = dates.get(stem)
        date_str = date_part.strftime("%d.%m.%Y") if date_part else "дата не определена"
        subtitle = f"{date_str} · transcript: {src_kind}"
        if has_mp3:
            try:
                size_mb = mp3_src.stat().st_size / (1024 * 1024)
                subtitle += f" · {size_mb:.1f} MB"
            except OSError:
                pass
        footer = (
            f"Сгенерировано Claude {CLAUDE_MODEL} · "
            f"{datetime.now():%Y-%m-%d %H:%M} · transcript: {src_kind}"
        )
        if not pdf_dst.exists():
            try:
                render_md_to_pdf(
                    ckpt["summary_md"],
                    pdf_dst,
                    header={
                        "title": ckpt["title"],
                        "subtitle": subtitle,
                        "footer": footer,
                    },
                )
            except Exception as e:
                log(f"  ! PDF render failed: {e}")
                failures[stem] = f"PDF render failed: {e}"
                continue

        processed.append({
            "stem": stem,
            "base": base,
            "title": ckpt["title"],
            "summary_md": ckpt["summary_md"],
            "source": src_kind,
            "date": date_part,
            "has_mp3": has_mp3,
        })

    # Failures file
    if failures:
        (batch_dir / "failures.json").write_text(
            json.dumps(failures, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"\n[failures] {len(failures)} stem(s) failed; see failures.json")

    # INDEX.md
    write_index(dest, folder_name, processed, failures, log)

    # Mirror to _Dictophone (raw flow compatibility)
    mirror_to_dict(unfiled, log)

    # Verify
    log("")
    verify_batch(dest, processed, failures, log)

    # SUCCESS marker
    (batch_dir / "SUCCESS").write_text(
        f"completed at {datetime.now().isoformat()}\n"
        f"dest: {dest}\n"
        f"processed: {len(processed)}\n"
        f"failures: {len(failures)}\n",
        encoding="utf-8",
    )

    plaud_count = sum(1 for p in processed if p["source"] == "plaud")
    whisper_count = sum(1 for p in processed if p["source"] == "whisper")
    log(f"\n=== Done ===")
    log(f"  New recordings:       {len(new_stems)}")
    log(f"  Plaud transcripts:    {plaud_count}")
    log(f"  Whisper transcripts:  {whisper_count}")
    log(f"  Summaries generated:  {len(processed)}")
    log(f"  PDFs generated:       {len(processed)}")
    log(f"  Failures:             {len(failures)}")
    log(f"  Batch folder:         {dest}")
    log.close()


# ---------- INDEX.md -----------------------------------------------

def _first_paragraph_after_topic(md: str) -> str:
    """Extract a short blurb from '## Тема' section."""
    m = re.search(r"##\s*Тема\s*\n+([^\n#].+?)(\n\n|\n##|\Z)", md, re.DOTALL)
    if not m:
        return ""
    blurb = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(blurb) > 220:
        blurb = blurb[:220].rstrip() + "…"
    return blurb


def _date_sort_key(date_str: str):
    """Turn a '## DD.MM' header token into a sortable (month, day) tuple."""
    m = re.match(r"(\d\d)\.(\d\d)", date_str)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    return (99, 99)


def _render_entry(p: dict) -> str:
    """Render one INDEX.md entry block (no trailing blank line) for a record."""
    date_str = p["date"].strftime("%d.%m") if p["date"] else "??"
    out = [f"## {date_str} — {p['title']}", ""]
    artefacts = []
    if p["has_mp3"]:
        artefacts.append(f"[🎧 mp3]({p['base']}.mp3)")
    else:
        artefacts.append("_(без аудио)_")
    artefacts.append(f"[📄 PDF]({p['base']}.pdf)")
    artefacts.append(f"[📝 MD]({p['base']}.md)")
    artefacts.append(f"[📰 транскрипт]({p['base']}-transcript.txt)")
    out.append(" · ".join(artefacts))
    out.append("")
    blurb = _first_paragraph_after_topic(p["summary_md"])
    if blurb:
        out.append(f"> {blurb}")
        out.append("")
    out.append(f"_Источник:_ {p['source']}  ·  _Исходное имя:_ `{p['stem']}`")
    return "\n".join(out)


def _parse_existing_entries(dest: Path) -> dict:
    """Read the current INDEX.md (if any) and return {base: (date_str, source,
    block_text)} for each entry whose .md still exists on disk. Lets us merge
    previously indexed records when a dated folder is reused across batches."""
    idx = dest / "INDEX.md"
    if not idx.exists():
        return {}
    text = idx.read_text(encoding="utf-8")
    out = {}
    # Each entry starts with "## DD.MM — title" and runs until the next "## "
    # heading or EOF. The failures section ("## ⚠ ...") has no date prefix and
    # is therefore skipped by the date-anchored pattern.
    for m in re.finditer(r"(?ms)^(## (\d\d\.\d\d|\?\?) .+?)(?=^## |\Z)", text):
        block = m.group(1).rstrip()
        date_str = m.group(2)
        bm = re.search(r"\]\(([^)]+)\.pdf\)", block)
        if not bm:
            continue
        base = bm.group(1)
        if not (dest / f"{base}.md").exists():
            continue  # record no longer on disk → drop from index
        sm = re.search(r"_Источник:_\s*(\w+)", block)
        source = sm.group(1) if sm else "?"
        out[base] = (date_str, source, block)
    return out


def write_index(dest: Path, folder_name: str, processed: list, failures: dict, log):
    # Start from records already indexed in this (possibly reused) folder...
    entries = _parse_existing_entries(dest)
    # ...then add/overwrite with what we processed in this batch.
    for p in processed:
        date_str = p["date"].strftime("%d.%m") if p["date"] else "??"
        entries[p["base"]] = (date_str, p["source"], _render_entry(p))

    ordered = sorted(entries.items(), key=lambda kv: (_date_sort_key(kv[1][0]), kv[0]))
    plaud_n = sum(1 for _, (_, src, _) in entries.items() if src == "plaud")
    whisper_n = sum(1 for _, (_, src, _) in entries.items() if src == "whisper")

    lines = [f"# {folder_name}", ""]
    lines.append(f"**Сгенерировано:** {datetime.now():%Y-%m-%d %H:%M}")
    lines.append(f"**Всего записей:** {len(entries)} обработано, {len(failures)} с ошибкой")
    lines.append(f"**Источник транскриптов:** {plaud_n} Plaud, {whisper_n} Whisper")
    lines.append("")
    lines.append("---")
    lines.append("")
    for _, (_, _, block) in ordered:
        lines.append(block)
        lines.append("")

    if failures:
        lines.append("---")
        lines.append("")
        lines.append("## ⚠ Не обработано")
        lines.append("")
        for stem, reason in failures.items():
            lines.append(f"- `{stem}` — {reason}")
        lines.append("")

    (dest / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"[index] INDEX.md written ({len(entries)} entries)")


# ---------- Verification -------------------------------------------

def verify_batch(dest: Path, processed: list, failures: dict, log):
    ok = True
    if not dest.exists():
        log(f"[verify] ✗ dest folder does not exist: {dest}")
        return
    for p in processed:
        for ext in (".pdf", ".md", "-transcript.txt"):
            f = dest / f"{p['base']}{ext}"
            if not f.exists() or f.stat().st_size == 0:
                log(f"[verify] ✗ missing/empty: {f.name}")
                ok = False
        if p["has_mp3"]:
            mp3 = dest / f"{p['base']}.mp3"
            if not mp3.exists() or mp3.stat().st_size == 0:
                log(f"[verify] ✗ missing/empty: {mp3.name}")
                ok = False
    idx = dest / "INDEX.md"
    if not idx.exists() or idx.stat().st_size == 0:
        log(f"[verify] ✗ missing/empty INDEX.md")
        ok = False
    if ok:
        log(f"[verify] ✓ all {len(processed)} artefact families OK in {dest}")
    else:
        log(f"[verify] ✗ verification failed; check files above")


if __name__ == "__main__":
    main()
