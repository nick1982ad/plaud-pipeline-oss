#!/usr/bin/env python3
"""
process_shorts.py
=================

Для папки с короткими видео (YouTube Shorts / Reels) делает:
  1. Извлекает аудио ffmpeg'ом.
  2. Прогоняет через Whisper (OpenVINO / faster-whisper).
  3. Зовёт Claude с YouTube-промптом → возвращает JSON:
       { title, alt_titles, description, tags, pinned_comment, hashtags, hook }
  4. Кладёт РЯДОМ с каждым видео:
       <stem>-transcript.txt  — полный транскрипт
       <stem>-youtube.md      — YouTube метаданные в Markdown

CLI:
  python process_shorts.py --source "C:\\path\\to\\Shorts" --openvino
  python process_shorts.py --source ...  --topic "Как работает ChatGPT"
  python process_shorts.py --source ...  --series "Истории в баре"
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

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = Path(__file__).resolve().parent
FROMPLAUD = ROOT.parent
TRANSCRIBER = FROMPLAUD / "audio_transcriber"
INBOX = TRANSCRIBER / "inbox"
DONE = TRANSCRIBER / "done"
LOG_ROOT = ROOT / "_logs"

CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TRANSCRIPT_CHARS = 50_000
CLAUDE_MAX_TOKENS = 4096

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".3gp", ".ts", ".mts"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".opus", ".ogg", ".flac"}


# ---------- API key -----------------------------------------------

def load_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"): return
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["ANTHROPIC_API_KEY"] = value
                return
    sys.exit("ANTHROPIC_API_KEY не задан")


# ---------- Logging -----------------------------------------------

class Log:
    def __init__(self, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = log_path.open("a", encoding="utf-8")
    def __call__(self, msg=""):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        print(line, flush=True); self.fp.write(line + "\n"); self.fp.flush()
    def close(self): self.fp.close()


# ---------- ffmpeg -----------------------------------------------

def get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception: pass
    p = shutil.which("ffmpeg")
    if p: return p
    sys.exit("ffmpeg не найден")


def extract_audio(video, wav, log) -> bool:
    ffmpeg = get_ffmpeg()
    wav.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(video), "-vn", "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", "-loglevel", "warning", str(wav)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
        if proc.returncode != 0:
            log(f"  ! ffmpeg exit {proc.returncode}: {proc.stderr[-300:]}")
            return False
        return wav.exists() and wav.stat().st_size > 0
    except Exception as e:
        log(f"  ! ffmpeg error: {e}"); return False


# ---------- Discovery --------------------------------------------

def discover(source: Path):
    items = []
    for f in sorted(source.iterdir()):
        if not f.is_file(): continue
        if f.suffix.lower() not in (VIDEO_EXTS | AUDIO_EXTS): continue
        # пропускаем уже обработанные (есть -youtube.md рядом)
        if (source / f"{f.stem}-youtube.md").exists():
            continue
        items.append((f.stem, f))
    return items


# ---------- Whisper ------------------------------------------------

def run_whisper(openvino: bool, device: str, log):
    cmd = ([sys.executable, "-u", str(TRANSCRIBER / "transcribe_openvino.py"),
            "--mode", "once", "--device", device] if openvino
           else [sys.executable, "-u", str(TRANSCRIBER / "transcribe.py"), "--mode", "once"])
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(cmd, cwd=str(TRANSCRIBER),
                                stdout=subprocess.PIPE, stderr=None,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, env=env)
        for line in proc.stdout:
            if line.strip(): log(f"[whisper] {line.rstrip()}")
        proc.wait()
    except Exception as e:
        log(f"[whisper] error: {e}")


# ---------- Claude prompt ----------------------------------------

YT_SYSTEM = """Ты — продюсер YouTube Shorts на русском языке. Тебе дан транскрипт короткого видео (Shorts/Reels). Ты должен превратить его в готовый набор YouTube-метаданных для публикации.

Верни СТРОГО валидный JSON следующего вида и больше ничего:

{
  "hook": "<первые 1-2 секунды видео в текст: что зрителю сказали ПЕРВЫМ — фраза-крюк>",
  "title": "<основной заголовок Shorts, 40-90 знаков, цепляющий, эмодзи допустим>",
  "alt_titles": ["<альтернатива 1>", "<альтернатива 2>", "<альтернатива 3>", "<альтернатива 4>"],
  "description": "<описание 3-7 абзацев — см. требования ниже>",
  "tags": ["тег1", "тег2", "тег3", "..."],
  "hashtags": ["#Shorts", "#хештег1", "#хештег2", "..."],
  "pinned_comment": "<текст закреплённого комментария>"
}

