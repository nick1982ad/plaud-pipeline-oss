#!/usr/bin/env python3
"""
transcribe_missing.py
=====================

Находит в C:\\_Dictophone\\Записи*  все mp3/wav без -transcript.txt (или с пустым)
и прогоняет их через локальный Whisper (transcribe.py --mode once).
По завершении кладёт готовый -transcript.txt рядом с каждым audio файлом в родной папке.

CLI:
  python transcribe_missing.py                       # CPU faster-whisper
  python transcribe_missing.py --openvino            # Intel iGPU (Arc 140T) через OpenVINO
  python transcribe_missing.py --openvino --device CPU   # OpenVINO на CPU (fallback)
  python transcribe_missing.py --dry-run
  python transcribe_missing.py --limit 10
"""
from __future__ import annotations

import argparse
import os
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
DICT = Path(r"C:\_Dictophone")
FROMPLAUD = ROOT.parent
TRANSCRIBER = FROMPLAUD / "audio_transcriber"
INBOX = TRANSCRIBER / "inbox"
DONE = TRANSCRIBER / "done"
LOG_DIR = ROOT / "_logs"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".opus", ".ogg", ".flac"}


def find_missing():
    """Yields (audio_path, parent_folder)."""
    missing = []
    for d in sorted(DICT.iterdir()):
        if not d.is_dir(): continue
        if not d.name.startswith("Записи"): continue
        for f in d.iterdir():
            if not f.is_file(): continue
            if f.suffix.lower() not in AUDIO_EXTS: continue
            t = d / f"{f.stem}-transcript.txt"
            if not t.exists() or t.stat().st_size < 200:
                missing.append((f, d))
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--openvino", action="store_true",
                    help="Использовать transcribe_openvino.py (Intel iGPU/GPU) вместо CPU faster-whisper")
    ap.add_argument("--device", default="GPU",
                    help="device для OpenVINO: GPU (по умолч.) или CPU")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"transcribe_missing_{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log_fp = log_path.open("w", encoding="utf-8")

    def log(msg=""):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {msg}"
        print(line, flush=True)
        log_fp.write(line + "\n"); log_fp.flush()

    log(f"=== transcribe_missing START (dry-run={args.dry_run}) ===")
    log(f"log: {log_path}")

    missing = find_missing()
    if args.limit:
        missing = missing[: args.limit]
    log(f"[scan] {len(missing)} файл(ов) без расшифровки")

    if not missing:
        log("Ничего делать. Все аудио расшифрованы.")
        log_fp.close()
        return

    # Print plan
    for f, d in missing[:20]:
        log(f"  · {d.name} / {f.name}")
    if len(missing) > 20:
        log(f"  … ещё {len(missing) - 20}")

    if args.dry_run:
        log("=== dry-run — ничего не делаю ===")
        log_fp.close()
        return

    # Стратегия: для каждого файла отдельный временный inbox-файл с уникальным
    # stem'ом (потому что транскрибер по stem'у определяет результат). У нас
    # же stem'ы могут конфликтовать между папками — поэтому будем обрабатывать
    # партиями по 10 файлов через одну общую inbox, и после — раскладывать
    # готовые transcripts в нужные папки по индексу stem→target_folder.

    target_index = {}  # stem -> target_folder (Path)
    INBOX.mkdir(parents=True, exist_ok=True)
    queued = 0
    for f, d in missing:
        target = INBOX / f.name
        if target.exists():
            # collision — already in inbox from another folder; warn
            log(f"  ! collision in inbox, skip ({f.name})")
            continue
        if (DONE / f"{f.stem}-transcript.txt").exists():
            # already done in this whisper instance — just copy back
            src_t = DONE / f"{f.stem}-transcript.txt"
            dst_t = d / f"{f.stem}-transcript.txt"
            if not dst_t.exists():
                shutil.copy2(str(src_t), str(dst_t))
                log(f"  · скопировал готовый transcript: {dst_t.relative_to(DICT)}")
            continue
        try:
            shutil.copy2(str(f), str(target))
            queued += 1
            target_index[f.stem] = d
        except Exception as e:
            log(f"  ! не удалось скопировать в inbox ({f.name}): {e}")
    log(f"[whisper] поставлено в очередь: {queued} файл(ов)")

    if queued == 0:
        log("Ничего не нужно делать (всё уже либо в done/, либо collision).")
        log_fp.close()
        return

    # Run whisper
    if args.openvino:
        log(f"[whisper] запускаю transcribe_openvino.py --device {args.device} (Intel iGPU/GPU)…")
        cmd = [sys.executable, "-u", str(TRANSCRIBER / "transcribe_openvino.py"),
               "--mode", "once", "--device", args.device]
    else:
        log("[whisper] запускаю transcribe.py --mode once (CPU faster-whisper)…")
        cmd = [sys.executable, "-u", str(TRANSCRIBER / "transcribe.py"), "--mode", "once"]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(TRANSCRIBER),
            stdout=subprocess.PIPE,
            stderr=None,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env,
        )
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log(f"[whisper]  {line}")
        rc = proc.wait()
        if rc != 0:
            log(f"[whisper] ! exit {rc}")
    except Exception as e:
        log(f"[whisper] ! ошибка: {e}")

    # Распределить готовые transcripts по родным папкам
    log("[dispatch] раскладываю готовые transcripts по папкам…")
    dispatched = 0
    for stem, target_dir in target_index.items():
        src_t = DONE / f"{stem}-transcript.txt"
        dst_t = target_dir / f"{stem}-transcript.txt"
        if not src_t.exists():
            log(f"  ! transcript не создан для: {stem}")
            continue
        if dst_t.exists() and dst_t.stat().st_size > 200:
            log(f"  = уже на месте: {dst_t.relative_to(DICT)}")
            continue
        try:
            shutil.copy2(str(src_t), str(dst_t))
            dispatched += 1
        except Exception as e:
            log(f"  ! ошибка копирования {stem}: {e}")
    log(f"[dispatch] скопировано transcripts в родные папки: {dispatched}/{queued}")

    log("=== ГОТОВО ===")
    log_fp.close()


if __name__ == "__main__":
    main()
