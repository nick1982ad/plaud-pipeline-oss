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

Если у вас есть NVIDIA GPU. **Torch не нужен** — ctranslate2 работает с CUDA
напрямую, нужны только две библиотеки NVIDIA:

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

В `audio_transcriber/config.json`:
```json
{
  "device": "cuda",
  "compute_type": "float16"
}
```

Либо `"device": "auto"` — `transcribe.py` спросит у ctranslate2, есть ли GPU.

Требования — те же, что у самого `ctranslate2` 4.x: **CUDA 12 и cuDNN 9**, то есть
драйвер NVIDIA с поддержкой CUDA 12. Больше ничего подбирать не нужно: два пакета
выше и есть готовый CUDA-рантайм, они не зависят ни от модели видеокарты, ни от
установленной в системе CUDA Toolkit (его вообще может не быть).

> **Почему не `torch --index-url .../cu124`.** Так советовала прошлая версия этого
> раздела. Ставить torch ради CUDA — лишние ~2.5 ГБ и, главное, привязка к
> конкретной сборке: `cu124` не содержит ядер для новых архитектур, `cu118` — для
> тех, что появились после неё. Каждой видеокарте свой `--index-url`, и это
> приходится знать заранее. `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` такой
> привязки не имеют. Сам torch для распознавания не используется — `ctranslate2`
> работает с CUDA напрямую.

### Как проверить, что GPU действительно задействован

В логе `transcribe.py` при загрузке модели должно быть `on 'cuda'`. Если написано
`on 'cpu'` или есть строка `! CUDA недоступна` — GPU не подхватился, и скрипт
честно доработает на CPU, а не упадёт.

Частые причины:

| Симптом в логе | Причина |
|---|---|
| `Library cublas64_12.dll is not found` | не установлены пакеты `nvidia-*`; на Windows см. ниже |
| `no kernel image is available for execution on the device` | видеокарта старее, чем поддерживают готовые колёса `ctranslate2` |
| `CUBLAS_STATUS_NOT_SUPPORTED` | `compute_type: "int8"` на GPU — поставьте `float16` |
| просто `on 'cpu'` | драйвер не даёт CUDA 12, либо GPU не найден |

### Подводный камень на Windows

DLL из pip-пакетов NVIDIA лежат в `site-packages\nvidia\<lib>\bin`, куда Windows
не заглядывает. Причём `os.add_dll_directory()` не спасает: `ctranslate2` грузит
cuBLAS ленивым `LoadLibrary` уже во время инференса, а тот смотрит в `PATH`.
Симптом коварный — модель загружается успешно, а падает на первом сегменте.
Этим занимается `register_cuda_dll_dirs()` в `transcribe.py`, отдельных действий
не требуется.

На Linux аналогичную роль играет `LD_LIBRARY_PATH` — см. README faster-whisper.

### Замеры

Модель `medium`, аудио 193 с, RTX 5070 Ti против Ryzen 7 8700F.
Цифры сильно зависят от железа, это порядок величины, а не обещание:

| Устройство | compute_type | время | × CPU |
|---|---|---|---|
| CPU | int8 (`cpu_threads: 8`) | 79.5 с | 1× |
| GPU | float16 | 7.3 с | **10.9×** |
| GPU | bfloat16 | 6.9 с | 11.5× |
| GPU | int8_float16 | 8.8 с | 9.0× |

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
