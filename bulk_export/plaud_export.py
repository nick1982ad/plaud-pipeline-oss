#!/usr/bin/env python3
"""
plaud_export.py - bulk export транскриптов и summary из Plaud Web (web.plaud.ai).

Логика:
  1. Берёт bearer token из config.json (или логинится по email+password).
  2. GET /file/simple/web - список всех записей.
  3. GET /filetag/list - папки/теги (для раскладки по подпапкам).
  4. Для каждой записи: GET /file/detail/{id}, вытаскивает transcript и summary
     из множества возможных полей (или подгружает по data_link с CDN).
  5. Сохраняет в output/<folder>/<filename>-transcript.txt и -Summary.txt.
  6. Skip-логика: если файл уже есть в output/ или в existing_dir - не качает.
"""
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "_logs"


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="Plaud bulk export")
    p.add_argument("--config", default=str(ROOT / "config.json"),
                   help="Path to config.json (default: ./config.json)")
    return p.parse_args()

# Force UTF-8 on stdout/stderr so Russian/Unicode names don't crash on Windows cp1251.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

US_BASE = "https://api.plaud.ai"
EU_BASE = "https://api-euc1.plaud.ai"
USER_AGENT = "Mozilla/5.0 (compatible; plaud-bulk-export/1.0)"

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def sanitize(name, max_len=180):
    s = INVALID_CHARS.sub("_", str(name)).strip().rstrip(". ")
    return (s[:max_len] or "_unnamed").rstrip(". ")


def first_nonempty(values):
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def is_dict(v):
    return isinstance(v, dict)


class Logger:
    def __init__(self, path):
        self.f = path.open("w", encoding="utf-8")

    def __call__(self, *args):
        line = " ".join(str(a) for a in args)
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


class PlaudClient:
    def __init__(self, base_url, token=None):
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").strip()
        if self.token.lower().startswith("bearer "):
            self.token = self.token[7:].strip()

    def _request(self, path, method="GET", body=None, content_type=None,
                 binary=False, allow_region_redirect=True):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                err_body = ""
            raise RuntimeError(f"HTTP {e.code} on {method} {url}: {err_body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error on {url}: {e}")

        if binary:
            return raw

        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace")

        if (allow_region_redirect and is_dict(payload) and payload.get("status") == -302):
            new_api = ((payload.get("data") or {}).get("domains") or {}).get("api")
            if new_api:
                self.base_url = new_api.rstrip("/")
                return self._request(path, method, body, content_type, binary, False)

        return payload

    def login(self, email, password):
        body = urllib.parse.urlencode({"username": email, "password": password}).encode()
        resp = self._request("/auth/access-token", "POST", body=body,
                             content_type="application/x-www-form-urlencoded")
        if not is_dict(resp) or resp.get("status") not in (0, 200) or not resp.get("access_token"):
            raise RuntimeError(f"Login failed: status={resp.get('status') if is_dict(resp) else resp}, "
                               f"msg={resp.get('msg') if is_dict(resp) else ''}")
        self.token = resp["access_token"]
        return self.token

    def list_files(self):
        resp = self._request("/file/simple/web")
        if not is_dict(resp):
            return []
        files = resp.get("data_file_list") or resp.get("data") or resp.get("payload") or []
        return [f for f in files if is_dict(f) and not f.get("is_trash")]

    def get_detail(self, file_id):
        resp = self._request(f"/file/detail/{urllib.parse.quote(str(file_id))}")
        if is_dict(resp) and is_dict(resp.get("data")):
            return resp["data"]
        return resp if is_dict(resp) else {}

    def list_filetags(self):
        for path in ("/filetag/", "/filetag/simple", "/filetag/list",
                     "/file-tag/simple", "/file-tag/list", "/filetag/all"):
            try:
                resp = self._request(path)
                if not is_dict(resp):
                    continue
                if resp.get("status") not in (None, 0, 200):
                    continue
                lst = (resp.get("data_filetag_list") or resp.get("data_file_tag_list")
                       or resp.get("data") or resp.get("payload") or [])
                if isinstance(lst, list) and lst:
                    out = {}
                    for t in lst:
                        if not is_dict(t):
                            continue
                        tid = t.get("id") or t.get("filetag_id") or t.get("file_tag_id")
                        nm = t.get("name") or t.get("filetag_name") or t.get("title")
                        if tid and nm:
                            out[str(tid)] = str(nm)
                    if out:
                        return out
            except Exception:
                continue
        return {}

    def fetch_link(self, url):
        """GET a presigned S3/CDN URL. Don't send Authorization (breaks S3 signature).
        Auto-decompress gzip. Returns parsed JSON when possible, else raw text, else ''."""
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method="GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                ce = (resp.headers.get("Content-Encoding") or "").lower()
        except Exception:
            return ""

        if not raw:
            return ""
        # Auto-detect gzip by magic bytes (S3 stores .json.gz; Content-Encoding may be empty)
        if raw[:2] == b"\x1f\x8b" or "gzip" in ce:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass

        text = raw.decode("utf-8", errors="replace")
        # Try JSON first; many Plaud content blobs are JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    def get_mp3_url(self, file_id):
        """Returns presigned MP3 URL from /file/temp-url/{id}?is_opus=false, or None."""
        try:
            resp = self._request(
                f"/file/temp-url/{urllib.parse.quote(str(file_id))}?is_opus=false")
        except Exception:
            return None
        if isinstance(resp, dict):
            for key in ("url", "temp_url"):
                v = resp.get(key)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            data = resp.get("data")
            if isinstance(data, str) and data.startswith("http"):
                return data
            if isinstance(data, dict):
                for key in ("url", "temp_url"):
                    v = data.get(key)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
        return None

    def download_binary(self, url):
        """GET a presigned URL without auth headers; return raw bytes or b''."""
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method="GET")
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read()
        except Exception:
            return b""


