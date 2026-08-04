#!/usr/bin/env python3
"""
init.py — интерактивная настройка путей и backend'а при первом запуске.

Создаёт user-config.json в корне портативной папки. Все скрипты (process_local_audio.py,
process_new_plaud_batch.py) читают его как defaults для --source/--output/--backend и т.д.

Запуск:
    python init.py            # интерактивно, заполнит/обновит user-config.json
    python init.py --show     # показать текущий config
    python init.py --reset    # перезаписать заново
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "user-config.json"

DEFAULTS = {
    "audio_source":   r"C:\AudioRecordings",
    "output_dir":     r"C:\_Dictophone",
    "plaud_mirror":   r"C:\_Dictophone\plaud",
    "video_source":   r"C:\Lectures",
    "book_output":    r"C:\Book",
    "backend":        "auto",
    "whisper_model":  "small",
    "ollama_model":   "qwen2.5:7b-instruct",
    "ollama_url":     "http://localhost:11434",
    "anthropic_api_key": "",
}


def banner(text):
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def ask(prompt: str, default: str = "", validator=None) -> str:
    """Ask user for input with [default] hint. Returns stripped string."""
    suffix = f"\n  [{default}]: " if default else ": "
    while True:
        ans = input(prompt + suffix).strip()
        if not ans:
            ans = default
        if validator:
            ok, err = validator(ans)
            if not ok:
                print(f"  ✗ {err}")
                continue
        return ans


def ask_choice(prompt: str, options: list, default_key: str) -> str:
    """Show numbered choices. Each option = (key, label). Returns key."""
    print(prompt)
    keys = [k for k, _ in options]
    for i, (k, label) in enumerate(options, 1):
        marker = " ✔ default" if k == default_key else ""
        print(f"  {i}. {label}{marker}")
    while True:
        raw = input(f"  выбери 1-{len(options)} [{default_key}]: ").strip()
        if not raw:
            return default_key
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        if raw in keys:
            return raw
        print(f"  ✗ непонятно, введи число 1-{len(options)} или один из: {', '.join(keys)}")


def validate_dir(p: str, must_exist=False):
    if not p:
        return False, "пустой путь"
    try:
        path = Path(p).expanduser()
    except Exception as e:
        return False, f"невалидный путь: {e}"
    if must_exist and not path.is_dir():
        return False, f"папка не существует: {path}"
    return True, None


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return {**DEFAULTS, **data}
        except Exception:
            return dict(DEFAULTS)
    return dict(DEFAULTS)


def save_config(cfg: dict):
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n✓ Сохранено: {CONFIG_PATH}")


def show(cfg: dict):
    print()
    print("Текущий user-config.json:")
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def detect_backends():
    """Return list of available backends right now."""
    avail = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        avail.append("sdk")
    # check .env
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY=") and "PASTE_" not in line:
                if "sdk" not in avail: avail.append("sdk")
                break
    if shutil.which("claude.cmd") or shutil.which("claude"):
        avail.append("cli")
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1) as r:
            if r.status == 200: avail.append("ollama")
    except Exception:
        pass
    return avail


def interactive(cfg: dict) -> dict:
    banner("Plaud Pipeline — настройка путей")
    print("(нажми Enter чтобы оставить значение из [скобок])")
    print()

    # 1. Source folder (где лежит аудио)
    print("⚙ ПАПКИ\n")
    cfg["audio_source"] = ask(
        "1) Где будут лежать ИСХОДНЫЕ .wav/.mp3 файлы для обработки",
        cfg["audio_source"],
    )
    Path(cfg["audio_source"]).mkdir(parents=True, exist_ok=True)

    # 2. Output (куда складывать партии)
    cfg["output_dir"] = ask(
        "2) Куда складывать ГОТОВЫЕ партии (создаются папки 'Записи с ДД.ММ.ГГГГ по ...')",
        cfg["output_dir"],
    )
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)

    # 3. Plaud mirror (для plaud flow)
    cfg["plaud_mirror"] = ask(
        "3) Куда складывать RAW Plaud-транскрипты (зеркало, для бэкапа). Не критично если Plaud не используешь",
        cfg["plaud_mirror"],
    )
    Path(cfg["plaud_mirror"]).mkdir(parents=True, exist_ok=True)

    # 3a. Video source (для lectures)
    cfg["video_source"] = ask(
        "3a) Где будут лежать ВИДЕО-ЛЕКЦИИ для обработки в главы книги (опционально)",
        cfg["video_source"],
    )
    Path(cfg["video_source"]).mkdir(parents=True, exist_ok=True)

    # 3b. Book output
    cfg["book_output"] = ask(
        "3b) Куда складывать главы будущей книги (PDF + MD + transcript на каждую лекцию)",
        cfg["book_output"],
    )
    Path(cfg["book_output"]).mkdir(parents=True, exist_ok=True)

    # 4. Whisper model
    print()
    print("⚙ WHISPER (локальная транскрипция аудио → текст)")
    print()
    cfg["whisper_model"] = ask_choice(
        "4) Какую модель Whisper использовать?",
        [
            ("tiny",     "tiny     — самая быстрая, низкое качество, ~75 MB"),
            ("base",     "base     — быстрая, средне-низкое, ~150 MB"),
            ("small",    "small    — быстрая, хорошее качество, ~500 MB"),
            ("medium",   "medium   — медленная, отличное качество, ~1.5 GB"),
            ("large-v3", "large-v3 — самая медленная, максимум, ~3 GB"),
        ],
        cfg["whisper_model"],
    )

    # 5. LLM backend
    print()
    print("⚙ LLM (модель которая делает заголовок и summary)")
    available = detect_backends()
    if available:
        print(f"\n  Сейчас доступны: {', '.join(available)}")
    else:
        print("\n  ⚠ Ни один backend ещё не настроен — но это можно сделать позже.")
    print()

    cfg["backend"] = ask_choice(
        "5) Какой LLM-backend использовать?",
        [
            ("auto",   "auto   — автодетект (порядок: API key → claude CLI → Ollama)"),
            ("sdk",    "sdk    — Anthropic API key (быстрее всего, отдельный биллинг)"),
            ("cli",    "cli    — Claude Code subscription (через установленный claude CLI)"),
            ("ollama", "ollama — полностью локально на CPU/GPU (Ollama + qwen2.5 7B)"),
        ],
        cfg["backend"],
    )

    if cfg["backend"] in ("auto", "sdk"):
        print()
        print("API ключ Anthropic нужен только для backend=sdk.")
        print("Можно: 1) положить в .env (рекомендуется), 2) системная переменная, 3) вписать сюда.")
        cur_key_hint = "(уже задан)" if cfg.get("anthropic_api_key") else "пропустить — Enter"
        key = ask(
            f"6) ANTHROPIC_API_KEY (или Enter чтобы пропустить — {cur_key_hint})",
            cfg.get("anthropic_api_key", ""),
        )
        if key and not key.startswith("PASTE_"):
            cfg["anthropic_api_key"] = key
            # Также записываем .env для совместимости
            env_file = ROOT / ".env"
            env_file.write_text(f"ANTHROPIC_API_KEY={key}\n", encoding="utf-8")
            print(f"  ✓ Записано также в {env_file}")

    if cfg["backend"] in ("auto", "ollama"):
        print()
        cfg["ollama_model"] = ask(
            "7) Какую модель использовать в Ollama (для backend=ollama)",
            cfg["ollama_model"],
        )

    return cfg


def main():
    ap = argparse.ArgumentParser(description="Initialise/edit user-config.json")
    ap.add_argument("--show", action="store_true", help="Show current config and exit")
    ap.add_argument("--reset", action="store_true", help="Start from defaults (ignore existing)")
    args = ap.parse_args()

    cfg = dict(DEFAULTS) if args.reset else load_config()

    if args.show:
        show(cfg)
        return

    cfg = interactive(cfg)
    save_config(cfg)
    show(cfg)
    print()
    print("Дальше:")
    print("  • Локальная папка с .wav  →  bulk_export\\local-process.bat  (двойной клик)")
    print(f"                           →  python bulk_export\\process_local_audio.py")
    print("  • В Claude Code           →  /local")
    print()


if __name__ == "__main__":
    main()
