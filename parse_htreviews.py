#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — минимальный резюмируемый парсер.
Собирает ТОЛЬКО: brand + flavor.

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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE = "https://htreviews.org"
STATE_FILE = Path("state.json")
JSONL_FILE = Path("data.jsonl")
OUTPUT_FILE = Path("htreviews_full.json")
SAVE_EVERY = 50

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
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


def clean_url(u):
    return u.split("#")[0].split("?")[0].rstrip("/")


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


def discover_brands():
    brands = {}
    page = 1
    while not _stop_requested:
        url = f"{BASE}/tobaccos/brands" + (f"?page={page}" if page > 1 else "")
        try:
            soup = soup_of(url)
        except Exception:
            break
        found = 0
        for a in soup.select('a[href^="/tobaccos/"]'):
            href = clean_url(a["href"])
            parts = href.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "tobaccos" and parts[1] != "brands":
                full = urljoin(BASE, href)
                if full not in brands:
                    brands[full] = {"name": text_of(a) or parts[1],
                                    "slug": parts[1], "url": full}
                    found += 1
        if found == 0:
            break
        page += 1
        if page > 500:
            break
    return list(brands.values())


def collect_brand_flavors(brand):
    flavors = {}
    lines = {}

    def scan_page(url):
        try:
            soup = soup_of(url)
        except Exception:
            return 0
        found = 0
        for a in soup.select('a[href^="/tobaccos/"]'):
            href = clean_url(a["href"])
            parts = href.strip("/").split("/")
            if len(parts) < 3 or parts[0] != "tobaccos" or parts[1] != brand["slug"]:
                continue
            full = urljoin(BASE, href)
            name = text_of(a)
            if len(parts) == 3 and full not in lines:
                lines[full] = {"slug": parts[2], "url": full}
                found += 1
            elif len(parts) == 4 and full not in flavors:
                flavors[full] = name or parts[3]
                found += 1
        return found

    page = 1
    while not _stop_requested:
        url = brand["url"] + (f"?page={page}" if page > 1 else "")
        if scan_page(url) == 0:
            break
        page += 1
        if page > 100:
            break

    for line in list(lines.values()):
        lp = 1
        while not _stop_requested:
            url = line["url"] + (f"?page={lp}" if lp > 1 else "")
            if scan_page(url) == 0:
                break
            lp += 1
            if lp > 100:
                break

    return [(name, url) for url, name in flavors.items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.2)
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
