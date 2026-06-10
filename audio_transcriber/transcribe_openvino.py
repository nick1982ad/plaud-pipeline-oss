#!/usr/bin/env python3
"""
transcribe_openvino.py — транскрибация через openvino-genai на Intel Arc / iGPU.

Та же inbox/processing/done структура что у transcribe.py, но использует Intel GPU
вместо CPU. На Intel Arc 140T ожидается ~5-10x ускорение по сравнению с CPU+int8.

Первый запуск:
  - Качает модель ~600 MB в _models/whisper-medium-ov (один раз)
  - Прогревает pipeline ~5-15 сек

Использование:
  python transcribe_openvino.py                       # GPU, медленно если CPU
  python transcribe_openvino.py --device GPU
  python transcribe_openvino.py --device CPU          # fallback на CPU
  python transcribe_openvino.py --model-id OpenVINO/whisper-large-v3-int8-ov
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
MODELS = ROOT / "_models"

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac",
              ".mp4", ".aac", ".wma", ".webm"}

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


def load_audio_16k_mono(path):
    """Decode any audio format → np.float32 16-kHz mono via librosa+soundfile."""
    import numpy as np
    import librosa
    audio, _ = librosa.load(str(path), sr=16000, mono=True)
    return audio.astype(np.float32)


def format_ts(seconds):
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def download_model(model_id, target_dir, log):
    from huggingface_hub import snapshot_download
    target_dir.mkdir(parents=True, exist_ok=True)
    log(f"[model] downloading {model_id} → {target_dir}")
    log(f"[model] (~600 MB; one-time)")
    snapshot_download(repo_id=model_id, local_dir=str(target_dir))
    log(f"[model] download complete")


def main():
    ap = argparse.ArgumentParser(description="OpenVINO Whisper transcriber")
    ap.add_argument("--device", default="GPU",
                    help="GPU | CPU | AUTO (default GPU)")
    ap.add_argument("--model-id", default="OpenVINO/whisper-medium-fp16-ov",
                    help="HuggingFace repo for OpenVINO Whisper model")
    ap.add_argument("--model-dir", default=None,
                    help="Local path override; defaults to _models/<model-id-tail>")
    ap.add_argument("--language", default="ru")
    ap.add_argument("--mode", choices=["watch", "once"], default="once")
    ap.add_argument("--interval", type=int, default=5)
    args = ap.parse_args()

    for d in (INBOX, PROCESSING, DONE, LOG_DIR, MODELS):
        d.mkdir(parents=True, exist_ok=True)

    log_path = LOG_DIR / f"run-openvino-{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log_file = log_path.open("w", encoding="utf-8")

    def log(msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    log(f"=== transcribe_openvino mode={args.mode!r} device={args.device!r} ===")
    log(f"log: {log_path}")

    # Model dir
    model_dir = Path(args.model_dir) if args.model_dir else (
        MODELS / args.model_id.split("/")[-1])

    # Download if missing or incomplete
    if not model_dir.exists() or not any(model_dir.glob("*.xml")):
        try:
            download_model(args.model_id, model_dir, log)
        except Exception as e:
            log(f"[fatal] model download failed: {e}")
            log_file.close()
            sys.exit(1)
    else:
        log(f"[model] using cached at {model_dir}")

    # Load OpenVINO
    try:
        import openvino as ov
        import openvino_genai as ov_genai
    except ImportError as e:
        log(f"[fatal] {e}")
        log(f"install: pip install openvino-genai librosa huggingface_hub")
        log_file.close()
        sys.exit(1)

    core = ov.Core()
    log(f"[ov] {ov.__version__}; devices: {core.available_devices}")
    for dev in core.available_devices:
        try:
            name = core.get_property(dev, "FULL_DEVICE_NAME")
            log(f"[ov]   {dev}: {name}")
        except Exception:
            pass

    log(f"[model] compiling on {args.device}...")
    t0 = time.time()
    try:
        pipe = ov_genai.WhisperPipeline(str(model_dir), args.device)
    except Exception as e:
        log(f"[fatal] pipeline init failed: {e}")
        log(f"[hint] try --device CPU or check that {args.device} is in devices list")
        log_file.close()
        sys.exit(1)
    log(f"[model] compiled in {time.time() - t0:.1f}s")

    # Process inbox
    files = sorted(p for p in INBOX.iterdir()
                   if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    log(f"[once] {len(files)} file(s) in inbox")
    stats = {"done": 0, "skipped": 0, "error": 0}

    while True:
        for audio_in in files:
            if _should_stop:
                break
            transcript_path = DONE / f"{audio_in.stem}-transcript.txt"
            if transcript_path.exists():
                log(f"[skip] {audio_in.name} (transcript exists)")
                target = DONE / audio_in.name
                if not target.exists():
                    try:
                        shutil.move(str(audio_in), str(target))
                    except Exception:
                        pass
                else:
                    audio_in.unlink(missing_ok=True)
                stats["skipped"] += 1
                continue

            proc_path = PROCESSING / audio_in.name
            try:
                shutil.move(str(audio_in), str(proc_path))
            except Exception as e:
                log(f"[error] move to processing failed: {audio_in.name}: {e}")
                stats["error"] += 1
                continue

            try:
                log(f"[transcribe] {audio_in.name}")
                t_decode = time.time()
                audio = load_audio_16k_mono(proc_path)
                duration = len(audio) / 16000.0
                log(f"[transcribe]   loaded {format_ts(duration)} audio in {time.time()-t_decode:.1f}s")

                t_infer = time.time()
                result = pipe.generate(
                    audio,
                    language=f"<|{args.language}|>",
                    task="transcribe",
                    return_timestamps=True,
                )
                elapsed = time.time() - t_infer
                speed = duration / elapsed if elapsed > 0 else 0

                # Extract chunks with timestamps
                lines = []
                chunks = getattr(result, "chunks", None) or []
                for ch in chunks:
                    start_ts = getattr(ch, "start_ts", 0.0)
                    text = getattr(ch, "text", "").strip()
                    if text:
                        lines.append(f"[{format_ts(start_ts)}] {text}")
                transcript = "\n".join(lines) if lines else str(result).strip()
                chars = len(transcript)

                transcript_path.write_text(transcript, encoding="utf-8")
                meta = {
                    "filename": audio_in.name,
                    "duration_sec": round(duration, 2),
                    "duration_human": format_ts(duration),
                    "detected_language": args.language,
                    "transcribed_at_utc": datetime.utcnow().isoformat() + "Z",
                    "model": args.model_id,
                    "device": args.device,
                    "elapsed_sec": round(elapsed, 1),
                    "speed_x_realtime": round(speed, 2),
                    "characters": chars,
                    "segments": len(lines),
                }
                (DONE / f"{audio_in.stem}-meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                shutil.move(str(proc_path), str(DONE / audio_in.name))
                log(f"[ok] {audio_in.name} ({chars} chars, {speed:.1f}x realtime, "
                    f"elapsed {format_ts(elapsed)})")
                stats["done"] += 1
            except Exception as e:
                # rollback to inbox so retry can pick it up
                try:
                    shutil.move(str(proc_path), str(INBOX / audio_in.name))
                except Exception:
                    pass
                log(f"[error] {audio_in.name}: {type(e).__name__}: {e}")
                stats["error"] += 1

        if args.mode == "once" or _should_stop:
            break
        time.sleep(args.interval)
        files = sorted(p for p in INBOX.iterdir()
                       if p.is_file() and p.suffix.lower() in AUDIO_EXTS)

    log(f"[once] done. {stats}")
    log_file.close()


if __name__ == "__main__":
    main()
