#!/usr/bin/env python3
"""
organize_by_date.py
===================

Раскладывает уже скачанные записи в C:\\_Dictophone по папкам:
   "Записи за ДД.ММ.ГГГГ - <Преобладающая тема>"

Логика:
  1. Skip-set: все stems, уже лежащие в подпапках "Записи за..." / "Записи с..."
  2. Скан C:\\_Dictophone\\*.{mp3,WAV,wav} в корне.
  3. Для каждого stem ищу transcript/summary в категориальных подпапках
     (IRIT-RTF, RectorSchool, Telegram, Кампус, Партнёрства_индустрия, ИИ_*,
     Образовательные_программы, Прочее, Прочее_test, Я_как_проект, 14.05.2026, plaud).
  4. Извлекаю дату из имени (MM-DD..., YYYY-MM-DD..., 10oct.2025..., 11nov25, 7july.2025).
     Fallback — mtime.
  5. Группирую по дате (по дню).
  6. Для каждой даты выбираю «преобладающую тему»:
     - 1 stem — берётся его существующее «говорящее» имя без префикса даты.
     - 2+ stems — одним Claude вызовом просим общую формулировку (5-12 слов).
  7. Создаю "Записи за ДД.ММ.ГГГГ - <тема>".
  8. Перемещаю mp3 из корня в эту папку. Копирую transcript/summary из категориальных.

CLI:
  python organize_by_date.py --dry-run         # показать план, ничего не делать
  python organize_by_date.py                   # выполнить
  python organize_by_date.py --no-llm          # не звать Claude, тема = первый stem
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = Path(__file__).resolve().parent
DICT = Path(r"C:\_Dictophone")
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_CONTEXT_CHARS = 80_000

INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".opus", ".ogg", ".flac"}

# Папки в _Dictophone, которые НЕ трогаем (служебные / уже разобранные)
SKIP_ROOT_DIRS = {
    ".claude", "_processed", "_scripts", "_tools", "plaud",
}

# Папки в _Dictophone, из которых КОПИРУЕМ transcript/summary
# (категориальные с парами -transcript.txt + -Summary.txt)
CATEGORY_DIRS = [
    "IRIT-RTF", "RectorSchool", "Telegram", "Кампус", "Партнёрства_индустрия",
    "ИИ_и_цифровые_двойники", "Образовательные_программы", "Прочее", "Прочее_test",
    "Я_как_проект", "14.05.2026", "plaud",
]

# Уже разобранные папки (skip-set источник)
ORGANISED_PREFIXES = ("Записи за", "Записи с")

MONTH_RU = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"jule":7,
    "aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}


# ---------- API key + Claude ---------------------------------------

def load_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            os.environ["ANTHROPIC_API_KEY"] = value
            return


# ---------- Date extraction ----------------------------------------

_MMDD_RE   = re.compile(r"^(\d{2})-(\d{2})[\s_]")
_YYYYMMDD_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DAY_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})([a-z]+)\.?(\d{2,4})", re.IGNORECASE)


def detect_date(stem: str, fallback_path: Path | None) -> tuple[datetime | None, bool]:
    """Return (date, has_date_in_name). has_date_in_name=False означает что
    извлечение шло по mtime — таким записям дата ненадёжна (день sync, не запись)."""
    m = _YYYYMMDD_RE.match(stem)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))), True
        except ValueError:
            pass
    m = _DAY_MONTH_YEAR_RE.match(stem)
    if m:
        try:
            day = int(m.group(1))
            mo = MONTH_RU.get(m.group(2).lower()[:3])
            if mo:
                year_raw = m.group(3)
                year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)
                return datetime(year, mo, day), True
        except (ValueError, KeyError):
            pass
    m = _MMDD_RE.match(stem)
    if m:
        try:
            month, day = int(m.group(1)), int(m.group(2))
            today = datetime.now()
            d = datetime(today.year, month, day)
            if (d - today).days > 60:
                d = datetime(today.year - 1, month, day)
            return d, True
        except ValueError:
            pass
    # Имя не содержит даты — НЕ доверяем mtime (это, скорее всего, день sync,
    # а не реальной записи). Помечаем has_date=False.
    return None, False


# ---------- Skip-set: stems already in organised folders -----------

def build_skip_set(log) -> set:
    """Stems внутри подпапок 'Записи за/с ...' — их трогать не надо."""
    skip = set()
    for d in DICT.iterdir():
        if not d.is_dir(): continue
        if not d.name.startswith(ORGANISED_PREFIXES): continue
        for f in d.iterdir():
            if not f.is_file(): continue
            n = f.name
            if n.endswith("-transcript.txt"):
                skip.add(n[:-len("-transcript.txt")])
            elif n.endswith("-Summary.txt"):
                skip.add(n[:-len("-Summary.txt")])
            elif f.suffix.lower() in AUDIO_EXTS:
                skip.add(f.stem)
            elif f.suffix.lower() in (".md", ".pdf"):
                skip.add(f.stem)
    log(f"[skip] {len(skip)} stems уже в организованных папках")
    return skip


# ---------- Index category dirs (where transcripts/summaries live) -

def index_categories(log) -> tuple[dict, dict]:
    """Возвращает (stem→transcript_path, stem→summary_path)."""
    t_index = {}
    s_index = {}
    for cat in CATEGORY_DIRS:
        cd = DICT / cat
        if not cd.is_dir(): continue
        for f in cd.rglob("*"):
            if not f.is_file(): continue
            n = f.name
            if n.endswith("-transcript.txt"):
                t_index.setdefault(n[:-len("-transcript.txt")], f)
            elif n.endswith("-Summary.txt"):
                s_index.setdefault(n[:-len("-Summary.txt")], f)
    log(f"[index] transcripts: {len(t_index)}, summaries: {len(s_index)}")
    return t_index, s_index


# ---------- Title extraction from existing data --------------------

def title_from_stem(stem: str) -> str:
    """Убираем дату-префикс, _, etc. Возвращаем читаемый человеком текст.
    Если в stem ТОЛЬКО timestamp (2026-05-19 12_38_06) — возвращаем пустую строку."""
    s = stem
    s = re.sub(r"^\d{4}-\d{2}-\d{2}[\s_]+\d{2}[_:]\d{2}[_:]\d{2}$", "", s)  # чистый timestamp
    if not s.strip():
        return ""
    s = re.sub(r"^\d{4}-\d{2}-\d{2}[\s_]+", "", s)
    s = re.sub(r"^\d{2}-\d{2}[\s_]+", "", s)
    s = re.sub(r"^\d{1,2}[a-zA-Z]+\.?\d{2,4}[\s_]+#?\w*[\s_]+", "", s)
    s = re.sub(r"^[\s_\-]+", "", s)
    s = s.replace("_", " ").strip()
    return s


def title_from_summary(summary_path: Path) -> str | None:
    """Читаем -Summary.txt и берём первую содержательную строку."""
    try:
        text = summary_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None
    # Пытаемся найти строку после "Тема:"
    for i, line in enumerate(lines):
        if line.lower().startswith(("тема:", "тема -", "тема —", "## тема")):
            rest = line.split(":", 1)[-1].strip() if ":" in line else ""
            if rest:
                return rest[:120]
            if i + 1 < len(lines):
                return lines[i+1][:120]
    # Иначе первая строка
    return lines[0][:120]


# ---------- Consolidate theme for a date group via Claude ----------

CONSOLIDATE_SYSTEM = """Ты — ассистент-каталогизатор личного аудиоархива. Тебе дан список заголовков нескольких записей за один день. Верни СТРОГО валидный JSON одной формы:

