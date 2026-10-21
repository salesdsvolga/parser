#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — FINAL PARSER (brand, line, flavor).
Использует API /postData для сбора линеек и вкусов.
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

# Принудительно включаем моментальный вывод логов в GitHub Actions
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


# ---------- API ----------

def _post_api(action, data, referer=None):
    try:
        r = _session.post(
            BASE + "/postData",
            json={"action": action, "data": data},
            headers={"Referer": referer or BASE + "/"},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
        else:
            print(f"      [!] API {action} -> HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"      [!] API {action} -> Ошибка: {e}", flush=True)
    return None


def _find_line_id_in_html(soup):
    """Резервный метод поиска ID, если API вдруг его не отдал."""
    for el in soup.select(".object_wrapper[data-id], .tobacco_list_items[data-id]"):
        val = el.get("data-id")
        if val and str(val).isdigit():
            return str(val)
    return None


# ---------- Главные функции сбора ----------

def get_all_lines_via_api():
    """Выкачивает ВСЕ линейки напрямую из API."""
    print("▶ Загрузка ВСЕХ линеек через API...", flush=True)
    lines_data = []
    seen_ids = set()
    
    # Пробуем оба типа (position и others), чтобы наверняка забрать всё
    for r_type in ["position", "others"]:
        offset = 0
        while not _stop_requested:
            data = {"r": r_type, "s": "rating", "d": "desc", "o": offset}
            chunk = _post_api("lines", data)
            
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
            time.sleep(0.1)
            
    print(f"  ✅ Загружено {len(lines_data)} уникальных линеек.", flush=True)
    return lines_data


def get_flavors_for_line(line_id, line_url):
    """Выкачивает ВСЕ вкусы для конкретной линейки."""
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
        time.sleep(0.1)
        
    return items


def format_record(it):
    """Преобразует ответ API в наш чистый формат."""
    slug = (it.get("slug") or "").strip("/")
    parts = slug.split("/")
    if len(parts) != 3:
        return None
        
    brand_slug, line_slug, flavor_slug = parts
    
    # Обработка тегов
    tags = []
    for t in (it.get("tags") or []):
        if isinstance(t, dict) and t.get("name"):
            tags.append({
                "group": t.get("group"),
                "name": t.get("name")
            })
            
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
    # ВОЗВРАЩАЕМ АРГУМЕНТ, ЧТОБЫ WORKFLOW НЕ ПАДАЛ
    ap.add_argument("--max-brands", type=int, default=0, help="Ограничить число линеек (для теста)")
    args = ap.parse_args()

    if args.consolidate_only:
        items = consolidate()
        print(f"Consolidated {len(items)} items -> {OUTPUT_FILE}", flush=True)
        return

    state = load_state()
    done_lines = set(state["done_lines"])
    existing_urls = load_existing_urls()
    
    print(f"▶ Старт. Уже собрано вкусов: {len(existing_urls)}", flush=True)

    # 1. Получаем все линейки сайта
    all_lines = get_all_lines_via_api()
    
    if not all_lines:
        print("❌ Не удалось загрузить линейки. Проверьте API.", flush=True)
        return

    # Если передан --max-brands, ограничиваем количество линеек для теста
    if args.max_brands > 0:
        print(f"⚠️ Ограничение: будет обработано только {args.max_brands} линеек.", flush=True)
        all_lines = all_lines[:args.max_brands]

    # 2. Идем по каждой линейке
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

        # Если API не отдал ID, идем на страницу и ищем в HTML
        if not line_id:
            soup = soup_of(line_url)
            line_id = _find_line_id_in_html(soup)

        got = 0
        if line_id:
            flavors = get_flavors_for_line(line_id, line_url)
            for f_it in flavors:
                rec = format_record(f_it)
                if rec and append_item(rec, existing_urls):
                    got += 1
        else:
            # Резервный парсинг HTML (если ID вообще нигде нет)
            soup = soup_of(line_url)
            for a in soup.find_all("a", href=True):
                pp = path_parts(a["href"])
                if len(pp) == 4 and pp[0] == "tobaccos":
                    rec = {
                        "brand": brand_name, "brand_url": f"{BASE}/tobaccos/{pp[1]}",
                        "line": line_name, "line_url": line_url,
                        "flavor": a.get_text(strip=True), "flavor_url": normalize(a["href"]),
                        "flavor_alt": None, "tags": []
                    }
                    if append_item(rec, existing_urls):
                        got += 1

        if got > 0:
            total_new += got
            print(f"    ✓ {brand_name} / {line_name}: +{got}", flush=True)
            
        done_lines.add(line_id)
        state["done_lines"] = list(done_lines)
        save_state(state)
        time.sleep(0.1)

    items = consolidate()
    print(f"\n✅ ГОТОВО! Всего вкусов: {len(items)}. Новых за этот прогон: {total_new}", flush=True)


if __name__ == "__main__":
    main()
