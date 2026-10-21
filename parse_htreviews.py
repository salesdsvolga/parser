#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — FINAL PARSER (с защитой от бана и умными паузами).
"""

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

sys.stdout.reconfigure(line_buffering=True)

BASE = "https://htreviews.org"
STATE_FILE = Path("state.json")
JSONL_FILE = Path("data.jsonl")
OUTPUT_FILE = Path("htreviews_full.json")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://htreviews.org/",
    "X-Requested-With": "XMLHttpRequest"
}

_session = requests.Session()
_session.headers.update(HEADERS)
_stop_requested = False

def _handle_signal(signum, frame):
    global _stop_requested
    if _stop_requested:
        print("\n⛔ Force exit.", flush=True)
        sys.exit(130)
    _stop_requested = True
    print("\n⏸ Graceful stop — saving…", flush=True)

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def fetch(url, timeout=30, referer=None):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = _session.get(url, timeout=timeout, headers=headers)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""

def soup_of(url, referer=None):
    return BeautifulSoup(fetch(url, referer=referer), "lxml")

def normalize(url):
    return urljoin(BASE + "/", url).split("#")[0].split("?")[0].rstrip("/")

def path_parts(url):
    return urlparse(normalize(url)).path.strip("/").split("/")

def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("done_lines", [])
            return s
        except Exception:
            pass
    return {"done_lines": [], "started_at": now_iso()}

def save_state(state):
    state["updated_at"] = now_iso()
    atomic_write_json(STATE_FILE, state)

def load_existing_urls():
    urls = set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    if obj.get("flavor_url"):
                        urls.add(obj["flavor_url"])
                except Exception:
                    pass
    return urls

def append_item(item, existing_urls):
    fu = item.get("flavor_url")
    if not fu or fu in existing_urls:
        return False
    with open(JSONL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()
    existing_urls.add(fu)
    return True

def consolidate():
    items, seen = [], set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    fu = obj.get("flavor_url")
                    if fu and fu not in seen:
                        seen.add(fu)
                        items.append(obj)
                except Exception:
                    pass
    items.sort(key=lambda x: (
        (x.get("brand") or "").lower(),
        (x.get("line") or "").lower(),
        (x.get("flavor") or "").lower(),
    ))
    atomic_write_json(OUTPUT_FILE, items)
    return items


# ---------- API С ЗАЩИТОЙ ОТ БАНА ----------

def _post_api(action, data, referer=None, retries=5):
    url = BASE + "/postData"
    headers = {"Referer": referer or BASE + "/"}
    
    for attempt in range(retries):
        if _stop_requested:
            return None
        try:
            r = _session.post(url, json={"action": action, "data": data}, headers=headers, timeout=30)
            
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    print(f"      [!] API вернул не JSON: {r.text[:100]}", flush=True)
                    return None
            
            # Если сервер заблокировал или перегружен
            print(f"      [!] Сервер занят (HTTP {r.status_code}). Ждем 30 секунд...", flush=True)
            time.sleep(30)
            
        except Exception as e:
            print(f"      [!] Ошибка сети: {e}. Ждем 30 секунд...", flush=True)
            time.sleep(30)
            
    return None


# ---------- Главные функции сбора ----------

def get_all_lines_via_api():
    print("▶ Загрузка ВСЕХ линеек через API (это займет пару минут)...", flush=True)
    lines_data = []
    seen_ids = set()
    
    for r_type in ["position", "others"]:
        offset = 0
        while not _stop_requested:
            # Передаем o строкой, как в вашем запросе из DevTools
            data = {"r": r_type, "s": "rating", "d": "desc", "o": str(offset)}
            chunk = _post_api("lines", data)
            
            # Обработка разных форматов ответа
            if isinstance(chunk, dict):
                chunk = chunk.get("data", chunk.get("lines", chunk.get("items", [])))
                
            if not isinstance(chunk, list) or len(chunk) == 0:
                break
                
            new_items = 0
            for it in chunk:
                line_id = str(it.get("id") or "")
                if line_id and line_id not in seen_ids:
                    seen_ids.add(line_id)
                    lines_data.append(it)
                    new_items += 1
                    
            if new_items == 0:
                break
                
            offset += len(chunk)
            print(f"    [{r_type}] +{new_items} линеек (всего {len(lines_data)})", flush=True)
            
            # Пауза 2 секунды, чтобы не злить Cloudflare
            time.sleep(2.0)
            
    print(f"  ✅ Загружено {len(lines_data)} уникальных линеек.", flush=True)
    return lines_data


def get_flavors_for_line(line_id, line_url):
    items = []
    offset = 0
    limit = 50
    seen_ids = set()
    
    while not _stop_requested:
        data = {
            "id": line_id,
            "limit": limit,
            "offset": offset,
            "sort": {"s": "position", "d": "asc"}
        }
        chunk = _post_api("objectByLine", data, referer=line_url)
        
        if not isinstance(chunk, list) or len(chunk) == 0:
            break
            
        new_items = 0
        for it in chunk:
            f_id = str(it.get("id") or "")
            if f_id and f_id not in seen_ids:
                seen_ids.add(f_id)
                items.append(it)
                new_items += 1
                
        if new_items == 0:
            break
            
        offset += len(chunk)
        
        # Пауза 1 секунда между запросами вкусов
        time.sleep(1.0)
        
    return items


def format_record(it):
    slug = (it.get("slug") or "").strip("/")
    parts = slug.split("/")
    if len(parts) != 3:
        return None
        
    brand_slug, line_slug, flavor_slug = parts
    
    tags = []
    for t in (it.get("tags") or []):
        if isinstance(t, dict) and t.get("name"):
            tags.append({"group": t.get("group"), "name": t.get("name")})
            
    return {
        "brand": it.get("brand") or brand_slug,
        "brand_url": f"{BASE}/tobaccos/{brand_slug}",
        "line": it.get("line") or line_slug,
        "line_url": f"{BASE}/tobaccos/{brand_slug}/{line_slug}",
        "flavor": it.get("name") or flavor_slug,
        "flavor_url": f"{BASE}/tobaccos/{slug}",
        "flavor_alt": it.get("alt_name"),
        "rating": it.get("rating"),
        "reviews_count": int(it["reviews"]) if str(it.get("reviews", "")).isdigit() else None,
        "strength": it.get("strength"),
        "status": it.get("status"),
        "tags": tags,
    }


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--consolidate-only", action="store_true")
    ap.add_argument("--max-brands", type=int, default=0)
    args = ap.parse_args()

    if args.consolidate_only:
        items = consolidate()
        print(f"Consolidated {len(items)} items -> {OUTPUT_FILE}", flush=True)
        return

    state = load_state()
    done_lines = set(state["done_lines"])
    existing_urls = load_existing_urls()
    
    print(f"▶ Старт. Уже собрано вкусов: {len(existing_urls)}", flush=True)

    all_lines = get_all_lines_via_api()
    
    if not all_lines:
        print("❌ Не удалось загрузить линейки. Сайт нас временно заблокировал. Попробуйте через 30 минут.", flush=True)
        return

    if args.max_brands > 0:
        print(f"⚠️ Ограничение: будет обработано только {args.max_brands} линеек.", flush=True)
        all_lines = all_lines[:args.max_brands]

    print("▶ Сбор вкусов по линейкам...", flush=True)
    total_new = 0
    
    for it in tqdm(all_lines, desc="Lines"):
        if _stop_requested:
            break
            
        slug = (it.get("slug") or "").strip("/")
        line_id = str(it.get("id") or "")
        line_url = normalize(f"{BASE}/tobaccos/{slug}")
        brand_name = it.get("brand") or "Unknown"
        line_name = it.get("line") or slug
        
        if not line_id or not slug:
            continue
            
        if line_id in done_lines:
            continue

        got = 0
        if line_id:
            flavors = get_flavors_for_line(line_id, line_url)
            for f_it in flavors:
                rec = format_record(f_it)
                if rec and append_item(rec, existing_urls):
                    got += 1
                    
        if got > 0:
            total_new += got
            print(f"    ✓ {brand_name} / {line_name}: +{got}", flush=True)
            
        done_lines.add(line_id)
        state["done_lines"] = list(done_lines)
        save_state(state)
        
        # Пауза 1.5 сек между линейками
        time.sleep(1.5)

    items = consolidate()
    print(f"\n✅ ГОТОВО! Всего вкусов: {len(items)}. Новых за этот прогон: {total_new}", flush=True)


if __name__ == "__main__":
    main()
