#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — минимальный резюмируемый парсер (brand + flavor).

Многофазный сбор брендов:
  1. Базовые страницы /tobaccos/brands (position + others)
  2. AJAX-эндпоинты «Показать ещё» (/htmx/load/...)
  3. Перебор фильтров: страна × сортировка × тип
  4. Дополнительные страницы сайта (/, /tobaccos, /tobaccos/new, /hall-of-fame, …)

Файлы:
  state.json           — прогресс (done_brands), атомарная запись
  data.jsonl           — журнал пар brand/flavor (append-only, crash-safe)
  htreviews_full.json  — итоговый массив

Resume: просто запустите снова.
Сигналы: Ctrl+C один раз — graceful stop. Два раза — force exit.
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

BASE = "https://htreviews.org"
STATE_FILE = Path("state.json")
JSONL_FILE = Path("data.jsonl")
OUTPUT_FILE = Path("htreviews_full.json")
SAVE_EVERY = 50

# служебные сегменты, которые не являются брендами
RESERVED = {"brands", "lines", "new", "now"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://htreviews.org/",
}

_session = requests.Session()
_session.headers.update(HEADERS)
_stop_requested = False


def _handle_signal(signum, frame):
    global _stop_requested
    if _stop_requested:
        print("\n⛔ Force exit.")
        sys.exit(130)
    _stop_requested = True
    print("\n⏸ Graceful stop requested (Ctrl+C again to force). Saving…")


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url, retries=5, backoff=1.5, timeout=30):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"fetch failed {url}: {last}")


def soup_of(url):
    return BeautifulSoup(fetch(url), "lxml")


def text_of(node):
    if not node:
        return None
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() or None


def normalize(url):
    """Приводит любой href к абсолютному URL."""
    return urljoin(BASE + "/", url).split("#")[0].split("?")[0].rstrip("/")


def path_parts(url):
    """Возвращает сегменты пути после нормализации."""
    return urlparse(normalize(url)).path.strip("/").split("/")


# ---------- persistence ----------

def atomic_write_json(path: Path, data):
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
            s.setdefault("done_brands", [])
            return s
        except Exception:
            pass
    return {"done_brands": [], "started_at": now_iso()}


