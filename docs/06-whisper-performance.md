# Производительность Whisper — выбор модели и backend'а

## Сравнение backend'ов

| Engine | Hardware | Скорость на small | Скорость на medium | Ставится | Где использовать |
|---|---|---|---|---|---|
| **faster-whisper CPU** | i7/Ryzen 7 | 1-3× realtime | 0.3-1× | `pip install faster-whisper imageio-ffmpeg` | Базовый default. Работает везде. |
| **faster-whisper + CUDA** | RTX 3060+ | 15-30× | 8-15× | + `pip install torch --index-url https://download.pytorch.org/whl/cu124` | NVIDIA GPU. Самая быстрая. |
| **OpenVINO** | Intel Arc 140T iGPU | 5-10× | 3-7× | `pip install openvino-genai librosa` | Без дискретной GPU, на современных Intel Core. |

Реальные замеры из практики:

| Файл | CPU small | OpenVINO Arc 140T | NVIDIA RTX 5060 Ti |
|---|---|---|---|
| 30 сек шортс | 5-10 сек | 1-2 сек | < 1 сек |
| 30 мин встреча | 8-15 мин | 4-6 мин | 1-2 мин |
| 1.5 ч лекция | 30-60 мин | 12-15 мин | 3-5 мин |

## Выбор модели

| Модель | Размер | Качество русского | Когда брать |
|---|---|---|---|
| `tiny` | 75 MB | посредственное | Только для теста и сверки timestamps |
| `base` | 150 MB | удовлетворительное | Когда нужна максимальная скорость |
| `small` | 500 MB | хорошее ✓ | **Default**. Подходит для большинства случаев. |
| `medium` | 1.5 GB | очень хорошее | Когда small путает термины |
| `large-v3` | 3 GB | топ | Когда нужна максимальная точность |

Меняется в `audio_transcriber/config.json` → `"model": "small"`.

## Параметры качества

```json
{
  "model": "small",
  "device": "auto",
  "compute_type": "auto",   // int8 на CPU, float16 на GPU
  "language": "ru",
  "beam_size": 5,
  "vad_filter": true
}
```

- **`beam_size`** — 1 быстрее на 30%, но качество чуть хуже. 5 — баланс. 10 для топа.
- **`vad_filter: true`** — пропускать паузы (ускоряет, иногда ловит лишний шум).

## Когда использовать OpenVINO

Современные Intel Core (12-го поколения и новее) имеют интегрированный Intel Arc iGPU. Это **бесплатный (для вас) ускоритель**, который сильно опережает CPU без дополнительного железа.

```bash
pip install openvino-genai librosa huggingface_hub
python bulk_export/process_lectures.py --openvino --source "..."
```

Первый запуск скачает `OpenVINO/whisper-medium-ov` модель (~600 MB, один раз).

## Когда нужен CUDA

Если у вас есть NVIDIA GPU:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install nvidia-cudnn-cu12
```

В `audio_transcriber/config.json`:
```json
{
  "model": "medium",
  "device": "cuda",
  "compute_type": "float16"
}
```

`transcribe.py` (faster-whisper) автоматически использует CUDA.

## Сравнение качества (subjective)

| Backend | Качество распознавания русского |
|---|---|
| faster-whisper small int8 CPU | Хорошее |
| faster-whisper small float16 GPU | Чуть лучше CPU |
| OpenVINO small | Сравнимо с faster-whisper small |
| OpenVINO medium | Заметно лучше small |
| faster-whisper large-v3 CUDA | Лучшее, но в 2-3× медленнее medium |

## Длинные аудио (>1 часа)

Whisper хорошо работает на любых длинах. Скрипт автоматически бьёт на сегменты по VAD.

Расход RAM: medium ≤ 3 GB, large-v3 ≤ 6 GB. Если падает по OOM — уменьшите модель или переключитесь на `compute_type: "int8"`.

## Если транскрипт качества «не очень»

1. Поднимите модель `small → medium → large-v3`.
2. Убедитесь что `"language": "ru"` явно указан.
3. Проверьте качество аудио (тихая запись → плохая расшифровка). Можно усилить через ffmpeg:
   ```bash
   ffmpeg -i quiet.mp3 -af "volume=2.0,highpass=f=80,lowpass=f=3000" loud.mp3
   ```

## Когда вообще не нужен Whisper

Если у вас уже есть `<stem>-transcript.txt` рядом с аудио (от Plaud, Otter, ручной правки) — Whisper-шаг пропускается, transcript подхватывается как есть.