{ "theme": "<преобладающая тема дня, 5-12 слов на русском>" }

ТРЕБОВАНИЯ К theme:
- Передаёт общий стержень дня (преобладающая активность, главная встреча или тема).
- Если темы РАЗНЫЕ — суммируй через запятую (например: "Стратсессия радиофака, кадровые вопросы, защита диссертации").
- 5-12 слов, на русском, без даты, без эмодзи.
- ЗАПРЕЩЕНО: символы Windows-FS: < > : " / \\ | ? * и управляющие символы.
- Не начинать со слов "Запись", "Записи", "Аудио", "Транскрипт", "День".
- Без точки в конце.

ФОРМАТ ОТВЕТА: только JSON, без markdown code-fence."""


def consolidate_theme(client, model, titles: list[str], log) -> str | None:
    if not titles:
        return None
    bullets = "\n".join(f"- {t}" for t in titles)
    user_prompt = f"Заголовки записей за один день:\n\n{bullets}\n\nВерни JSON с полем theme."
    for attempt in (1, 2, 3):
        try:
            resp = client.messages.create(
                model=model, max_tokens=512,
                system=[{"type": "text", "text": CONSOLIDATE_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit" in msg.lower():
                log(f"    ! rate-limit, sleep 30s")
                time.sleep(30); continue
            log(f"    ! API error: {e}")
            time.sleep(3); continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                if "theme" in data:
                    return data["theme"]
            except json.JSONDecodeError:
                pass
        log(f"    ! invalid JSON attempt {attempt}: {text[:120]!r}")
    return None


# ---------- Folder name sanitisation -------------------------------

def sanitise_folder_name(name: str, max_len: int = 130) -> str:
    s = INVALID_FS.sub(" ", name)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .-_") + "…"
    return s or "Без темы"


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
    raise RuntimeError(f"cannot find unique name under {parent} for {name!r}")


# ---------- Main ---------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Показать план, ничего не делать")
    ap.add_argument("--no-llm", action="store_true", help="Не звать Claude, theme = первый title")
    ap.add_argument("--limit", type=int, default=None, help="Ограничить обработку первыми N датами (для отладки)")
    args = ap.parse_args()

    log_path = ROOT / "_logs" / f"organize_{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("w", encoding="utf-8")

    def log(msg=""):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        print(line, flush=True)
        log_fp.write(line + "\n"); log_fp.flush()

    log(f"=== organize_by_date START (dry-run={args.dry_run}, no-llm={args.no_llm}) ===")
    log(f"target:    {DICT}")
    log(f"log:       {log_path}")

    if not args.no_llm and not args.dry_run:
        load_api_key()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("[!] ANTHROPIC_API_KEY не задан — переключаюсь на --no-llm")
            args.no_llm = True

    # 1. Build skip-set
    skip = build_skip_set(log)

    # 2. Scan root for audio
    audio_files = []
    for f in DICT.iterdir():
        if not f.is_file(): continue
        if f.suffix.lower() not in AUDIO_EXTS: continue
        if f.stem in skip:
            continue
        audio_files.append(f)
    log(f"[scan] {len(audio_files)} аудио-файлов в корне (после skip-set)")

    # 3. Index categorical dirs
    t_index, s_index = index_categories(log)

    # 4. Build records
    records = []   # dict per file
    for audio in audio_files:
        stem = audio.stem
        rec = {
            "stem": stem,
            "audio": audio,
            "transcript": t_index.get(stem),
            "summary": s_index.get(stem),
            "date": None,
            "title": None,
        }
        rec["date"], rec["has_date_in_name"] = detect_date(stem, audio)
        # Title: сначала пробуем из stem (часто говорящий), иначе из Plaud-summary (1-я строка).
        rec["title"] = title_from_stem(stem)
        if not rec["title"] and rec["summary"]:
            t = title_from_summary(rec["summary"])
            if t:
                rec["title"] = t
        if not rec["title"]:
            rec["title"] = stem
        records.append(rec)

    log(f"[records] {len(records)} записей сформировано")

    # Records without date in NAME — to "Без даты" bucket (mtime ненадёжен)
    by_date = defaultdict(list)
    no_date = []
    for r in records:
        if r["has_date_in_name"] and r["date"]:
            by_date[r["date"].date()].append(r)
        else:
            no_date.append(r)
    log(f"[group] {len(by_date)} уникальных дат, {len(no_date)} без даты в имени")
    if args.limit:
        keys = sorted(by_date.keys())[:args.limit]
        by_date = {k: by_date[k] for k in keys}
        log(f"[group] limited to first {args.limit} dates")

    # 5. Resolve theme per date
    client = None
    if not args.no_llm and not args.dry_run:
        import anthropic
        client = anthropic.Anthropic()

    plan = []  # list of (date, theme, folder_name, [records])
    for d in sorted(by_date.keys()):
        recs = by_date[d]
        date_str = d.strftime("%d.%m.%Y")
        if len(recs) == 1:
            theme = recs[0]["title"]
        else:
            titles = [r["title"] for r in recs if r["title"]]
            if args.no_llm or client is None:
                # take first
                theme = titles[0] if titles else "Несколько записей"
            else:
                log(f"  • {date_str}: {len(recs)} записей — спрашиваю Claude для общей темы")
                theme = consolidate_theme(client, CLAUDE_MODEL, titles, log) or titles[0]
        folder_name = sanitise_folder_name(f"Записи за {date_str} - {theme}")
        plan.append((d, theme, folder_name, recs))

    if no_date:
        folder_name = sanitise_folder_name("Записи без даты")
        plan.append((None, "Без даты", folder_name, no_date))

    # 6. Print plan
    log("")
    log("=== ПЛАН РАЗБИВКИ ===")
    for d, theme, folder_name, recs in plan:
        log(f"  ▸ {folder_name}  ({len(recs)} зап.)")
        for r in recs[:6]:
            has_t = "✓" if r["transcript"] else "·"
            has_s = "✓" if r["summary"] else "·"
            log(f"      [t:{has_t} s:{has_s}]  {r['stem'][:80]}")
        if len(recs) > 6:
            log(f"      … ещё {len(recs) - 6}")

    total_records = sum(len(recs) for _, _, _, recs in plan)
    log("")
    log(f"Итого: {len(plan)} папок, {total_records} записей")
    log("")

    if args.dry_run:
        log("=== dry-run, ничего не выполнено ===")
        log_fp.close()
        return

    # 7. Execute: create dirs, move mp3, copy transcripts/summaries
    log("=== ВЫПОЛНЯЮ ПЕРЕМЕЩЕНИЕ ===")
    for d, theme, folder_name, recs in plan:
        target = ensure_unique_dir(DICT, folder_name)
        log(f"\n→ {target.name}")
        for r in recs:
            # move mp3
            new_audio = target / r["audio"].name
            try:
                shutil.move(str(r["audio"]), str(new_audio))
                log(f"  · move audio: {r['audio'].name}")
            except Exception as e:
                log(f"  ! move audio fail ({r['audio'].name}): {e}")
            # copy transcript
            if r["transcript"]:
                new_t = target / r["transcript"].name
                if not new_t.exists():
                    try:
                        shutil.copy2(str(r["transcript"]), str(new_t))
                        log(f"  · copy transcript: {r['transcript'].name}")
                    except Exception as e:
                        log(f"  ! copy transcript fail: {e}")
            # copy summary
            if r["summary"]:
                new_s = target / r["summary"].name
                if not new_s.exists():
                    try:
                        shutil.copy2(str(r["summary"]), str(new_s))
                        log(f"  · copy summary: {r['summary'].name}")
                    except Exception as e:
                        log(f"  ! copy summary fail: {e}")

    log("")
    log("=== ГОТОВО ===")
    log_fp.close()


if __name__ == "__main__":
    main()