ТРЕБОВАНИЯ:

title:
- 40-90 знаков. Если больше — режь.
- Цепляющий, провоцирует клик, без обмана.
- Без точки в конце.
- На русском (если транскрипт на русском).
- Эмодзи 1-2 шт можно, но не обязательны.
- Без слов «Видео», «Шортс».

alt_titles (4 штуки):
- Разные формулировки той же идеи: вопрос, число, шок, прямой совет.
- Подходят для A/B-тестов через VidIQ.

description:
- Абзац 1 — повтор хука + 1-2 предложения о ценности. КЛЮЧЕВЫЕ СЛОВА В ПЕРВЫХ 100 СИМВОЛАХ.
- Абзац 2-3 — ключевые тезисы из видео списком (3-5 пунктов с эмодзи 💡 или ▸ или —).
- Абзац 4 — призыв к действию (подписаться, посмотреть полную лекцию, прокомментировать).
- Абзац 5 — короткий блок «Об авторе» (только если есть данные из транскрипта).
- В конце — отдельной строкой все hashtags через пробел.
- Без markdown-форматирования внутри description (плоский текст).

tags (10-20 ключевых слов):
- Только русские/английские слова, без # и эмодзи.
- Без дублей.
- Должны быть релевантными содержанию.

hashtags (5-10 штук):
- Первый — всегда #Shorts.
- Остальные — релевантные теме и нише.
- На том же языке, что и видео.

pinned_comment:
- 1-3 предложения. Задаёт обсуждение, спрашивает мнение зрителя.
- Заканчивается вопросом или приглашением к диалогу.

hook:
- Дословная цитата первой фразы лектора (или близкий перифраз, если первая фраза косноязычная).
- 5-20 слов.

