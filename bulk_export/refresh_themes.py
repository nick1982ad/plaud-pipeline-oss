#!/usr/bin/env python3
"""
refresh_themes.py
=================

Пересчитывает «преобладающую тему» в имени папки  «Записи за ДД.ММ.ГГГГ - <тема>»,
используя свежие transcripts (которых раньше не было).

Логика:
  1. Сканирует все папки C:\\_Dictophone\\Записи за DD.MM.YYYY - <тема>
  2. Для каждой папки собирает список stems внутри.
  3. Для каждого stem'а формирует заголовок:
       - если stem говорящий (не сырое имя 2026-05-19 19_28_07) — берёт его
       - иначе если есть -transcript.txt — выжимка через Claude (1 запрос на stem)
       - иначе — пропускает
  4. Одним вызовом Claude получает «общую тему» для дня (5-12 слов, через запятую если разные).
  5. Если новая тема отличается от текущей — переименовывает папку.

CLI:
  python refresh_themes.py                 # выполнить с переименованием
  python refresh_themes.py --dry-run       # показать план
  python refresh_themes.py --only-template # только папки с "Рабочий день", "Несколько записей" и т.п.
  python refresh_themes.py --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = Path(__file__).resolve().parent
DICT = Path(r"C:\_Dictophone")
CLAUDE_MODEL = "claude-sonnet-4-6"
LOG_DIR = ROOT / "_logs"

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".opus", ".ogg", ".flac"}

# Регулярка для папок Записи за DD.MM.YYYY - <тема>
FOLDER_RE = re.compile(r"^Записи за (\d{2}\.\d{2}\.\d{4})\s*[-—–]?\s*(.*)$")

# Признаки «шаблонной» темы (заглушка после organize_by_date без свежих transcripts)
TEMPLATE_HINTS = [
    "Рабочий день", "рабочий день", "Несколько записей", "несколько записей",
    "содержание неизвестно", "без описательных", "Без даты", "Без названия",
    "записей без описательных", "встреч и активностей", "встречи или активности",
    "в середине дня", "во второй половине дня",
]


def is_template_theme(theme: str) -> bool:
    return any(hint.lower() in theme.lower() for hint in TEMPLATE_HINTS)


def is_raw_stem(stem: str) -> bool:
    """True если stem — это «сырое» имя диктофона: 2026-05-19 19_28_07 и т.п."""
    if re.match(r"^\d{4}-\d{2}-\d{2}[\s_]+\d{2}[_:]\d{2}[_:]\d{2}$", stem.strip()):
        return True
    return False


def load_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    if not ENV_FILE.exists(): return
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            os.environ["ANTHROPIC_API_KEY"] = value
            return


def sanitise_folder(name: str, max_len: int = 130) -> str:
    s = INVALID_FS.sub(" ", name)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .-_") + "…"
    return s or "Без темы"


def title_from_stem(stem: str) -> str:
    s = stem
    s = re.sub(r"^\d{4}-\d{2}-\d{2}[\s_]+\d{2}[_:]\d{2}[_:]\d{2}$", "", s)
    if not s.strip(): return ""
    s = re.sub(r"^\d{4}-\d{2}-\d{2}[\s_]+", "", s)
    s = re.sub(r"^\d{2}-\d{2}[\s_]+", "", s)
    s = re.sub(r"^\d{1,2}[a-zA-Z]+\.?\d{2,4}[\s_]+#?\w*[\s_]+", "", s)
    s = re.sub(r"^[\s_\-]+", "", s)
    s = s.replace("_", " ").strip()
    return s


# ---------- Claude: title from transcript ---------------------------

TITLE_SYSTEM = """Ты — ассистент-каталогизатор аудио-записей. Тебе дан фрагмент транскрипта одной записи. Верни СТРОГО валидный JSON:

{ "title": "<краткое говорящее имя записи, 5-12 слов на русском>" }

ТРЕБОВАНИЯ:
- 5-12 слов на русском, передаёт суть разговора.
- Без эмодзи, без даты в начале, без слов "Запись", "Аудио", "Транскрипт".
- ЗАПРЕЩЕНЫ символы Windows-FS: < > : " / \\ | ? * и управляющие.
- Без точки в конце.

ФОРМАТ ОТВЕТА: только JSON, без markdown-обёрток."""


THEME_SYSTEM = """Ты — ассистент-каталогизатор личного аудиоархива. Тебе дан список заголовков нескольких записей за один день. Верни СТРОГО валидный JSON:

{ "theme": "<преобладающая тема дня, 5-12 слов на русском>" }

ТРЕБОВАНИЯ:
- Передаёт общий стержень дня. Если темы разные — через запятую.
- 5-12 слов, на русском, без даты, без эмодзи.
- ЗАПРЕЩЕНЫ символы Windows-FS: < > : " / \\ | ? * и управляющие.
- Не начинать со слов "Запись", "Записи", "Аудио", "День".
- Без точки в конце.

