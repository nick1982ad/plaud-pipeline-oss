# Security Policy

## ⚠ Чего этот репозиторий категорически НЕ должен содержать

| Тип данных | Где может встретиться | Защита |
|---|---|---|
| **Bearer-токен Plaud (JWT)** | `bulk_export/config*.json` | `.gitignore` блокирует `config*.json`, в репо только `config*.example.json` с плейсхолдером |
| **Anthropic API ключ** | `.env`, `user-config.json` | `.gitignore` блокирует `.env` и `user-config.json` |
| **Аудио/видеозаписи** | корневые папки | `.gitignore` блокирует `*.mp3 *.wav *.mov *.mp4 ...` |
| **Транскрипты/саммари** | `*-transcript.txt`, `*-Summary.txt`, `*-summary.md` | `.gitignore` блокирует все эти суффиксы |
| **Email / пароль Plaud** | те же конфиги | заблокировано вместе с `config*.json` |

## Перед публикацией всегда

```bash
# Найти всё, что выглядит как токен или ключ
git grep -E "sk-ant-api03-[A-Za-z0-9_-]{40,}|bearer eyJ[A-Za-z0-9_=.-]{40,}|eyJhbGciOi[A-Za-z0-9_=.-]{40,}"
```

Если что-то нашлось — **СТОП**, откатить коммит, ротировать секрет.

## Если случайно закоммитили секрет

1. **Считайте секрет скомпрометированным** — git history публичен.
2. **Ротируйте сразу**:
   - Plaud-токен: разлогиньтесь в web.plaud.ai и заново войдите → получите новый `tokenstr`.
   - Anthropic API: https://console.anthropic.com/settings/keys → revoke старого ключа и создать новый.
3. Удалите файл из истории git:
   ```bash
   git filter-repo --invert-paths --path <secret_file>
   git push --force-with-lease
   ```
4. Откройте issue с описанием — это поможет проекту сделать .gitignore жёстче.

## Reporting a Vulnerability

Найдена уязвимость в коде пайплайна, скачке от Plaud, обработке транскриптов, или утечка ключей в логах?

- Откройте **GitHub Security Advisory** на этом репозитории, или
- Напишите maintainer'ам в приватный issue с тегом `security`.

Не публикуйте детали в публичных issues / PR до фикса.