ФОРМАТ: только JSON, без markdown-обёрток ```json"""


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m: return None
    try: return json.loads(m.group(0))
    except json.JSONDecodeError: return None


def claude_call(client, model, stem, transcript, topic, series, log):
    context_hint = ""
    if topic: context_hint += f"Общая тема серии: {topic}.\n"
    if series: context_hint += f"Серия / плейлист: «{series}».\n"
    user = (f"{context_hint}"
            f"Транскрипт шортса (файл: {stem}):\n\n"
            f"---\n{transcript[:MAX_TRANSCRIPT_CHARS]}\n---\n\n"
            "Сделай YouTube Shorts метаданные. Верни JSON.")
    messages = [{"role": "user", "content": user}]
    for attempt in (1, 2, 3):
        try:
            resp = client.messages.create(
                model=model, max_tokens=CLAUDE_MAX_TOKENS,
                system=[{"type": "text", "text": YT_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=messages,
            )
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                log(f"  ! rate-limit attempt {attempt}, sleep 30"); time.sleep(30); continue
            log(f"  ! API error {e}"); time.sleep(3); continue
        usage = resp.usage
        log(f"  tokens in={usage.input_tokens} cache_read={getattr(usage,'cache_read_input_tokens',0)} out={usage.output_tokens}")
        text = next((b.text for b in resp.content if b.type=="text"), "")
        data = _extract_json(text)
        if (data and all(k in data for k in
                ("title","description","tags","hashtags","alt_titles","hook","pinned_comment"))):
            return data
        log(f"  ! invalid JSON attempt {attempt}; head: {text[:150]!r}")
        messages.append({"role":"assistant","content":text})
        messages.append({"role":"user","content":
            "Верни ТОЛЬКО валидный JSON со всеми полями: hook, title, alt_titles, description, tags, hashtags, pinned_comment."})
    return None


# ---------- Markdown render --------------------------------------

def yt_md(data, stem, src, topic, series):
    lines = [
        f"# YouTube Shorts — {data['title']}",
        "",
        f"_Файл:_ `{src.name}`  ·  _Сгенерировано:_ {datetime.now():%Y-%m-%d %H:%M}",
    ]
    if series: lines.append(f"_Серия:_ {series}")
    if topic: lines.append(f"_Тема:_ {topic}")
    lines += ["", "---", ""]

    lines += ["## 🎯 Hook (первая фраза)", "", f"> {data['hook']}", ""]

    lines += ["## 📢 Заголовок (основной)", "",
              f"**{data['title']}**", "",
              f"_Длина:_ {len(data['title'])} зн.", ""]

    lines += ["## 🔄 Альтернативные заголовки (A/B-тесты через VidIQ)", ""]
    for i, t in enumerate(data["alt_titles"], 1):
        lines.append(f"{i}. {t}  _({len(t)} зн.)_")
    lines.append("")

    lines += ["## 📝 Описание", "", "```", data["description"], "```", ""]

    lines += ["## 🏷 Теги (поле Tags при загрузке)", "",
              ", ".join(data["tags"]),
              "",
              f"_Всего тегов: {len(data['tags'])}_", ""]

    lines += ["## #️⃣ Хэштеги (в конце описания)", "",
              " ".join(data["hashtags"]), ""]

    lines += ["## 💬 Закреплённый комментарий", "",
              f"> {data['pinned_comment']}", ""]

    lines += ["---", "",
              "_Это первичная версия. Прогони через VidIQ для финальной оптимизации._",
              ""]
    return "\n".join(lines)


# ---------- Main -------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="Папка с видео-шортсами (.mov/.mp4)")
    ap.add_argument("--topic", default="",
                    help="Общая тема серии — даётся Claude как hint (опц.)")
    ap.add_argument("--series", default="",
                    help="Название серии/плейлиста — даётся Claude как hint (опц.)")
    ap.add_argument("--openvino", action="store_true")
    ap.add_argument("--device", default="GPU")
    ap.add_argument("--skip-whisper", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir(): sys.exit(f"not a dir: {source}")

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = LOG_ROOT / f"shorts_{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log = Log(log_path)

    log(f"=== process_shorts START ===")
    log(f"source: {source}")
    log(f"topic:  {args.topic}")
    log(f"series: {args.series}")
    log(f"log:    {log_path}")

    load_api_key()
    import anthropic
    client = anthropic.Anthropic()

    items = discover(source)
    if args.limit: items = items[: args.limit]
    log(f"[scan] новых видео: {len(items)}")
    if not items:
        log("=== nothing new ==="); log.close(); return

    # 1) extract audio → inbox (если ещё нет transcript в done/)
    log("\n[extract] извлекаю аудио…")
    INBOX.mkdir(parents=True, exist_ok=True)
    todo_for_whisper = False
    for stem, src in items:
        if (DONE / f"{stem}-transcript.txt").exists():
            log(f"  = already in done: {stem}")
            continue
        if src.suffix.lower() in AUDIO_EXTS:
            target = INBOX / src.name
            if not target.exists():
                shutil.copy2(str(src), str(target))
            todo_for_whisper = True
            log(f"  · audio {stem}")
            continue
        wav = INBOX / f"{stem}.wav"
        if not wav.exists() or wav.stat().st_size == 0:
            log(f"  · {src.name} → wav…")
            if extract_audio(src, wav, log):
                todo_for_whisper = True
        else:
            todo_for_whisper = True

    # 2) whisper
    if todo_for_whisper and not args.skip_whisper:
        log("\n[whisper] starting…")
        run_whisper(args.openvino, args.device, log)

    # 3) Claude + markdown
    ok = 0
    fail = 0
    for i, (stem, src) in enumerate(items, 1):
        log(f"\n[{i}/{len(items)}] {stem}")
        t_path = DONE / f"{stem}-transcript.txt"
        if not t_path.exists() or t_path.stat().st_size < 50:
            log("  ! transcript missing or empty")
            fail += 1; continue
        try:
            transcript = t_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log(f"  ! read err: {e}"); fail += 1; continue

        # копия transcript рядом с видео
        local_t = source / f"{stem}-transcript.txt"
        if not local_t.exists():
            shutil.copy2(str(t_path), str(local_t))

        if len(transcript.strip()) < 30:
            log("  ! transcript слишком короткий (< 30 chars), пропускаю Claude")
            fail += 1; continue

        log("  -> Claude (YouTube meta)…")
        data = claude_call(client, CLAUDE_MODEL, stem, transcript,
                           args.topic, args.series, log)
        if not data:
            log("  ! Claude failed"); fail += 1; continue

        md_path = source / f"{stem}-youtube.md"
        md_path.write_text(yt_md(data, stem, src, args.topic, args.series),
                           encoding="utf-8")
        log(f"  ✓ {md_path.name}  (title: {data['title']!r})")
        ok += 1

    log(f"\n=== Done ===")
    log(f"  Видео обработано:   {ok}")
    log(f"  Ошибок:             {fail}")
    log(f"  Папка:              {source}")
    log.close()


if __name__ == "__main__":
    main()
