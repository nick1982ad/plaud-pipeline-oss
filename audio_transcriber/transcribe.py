#!/usr/bin/env python3
"""
transcribe.py — folder-watcher для локальной транскрипции аудио (mp3/m4a/wav/opus/...).

Workflow:
  1. Положи аудио-файл в audio_transcriber/inbox/
  2. Скрипт его подхватит, переместит в processing/, транскрибирует,
     результат сложит в done/ (рядом с аудио — *-transcript.txt и *-meta.json).
  3. Можно запускать как demon (mode=watch) или однократно (mode=once).

Зависимости:
  pip install faster-whisper imageio-ffmpeg

Модели качаются автоматически в ~/.cache/huggingface/ при первом запуске.
Размеры моделей: tiny ~75MB, base ~150MB, small ~500MB, medium ~1.5GB, large-v3 ~3GB.
Для русского рекомендуется medium или large-v3.
"""
import argparse
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
PROCESSING = ROOT / "processing"
DONE = ROOT / "done"
LOG_DIR = ROOT / "_logs"
CONFIG_PATH = ROOT / "config.json"

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac",
              ".mp4", ".aac", ".wma", ".webm"}

# Force UTF-8 stdout (Windows cp1251 chokes on Russian).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_should_stop = False


def _sigint(_a, _b):
    global _should_stop
    _should_stop = True
    print("\n[!] stopping after current file...")


signal.signal(signal.SIGINT, _sigint)


def log_setup():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"run-{datetime.now():%Y-%m-%d-%H%M%S}.log"


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"config.json not found at {CONFIG_PATH}\n"
                 f"Copy config.example.json -> config.json first.")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_model(cfg):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper not installed.\n"
                 "Install:  pip install faster-whisper imageio-ffmpeg")

    # Optional: register imageio-ffmpeg binary so faster-whisper can decode without
    # system ffmpeg on PATH.
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        os.environ["PATH"] = f"{Path(ff).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    except ImportError:
        pass

    model_name = cfg.get("model", "medium")
    device = cfg.get("device", "auto")
    compute_type = cfg.get("compute_type", "auto")

    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    print(f"[model] loading {model_name!r} on {device!r} (compute_type={compute_type!r})")
    print(f"[model] first run downloads ~MB-GB of model files to ~/.cache/huggingface/")
    return WhisperModel(model_name, device=device, compute_type=compute_type)


def format_ts(seconds):
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_human(seconds):
    """Человекочитаемая длительность: '1м 40с', '2ч 05м'."""
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м"
    if m:
        return f"{m}м {sec:02d}с"
    return f"{sec}с"


def progress_bar(frac, width=28):
    """Юникод-бар: ▕████████░░░░░░░░▏ — frac в диапазоне 0..1."""
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "▕" + "█" * filled + "░" * (width - filled) + "▏"


def render_live(stream, frac, pos, dur_h, eta_h, chars):
    """Живой прогресс-бар в TTY (перерисовка через \\r). В пайпе ничего не делает."""
    try:
        if not stream.isatty():
            return
    except (AttributeError, ValueError):
        return
    pct = f"{int((frac or 0) * 100):3d}%"
    bar = progress_bar(frac)
    eta = f"осталось ~{eta_h}" if eta_h else "оцениваю…"
    line = f"\r  🎙  {bar} {pct}  {pos}/{dur_h}  {eta}  {chars} симв.   "
    stream.write(line)
    stream.flush()


def transcribe_audio(model, audio_path, cfg, log):
    language = cfg.get("language", "ru")
    beam_size = int(cfg.get("beam_size", 5))
    vad_filter = bool(cfg.get("vad_filter", True))

    log(f"[transcribe] {audio_path.name} (lang={language}, beam={beam_size})")
    segments, info = model.transcribe(
        str(audio_path),
        language=language if language and language != "auto" else None,
        beam_size=beam_size,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 500} if vad_filter else None,
    )

    duration = info.duration or 0
    dur_h = format_ts(duration) if duration else "??:??"
    log(f"[transcribe]   ▶ длительность {dur_h}, язык={info.language} "
        f"({int((info.language_probability or 0) * 100)}%) — распознаю…")

    try:
        is_tty = sys.stderr.isatty()
    except (AttributeError, ValueError):
        is_tty = False

    lines = []
    char_count = 0
    t0 = time.time()
    last_log = t0
    for i, seg in enumerate(segments):
        ts = format_ts(seg.start)
        text = seg.text.strip()
        lines.append(f"[{ts}] {text}")
        char_count += len(text)

        # Прогресс: доля = позиция в аудио / общая длительность.
        frac = (seg.end / duration) if duration else None
        elapsed_now = time.time() - t0
        eta_h = None
        if frac and frac > 0.01:
            eta_h = format_human(elapsed_now / frac - elapsed_now)
        if is_tty:
            # Прямой терминал: живой бар с перерисовкой через \r.
            render_live(sys.stderr, frac, format_ts(seg.end), dur_h, eta_h, char_count)
        else:
            # Захваченный вывод (пайплайн/монитор): текстовая строка прогресса
            # каждые 50 сегментов или раз в 30 с — живой бар тут недоступен.
            now = time.time()
            if (i + 1) % 50 == 0 or (now - last_log) >= 30:
                last_log = now
                pct = int((frac or 0) * 100)
                eta_txt = f", осталось ~{eta_h}" if eta_h else ""
                log(f"[progress] {progress_bar(frac, 20)} {pct:3d}% • "
                    f"{format_ts(seg.end)}/{dur_h}{eta_txt} • "
                    f"{i+1} сегм., {char_count} симв.")

    # Закрыть строку живого бара переводом строки.
    try:
        if sys.stderr.isatty():
            sys.stderr.write("\n")
            sys.stderr.flush()
    except (AttributeError, ValueError):
        pass

    elapsed = time.time() - t0
    transcript = "\n".join(lines)
    meta = {
        "filename": audio_path.name,
        "duration_sec": round(info.duration, 2) if info.duration else None,
        "duration_human": format_ts(info.duration) if info.duration else None,
        "detected_language": info.language,
        "language_probability": round(info.language_probability, 3) if info.language_probability else None,
        "transcribed_at_utc": datetime.utcnow().isoformat() + "Z",
        "model": cfg.get("model", "medium"),
        "compute_type": cfg.get("compute_type", "auto"),
        "elapsed_sec": round(elapsed, 1),
        "speed_x_realtime": round(info.duration / elapsed, 2) if info.duration and elapsed > 0 else None,
        "characters": char_count,
        "segments": len(lines),
    }
    return transcript, meta