def append_jsonl(item):
    with open(JSONL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()


def consolidate():
    pairs, seen = [], set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (obj.get("brand"), obj.get("flavor"))
                if None in key or key in seen:
                    continue
                seen.add(key)
                pairs.append(obj)
    pairs.sort(key=lambda x: (x["brand"].lower(), x["flavor"].lower()))
    atomic_write_json(OUTPUT_FILE, pairs)
    return pairs


def save_state(state, done_brands):
    state["done_brands"] = sorted(done_brands)
    state["updated_at"] = now_iso()
    atomic_write_json(STATE_FILE, state)


# ---------- discovery ----------

def _add_brands_from_soup(soup, brands):
    """Из супа вытаскивает все ссылки на бренды и добавляет в dict."""
    added = 0
    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if (len(parts) == 2 and parts[0] == "tobaccos"
                and parts[1] and parts[1] not in RESERVED):
            full = normalize(a["href"])
            if full not in brands:
                brands[full] = {
                    "name": text_of(a) or parts[1],
                    "slug": parts[1],
                    "url": full,
                }
                added += 1
    return added


def _try_ajax_endpoints(brands):
    """Пробует несколько стандартных AJAX-URL для 'Показать ещё'."""
    candidates = [
        "/htmx/load/brands?offset=20",
        "/htmx/load/brands?offset=20&limit=100",
        "/htmx/load/brands?start=20",
        "/htmx/load/brands?page=2",
        "/htmx/load/brands?type=others&offset=20",
        "/htmx/load/more?type=brands&offset=20",
        "/htmx/load/more?entity=brands&offset=20",
        "/htmx/load/tops_shortlist?entity=brands&offset=20",
    ]
    for path in candidates:
        try:
            text = fetch(BASE + path, retries=2)
            soup = BeautifulSoup(text, "lxml")
            added = _add_brands_from_soup(soup, brands)
            print(f"    ajax {path}: +{added} (total {len(brands)})")
            if added > 5:
                return True
        except Exception:
            pass
    return False


def _sweep_filters(brands):
    """Перебирает фильтры страны × сортировки × тип, собирая бренды."""
    countries = ["", "13", "20", "29", "37", "41", "51", "64", "68", "69",
                 "70", "71", "80", "98", "100", "137", "142", "145", "151",
                 "188", "217", "218", "219"]
    sorts = ["desc", "asc"]
    types = ["position", "others"]

    for c in countries:
        if _stop_requested:
            return
        for s in sorts:
            for t in types:
                if _stop_requested:
                    return
                params = [f"s={s}", f"type={t}"]
                if c:
                    params.insert(0, f"c={c}")
                url = f"{BASE}/tobaccos/brands?" + "&".join(params)
                try:
                    soup = soup_of(url)
                    _add_brands_from_soup(soup, brands)
                except Exception:
                    pass
                time.sleep(0.15)
        print(f"    sweep c={c or 'all'}: total {len(brands)}")


def _sweep_other_pages(brands):
    """Добирает бренды из других страниц сайта."""
    extra_pages = [
        "/tobaccos",
        "/tobaccos/new",
        "/tobaccos/lines",
        "/hall-of-fame",
        "/reviews",
        "/compilation",
        "/",
    ]
    for path in extra_pages:
        if _stop_requested:
            return
        try:
            soup = soup_of(BASE + path)
            added = _add_brands_from_soup(soup, brands)
            print(f"    {path}: +{added} (total {len(brands)})")
        except Exception as e:
            print(f"    {path}: {e}")


def discover_brands():
    """Многофазный сбор всех брендов."""
    brands = {}

    # --- Фаза 1: базовые страницы /tobaccos/brands ---
    for path in ["/tobaccos/brands", "/tobaccos/brands?type=others"]:
        try:
            soup = soup_of(BASE + path)
            added = _add_brands_from_soup(soup, brands)
            print(f"  base {path}: +{added} (total {len(brands)})")
        except Exception as e:
            print(f"  base {path}: {e}")

    # --- Фаза 2: AJAX-эндпоинты 'Показать ещё' ---
    print("  trying AJAX endpoints…")
    if _try_ajax_endpoints(brands):
        print(f"  ✅ AJAX working — collected {len(brands)} brands")

    # --- Фаза 3: перебор фильтров (если AJAX не дал) ---
    if len(brands) < 250:
        print("  sweeping filters (may take ~1 min)…")
        _sweep_filters(brands)

    # --- Фаза 4: другие страницы сайта ---
    if len(brands) < 250:
        print("  sweeping other pages…")
        _sweep_other_pages(brands)

    print(f"▶ brands found: {len(brands)}")
    return list(brands.values())


# ---------- brand page scan ----------

def scan_page_for_brand(url, brand_slug, flavors, lines):
    """Обходит страницу и собирает линейки/вкусы бренда."""
    try:
        soup = soup_of(url)
    except Exception:
        return 0
    found = 0
    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if len(parts) < 3 or parts[0] != "tobaccos":
            continue
        if parts[1] != brand_slug:
            continue
        full = normalize(a["href"])
        name = text_of(a)
        if len(parts) == 3 and parts[2] and full not in lines:
            lines[full] = {"slug": parts[2], "url": full}
            found += 1
        elif len(parts) == 4 and parts[2] and parts[3] and full not in flavors:
            flavors[full] = name or parts[3]
            found += 1
    return found


def collect_brand_flavors(brand):
    flavors = {}
    lines = {}

    # 1) страница бренда + пагинация
    page = 1
    while not _stop_requested:
        url = brand["url"] + (f"?page={page}" if page > 1 else "")
        if scan_page_for_brand(url, brand["slug"], flavors, lines) == 0:
            break
        page += 1
        if page > 50:
            break

    # 2) страницы линеек + пагинация
    for line in list(lines.values()):
        lp = 1
        while not _stop_requested:
            url = line["url"] + (f"?page={lp}" if lp > 1 else "")
            if scan_page_for_brand(url, brand["slug"], flavors, lines) == 0:
                break
            lp += 1
            if lp > 50:
                break

    return [(name, url) for url, name in flavors.items()]


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--max-brands", type=int, default=0)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--consolidate-only", action="store_true")
    args = ap.parse_args()

    if args.consolidate_only:
        pairs = consolidate()
        print(f"Consolidated {len(pairs)} pairs -> {OUTPUT_FILE}")
        return

    if args.reset:
        for p in (STATE_FILE, JSONL_FILE, OUTPUT_FILE):
            if p.exists():
                p.unlink()
        print("Reset: state.json, data.jsonl, htreviews_full.json removed")

    state = load_state()
    done = set(state["done_brands"])
    print(f"▶ Resume: {len(done)} brands already done")

    print("▶ Discovering brands…")
    brands = discover_brands()
    print(f"  brands found: {len(brands)}")

    todo = [b for b in brands if b["url"] not in done]
    if args.max_brands:
        todo = todo[:args.max_brands]
    print(f"  to process this run: {len(todo)}")

    new_total = 0
    for brand in tqdm(todo, desc="Brands"):
        if _stop_requested:
            break
        try:
            pairs = collect_brand_flavors(brand)
        except Exception as e:
            print(f"  [!] {brand['name']}: {e}")
            continue

        for flavor_name, _ in pairs:
            append_jsonl({"brand": brand["name"], "flavor": flavor_name})
        new_total += len(pairs)
        done.add(brand["url"])
        save_state(state, done)
        time.sleep(args.delay)

    save_state(state, done)
    pairs = consolidate()
    print(f"\n✅ Done. total pairs={len(pairs)}  new this run={new_total}")
    print(f"   output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