ФОРМАТ ОТВЕТА: только JSON, без markdown-обёрток."""


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m: return None
    try: return json.loads(m.group(0))
    except json.JSONDecodeError: return None


def claude_title_from_transcript(client, model, transcript_text: str, log) -> str | None:
    """Получить заголовок одной записи из её транскрипта."""
    head = transcript_text[:3000]
    user = f"Фрагмент транскрипта:\n\n---\n{head}\n---\n\nВерни JSON с title."
    for attempt in (1, 2):
        try:
            resp = client.messages.create(
                model=model, max_tokens=256,
                system=[{"type": "text", "text": TITLE_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                log("    ! rate-limit, sleep 30")
                time.sleep(30); continue
            log(f"    ! API error: {e}")
            time.sleep(3); continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = _extract_json(text)
        if data and "title" in data:
            return data["title"]
    return None


def claude_theme(client, model, titles: list[str], log) -> str | None:
    bullets = "\n".join(f"- {t}" for t in titles)
    user = f"Заголовки записей за один день:\n\n{bullets}\n\nВерни JSON с theme."
    for attempt in (1, 2, 3):
        try:
            resp = client.messages.create(
                model=model, max_tokens=512,
                system=[{"type": "text", "text": THEME_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                log("    ! rate-limit, sleep 30")
                time.sleep(30); continue
            log(f"    ! API error: {e}")
            time.sleep(3); continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = _extract_json(text)
        if data and "theme" in data:
            return data["theme"]
    return None


# ---------- Main ---------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-template", action="store_true",
                    help="Только папки с шаблонной темой (Рабочий день, ...)")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"refresh_themes_{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log_fp = log_path.open("w", encoding="utf-8")
    def log(msg=""):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        print(line, flush=True); log_fp.write(line + "\n"); log_fp.flush()

    log(f"=== refresh_themes START (dry-run={args.dry_run}, only-template={args.only_template}) ===")
    log(f"log: {log_path}")

    if not args.dry_run:
        load_api_key()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY не задан")
        import anthropic
        client = anthropic.Anthropic()
    else:
        client = None

    # Найти все папки
    folders = []
    for d in sorted(DICT.iterdir()):
        if not d.is_dir(): continue
        m = FOLDER_RE.match(d.name)
        if not m: continue
        date_str = m.group(1)
        current_theme = m.group(2).strip(" .-_")
        if args.only_template and not is_template_theme(current_theme):
            continue
        folders.append((d, date_str, current_theme))

    log(f"[scan] нашёл {len(folders)} папок")
    if args.limit:
        folders = folders[: args.limit]
        log(f"[scan] limit → {len(folders)}")

    renamed = 0
    skipped_same = 0
    for d, date_str, current_theme in folders:
        log(f"\n▸ {d.name}")
        # Собрать stems + источники
        records = []  # list of (stem, title_or_None, transcript_path or None)
        for f in d.iterdir():
            if not f.is_file(): continue
            if f.suffix.lower() not in AUDIO_EXTS: continue
            stem = f.stem
            t_path = d / f"{stem}-transcript.txt"
            t_from_stem = title_from_stem(stem)
            records.append((stem, t_from_stem if not is_raw_stem(stem) else None,
                            t_path if t_path.exists() else None))

        # Для записей без говорящего stem — извлечь title из transcript
        for i, (stem, title, t_path) in enumerate(records):
            if title is not None:
                continue
            if t_path is None or t_path.stat().st_size < 200:
                continue
            if client:
                try:
                    txt = t_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                log(f"    · извлекаю title для {stem}…")
                t = claude_title_from_transcript(client, CLAUDE_MODEL, txt, log)
                if t:
                    records[i] = (stem, t, t_path)
                    log(f"      → {t}")

        titles = [t for (_, t, _) in records if t]
        if not titles:
            log("    (нет titles, пропускаю)")
            continue

        # Получить общую тему
        if len(titles) == 1:
            theme = titles[0]
        elif client:
            log(f"    спрашиваю Claude для общей темы из {len(titles)} заголовков")
            theme = claude_theme(client, CLAUDE_MODEL, titles, log) or titles[0]
        else:
            theme = titles[0]

        new_name = sanitise_folder(f"Записи за {date_str} - {theme}")
        if new_name == d.name:
            log(f"    = тема не изменилась")
            skipped_same += 1
            continue
        log(f"    NEW: {new_name}")
        if args.dry_run:
            continue
        # rename
        target = DICT / new_name
        if target.exists():
            # коллизия — добавим суффикс времени
            target = DICT / f"{new_name} ({datetime.now():%H%M%S})"
        try:
            d.rename(target)
            log(f"    ✓ переименовано: {target.name}")
            renamed += 1
        except OSError as e:
            log(f"    ! ошибка rename: {e}")

    log(f"\n=== ИТОГ ===")
    log(f"  Обработано папок:   {len(folders)}")
    log(f"  Переименовано:      {renamed}")
    log(f"  Тема не изменилась: {skipped_same}")
    log_fp.close()


if __name__ == "__main__":
    main()