def process_file(model, audio_in, cfg, log):
    proc_path = PROCESSING / audio_in.name
    transcript_path = DONE / f"{audio_in.stem}-transcript.txt"

    # Skip if already done
    if transcript_path.exists():
        log(f"[skip] ⏭ уже распознано ранее: {transcript_path.name}")
        # move audio to done anyway to clear inbox
        target = DONE / audio_in.name
        if not target.exists():
            shutil.move(str(audio_in), str(target))
        else:
            audio_in.unlink()  # duplicate, drop from inbox
        return "skipped"

    shutil.move(str(audio_in), str(proc_path))
    try:
        transcript, meta = transcribe_audio(model, proc_path, cfg, log)
        transcript_path.write_text(transcript, encoding="utf-8")
        meta_path = DONE / f"{audio_in.stem}-meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        shutil.move(str(proc_path), str(DONE / audio_in.name))
        log(f"[ok] ✅ Готово: {audio_in.name} → "
            f"{meta['characters']} симв., {meta['segments']} сегм., "
            f"{meta['speed_x_realtime']}× реального времени, "
            f"за {format_human(meta['elapsed_sec'])}")
        return "done"
    except Exception as e:
        # rollback to inbox for next attempt
        try:
            shutil.move(str(proc_path), str(INBOX / audio_in.name))
        except Exception:
            pass
        log(f"[error] ❌ Ошибка при распознавании {audio_in.name}: {e}")
        return "error"


def list_inbox_audio():
    return sorted(p for p in INBOX.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def watch_mode(model, cfg, log, interval):
    log(f"[watch] polling {INBOX} every {interval}s. Drop audio files into inbox/.")
    log(f"[watch] Ctrl+C to stop.")
    while not _should_stop:
        files = list_inbox_audio()
        for audio in files:
            if _should_stop:
                break
            process_file(model, audio, cfg, log)
        if _should_stop:
            break
        time.sleep(interval)
    log("[watch] stopped.")


def once_mode(model, cfg, log):
    files = list_inbox_audio()
    log(f"[once] 📥 в очереди {len(files)} аудио-файл(ов)")
    stats = {"done": 0, "skipped": 0, "error": 0}
    for n, audio in enumerate(files, 1):
        if _should_stop:
            break
        log(f"[once] ── [{n}/{len(files)}] {audio.name}")
        result = process_file(model, audio, cfg, log)
        stats[result] = stats.get(result, 0) + 1
    log(f"[once] 📊 Итог: ✅ {stats['done']} готово, "
        f"⏭ {stats['skipped']} пропущено, ❌ {stats['error']} с ошибкой. "
        f"{stats}")


def main():
    ap = argparse.ArgumentParser(description="Audio transcriber (faster-whisper)")
    ap.add_argument("--mode", choices=["watch", "once"], default=None,
                    help="watch = daemon (default); once = process current inbox and exit")
    ap.add_argument("--interval", type=int, default=None,
                    help="poll interval in seconds for watch mode (default 5)")
    args = ap.parse_args()

    cfg = load_config()
    mode = args.mode or cfg.get("mode", "watch")
    interval = args.interval or int(cfg.get("interval_sec", 5))

    for d in (INBOX, PROCESSING, DONE, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    log_path = log_setup()
    log_file = log_path.open("w", encoding="utf-8")

    def log(msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    log(f"=== audio_transcriber started in mode={mode!r} ===")
    log(f"config: {CONFIG_PATH}")
    log(f"log:    {log_path}")

    try:
        model = load_model(cfg)
    except SystemExit:
        raise
    except Exception as e:
        log(f"[fatal] failed to load model: {e}")
        sys.exit(1)

    try:
        if mode == "once":
            once_mode(model, cfg, log)
        else:
            watch_mode(model, cfg, log, interval)
    finally:
        log_file.close()


if __name__ == "__main__":
    main()