# -------- transcript / summary extraction (Plaud detail JSON has many shapes) --------

def _maybe_parse_json(s):
    if not isinstance(s, str):
        return s
    s = s.strip()
    if not s or s[0] not in "[{\"":
        return s
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return s


def _stringify_paragraphs(arr):
    lines = []
    for entry in arr:
        if not is_dict(entry):
            if isinstance(entry, str) and entry.strip():
                lines.append(entry.strip())
            continue
        speaker = first_nonempty([entry.get("speaker_name"), entry.get("speaker"),
                                  entry.get("name"), entry.get("role")]) or "Speaker"
        text = first_nonempty([entry.get("text"), entry.get("content"),
                               entry.get("value"), entry.get("transcript")])
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _content_to_text(content):
    """Universal: dict/list/str -> plain text."""
    parsed = _maybe_parse_json(content) if isinstance(content, str) else content
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, list):
        return _stringify_paragraphs(parsed)
    if is_dict(parsed):
        for key in ("full_text", "text", "ai_content", "content", "summary",
                    "abstract", "value"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for key in ("paragraphs", "sentences", "lines", "items"):
            v = parsed.get(key)
            if isinstance(v, list) and v:
                txt = _stringify_paragraphs(v)
                if txt:
                    return txt
    return ""


def _resolve_item(item, fetch_link):
    """Get text payload of a content_list / pre_download_content_list item.
    Prefers inline data_content; falls back to fetching data_link from S3."""
    if not is_dict(item):
        return ""
    inline = first_nonempty([item.get("data_content"), item.get("content"),
                             item.get("text"), item.get("value")])
    if inline:
        txt = _content_to_text(inline)
        if txt:
            return txt
    link = first_nonempty([item.get("data_link"), item.get("link"), item.get("url")])
    if link:
        raw = fetch_link(link)  # may return dict/list/str (already JSON-parsed)
        if raw:
            return _content_to_text(raw)
    return ""


def _data_type(item):
    return first_nonempty([item.get("data_type"), item.get("type"),
                           item.get("label"), item.get("name")]).lower() if is_dict(item) else ""


def _data_id(item):
    return first_nonempty([item.get("data_id")]).lower() if is_dict(item) else ""


def _all_content_items(detail):
    """Combine pre_download_content_list (inline) + content_list (S3 links)."""
    out = []
    for key in ("pre_download_content_list", "content_list"):
        v = detail.get(key)
        if isinstance(v, list):
            out.extend(v)
    return out


def extract_transcript(detail, fetch_link):
    """Pulls transcript text. Order of preference:
       1. detail.transcript_text / detail.full_text (rare)
       2. detail.trans_result.full_text / paragraphs (older API shape)
       3. content_list item with data_type='transaction_polish' (cleaner output)
       4. content_list item with data_type='transaction' (raw)
       5. inline data_content matching trans:* / transaction*
    """
    direct = first_nonempty([detail.get("transcript_text"), detail.get("full_text")])
    if direct:
        return direct

    tr = detail.get("trans_result")
    if is_dict(tr):
        ft = tr.get("full_text")
        if isinstance(ft, str) and ft.strip():
            return ft.strip()
        for fld in ("paragraphs", "sentences", "lines"):
            arr = tr.get(fld)
            if isinstance(arr, list) and arr:
                txt = _stringify_paragraphs(arr)
                if txt:
                    return txt

    arr = detail.get("transcript")
    if isinstance(arr, list) and arr:
        txt = _stringify_paragraphs(arr)
        if txt:
            return txt

    items = _all_content_items(detail)

    # Pass 1: prefer transaction_polish (human-edited)
    for it in items:
        if _data_type(it) == "transaction_polish":
            txt = _resolve_item(it, fetch_link)
            if txt:
                return txt

    # Pass 2: raw transaction
    for it in items:
        if _data_type(it) == "transaction":
            txt = _resolve_item(it, fetch_link)
            if txt:
                return txt

    # Pass 3: anything that looks transcript-like
    for it in items:
        dt = _data_type(it); did = _data_id(it)
        if "trans" in dt or did.startswith("trans"):
            txt = _resolve_item(it, fetch_link)
            if txt:
                return txt

    return ""


def extract_summary(detail, fetch_link):
    """Pulls summary text. Plaud's auto_sum payload has shape {ai_content: '<markdown>'}."""
    direct_candidates = [detail.get("summary")]
    for k in ("ai_content", "ai_notes"):
        sub = detail.get(k)
        if is_dict(sub):
            direct_candidates += [sub.get("summary"), sub.get("abstract"),
                                  sub.get("ai_content"), sub.get("content")]
    direct = first_nonempty(direct_candidates)
    if direct:
        return direct

    items = _all_content_items(detail)

    # Pass 1: auto_sum_note (default Plaud meeting summary)
    for it in items:
        if _data_type(it) == "auto_sum_note":
            txt = _resolve_item(it, fetch_link)
            if txt:
                return txt

    # Pass 2: any auto_sum* / *summary* / abstract
    for it in items:
        dt = _data_type(it); did = _data_id(it)
        if (did.startswith("auto_sum") or did.startswith("summary")
                or "summary" in dt or "abstract" in dt or "auto_sum" in dt):
            txt = _resolve_item(it, fetch_link)
            if txt:
                return txt

    return ""


# -------- skip set --------

_skip_index_cache = {}


def build_skip_index(search_dirs):
    """Returns list of normalized stems of *-transcript.txt and *-Summary.txt
    found in given dirs. Used for substring matching."""
    key = tuple(str(d) for d in search_dirs)
    if key in _skip_index_cache:
        return _skip_index_cache[key]
    stems = []
    for d in search_dirs:
        if not d.exists():
            continue
        for p in list(d.rglob("*-transcript.txt")) + list(d.rglob("*-Summary.txt")):
            stem = p.stem
            for suffix in ("-transcript", "-Summary"):
                if stem.endswith(suffix):
                    stem = stem[:-len(suffix)]
                    break
            stems.append(stem.lower().strip())
    _skip_index_cache[key] = stems
    return stems


def already_exists(plaud_filename, search_dirs, min_len=8):
    """True iff a sanitized form of plaud_filename matches an existing file.
    Default: exact match. Substring matching is too risky for series like
    'X', 'X 2', 'X 3' — it caused false-positive skips."""
    needle = sanitize(plaud_filename or "").lower().strip()
    if len(needle) < min_len:
        return False
    return needle in set(build_skip_index(search_dirs))


# -------- main --------

def main():
    args = parse_args()
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        sys.exit(f"config not found at {config_path}\n"
                 f"Create it from config.example.json (see README.md).")

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    output_dir = Path(cfg["output_dir"])
    existing_dir = Path(cfg["existing_dir"]) if cfg.get("existing_dir") else None
    output_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_tag = config_path.stem  # e.g. "config" or "config_2"
    log_path = LOG_DIR / f"run-{log_tag}-{datetime.now():%Y-%m-%d-%H%M%S}.log"
    log = Logger(log_path)
    log(f"=== Plaud bulk export started {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    log(f"output_dir   = {output_dir}")
    log(f"existing_dir = {existing_dir}")
    log(f"formats      = {cfg.get('formats', ['transcript', 'summary'])}")

    region = (cfg.get("region") or "eu").lower()
    base = EU_BASE if region == "eu" else US_BASE
    client = PlaudClient(base, token=cfg.get("token"))

    if not client.token:
        email = cfg.get("email")
        password = cfg.get("password")
        if not email or not password:
            sys.exit("Need either 'token' or ('email' AND 'password') in config.json")
        log(f"No token in config; logging in as {email}...")
        client.login(email, password)
        log("Login OK")
    else:
        log(f"Using token from config (length={len(client.token)})")

    log(f"GET /file/simple/web on {client.base_url} ...")
    files = client.list_files()
    log(f"Got {len(files)} files (excluding trash). Effective base: {client.base_url}")
    if not files:
        log("Nothing to download. Exiting.")
        log.close()
        return

    log("Fetching folder/tag mapping...")
    tag_map = client.list_filetags()
    if tag_map:
        log(f"Got {len(tag_map)} tags: {sorted(tag_map.values())}")
    else:
        log("No tag mapping endpoint worked; everything will go to _Unfiled")

    search_dirs = [output_dir]
    if existing_dir and existing_dir.exists():
        search_dirs.append(existing_dir)
    log(f"Skip-set search dirs: {[str(d) for d in search_dirs]}")
    log(f"Skip-set: indexed {len(build_skip_index(search_dirs))} existing transcripts")

    formats = set(cfg.get("formats", ["transcript", "summary"]))
    sleep_sec = float(cfg.get("sleep_between_files", 0.3))

    total = len(files)
    skipped = downloaded = errors = 0

    mp3_mode = cfg.get("mp3_mode", "off")  # off | untranscribed | all
    mp3_downloaded = mp3_skipped = mp3_errors = 0
    no_content = 0
    for i, f in enumerate(files, 1):
        fid = f.get("id") or f.get("file_id")
        fname = (f.get("filename") or f.get("file_name")
                 or f.get("fullname") or str(fid))
        prefix = f"[{i}/{total}]"
        is_trans = bool(f.get("is_trans"))
        is_summary = bool(f.get("is_summary"))

        # Skip-set check applies to text outputs; mp3 has its own check below.
        text_already = already_exists(fname, search_dirs)

        want_t = "transcript" in formats and is_trans and not text_already
        want_s = "summary" in formats and is_summary and not text_already
        # mp3 logic: "untranscribed" = both flags False; "all" = every file
        want_mp3 = False
        if mp3_mode == "all":
            want_mp3 = True
        elif mp3_mode == "untranscribed":
            want_mp3 = not is_trans and not is_summary

        if text_already and not want_mp3:
            skipped += 1
            log(f"{prefix} SKIP existing: {fname}")
            continue

        if not (want_t or want_s or want_mp3):
            no_content += 1
            log(f"{prefix} -- no transcript/summary in Plaud: {fname}")
            continue

        # determine target folder by filetag
        tag_ids = (f.get("filetag_id_list") or f.get("file_tag_id_list")
                   or f.get("filetag_ids") or [])
        folder_name = "_Unfiled"
        for tid in tag_ids:
            if str(tid) in tag_map:
                folder_name = sanitize(tag_map[str(tid)])
                break
        target_dir = output_dir / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)

        log(f"{prefix} GET {fname} -> {folder_name}/")
        # mp3 download path doesn't require /file/detail
        if want_mp3:
            mp3_out = target_dir / f"{sanitize(fname)}.mp3"
            if mp3_out.exists() and mp3_out.stat().st_size > 0:
                log(f"{prefix}   = mp3 already on disk, skip ({mp3_out.name})")
                mp3_skipped += 1
            else:
                url = client.get_mp3_url(fid)
                if not url:
                    log(f"{prefix}   ! mp3 url not available")
                    mp3_errors += 1
                else:
                    data = client.download_binary(url)
                    if not data:
                        log(f"{prefix}   ! mp3 download failed (empty)")
                        mp3_errors += 1
                    else:
                        mp3_out.write_bytes(data)
                        size_kb = len(data) / 1024
                        log(f"{prefix}   + mp3 ({size_kb:,.0f} KB) -> {mp3_out.name}")
                        mp3_downloaded += 1

        if not (want_t or want_s):
            time.sleep(sleep_sec)
            continue

        try:
            detail = client.get_detail(fid)
        except Exception as e:
            errors += 1
            log(f"{prefix}   ! detail fetch failed: {e}")
            continue

        wrote_any = False
        if want_t:
            try:
                txt = extract_transcript(detail, client.fetch_link)
                if txt:
                    out = target_dir / f"{sanitize(fname)}-transcript.txt"
                    out.write_text(txt, encoding="utf-8")
                    wrote_any = True
                    log(f"{prefix}   + transcript ({len(txt):,} chars) -> {out.name}")
                else:
                    log(f"{prefix}   - no transcript content (expected, is_trans=True)")
            except Exception as e:
                log(f"{prefix}   ! transcript error: {e}")

        if want_s:
            try:
                txt = extract_summary(detail, client.fetch_link)
                if txt:
                    out = target_dir / f"{sanitize(fname)}-Summary.txt"
                    out.write_text(txt, encoding="utf-8")
                    wrote_any = True
                    log(f"{prefix}   + summary    ({len(txt):,} chars) -> {out.name}")
                else:
                    log(f"{prefix}   - no summary content (expected, is_summary=True)")
            except Exception as e:
                log(f"{prefix}   ! summary error: {e}")

        if wrote_any:
            downloaded += 1
            # invalidate skip cache so re-runs see the new file
            _skip_index_cache.clear()
        else:
            errors += 1

        time.sleep(sleep_sec)

    log("")
    log(f"=== Done: total={total}, downloaded={downloaded}, "
        f"skipped={skipped}, no_content={no_content}, errors={errors} "
        f"| mp3_mode={mp3_mode}, mp3_downloaded={mp3_downloaded}, "
        f"mp3_skipped={mp3_skipped}, mp3_errors={mp3_errors} ===")
    log(f"Log saved: {log_path}")
    log.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Interrupted by user.")
        sys.exit(130)
