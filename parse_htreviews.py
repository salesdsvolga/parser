#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
htreviews.org — парсер (brand, line, flavor).
6 фаз сбора брендов. flavor = первый span (официальное название с банки).
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
RESERVED = {"brands", "lines", "new", "now"}
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

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
    print("\n⏸ Graceful stop — saving…")


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url, retries=3, backoff=1.5, timeout=30):
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
    return urljoin(BASE + "/", url).split("#")[0].split("?")[0].rstrip("/")


def path_parts(url):
    return urlparse(normalize(url)).path.strip("/").split("/")


def get_preferred_name(link_node, skip_classes=("country", "date_info", "extra")):
    """
    Берёт ПЕРВЫЙ непустой <span> — это официальное название (как на банке).
    Второй span (перевод/описание) игнорируется.
    Служебные span'ы (country, date_info) пропускаются.
    """
    spans = link_node.find_all("span", recursive=False)
    for sp in spans:
        classes = set(sp.get("class") or [])
        if classes & set(skip_classes):
            continue
        t = text_of(sp)
        if t:
            return t
    return text_of(link_node)


def get_alt_name(link_node, skip_classes=("country", "date_info", "extra")):
    """Второй непустой span (перевод/описание) — для необязательного поля flavor_alt."""
    spans = link_node.find_all("span", recursive=False)
    texts = []
    for sp in spans:
        classes = set(sp.get("class") or [])
        if classes & set(skip_classes):
            continue
        t = text_of(sp)
        if t:
            texts.append(t)
    return texts[1] if len(texts) > 1 else None


# ---------- persistence ----------

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
            s.setdefault("done_brands", [])
            return s
        except Exception:
            pass
    return {"done_brands": [], "started_at": now_iso()}


def load_existing_keys():
    keys = set()
    if JSONL_FILE.exists():
        with open(JSONL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    keys.add((obj.get("brand"), obj.get("line"), obj.get("flavor")))
                except Exception:
                    pass
    return keys


def append_jsonl(item):
    with open(JSONL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()


def save_state(state, done_brands):
    state["done_brands"] = sorted(done_brands)
    state["updated_at"] = now_iso()
    atomic_write_json(STATE_FILE, state)


def consolidate():
    items, seen = [], set()
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
                key = (obj.get("brand"), obj.get("line"), obj.get("flavor"))
                if None in key or key in seen:
                    continue
                seen.add(key)
                items.append(obj)
    items.sort(key=lambda x: (
        (x.get("brand") or "").lower(),
        (x.get("line") or "").lower(),
        (x.get("flavor") or "").lower(),
    ))
    atomic_write_json(OUTPUT_FILE, items)
    return items


# ---------- brand discovery ----------

def _add_brands_from_soup(soup, brands):
    added = 0
    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if not (len(parts) == 2 and parts[0] == "tobaccos"
                and parts[1] and parts[1] not in RESERVED):
            continue
        full = normalize(a["href"])
        name = get_preferred_name(a)
        if full not in brands:
            brands[full] = {"name": name or parts[1], "slug": parts[1], "url": full}
            added += 1
        else:
            old = brands[full]["name"]
            if name and CYRILLIC_RE.search(name) and not CYRILLIC_RE.search(old or ""):
                brands[full]["name"] = name
    return added


def _extract_from_items(soup, brands):
    items = soup.select(".tobacco_list_item, .new_flavors_item, .last_reviews_item")
    if not items:
        return _add_brands_from_soup(soup, brands)
    added = 0
    for item in items:
        for a in item.find_all("a", href=True):
            parts = path_parts(a["href"])
            if not (len(parts) == 2 and parts[0] == "tobaccos"
                    and parts[1] and parts[1] not in RESERVED):
                continue
            full = normalize(a["href"])
            name = get_preferred_name(a)
            if full not in brands:
                brands[full] = {"name": name or parts[1], "slug": parts[1], "url": full}
                added += 1
            else:
                old = brands[full]["name"]
                if name and CYRILLIC_RE.search(name) and not CYRILLIC_RE.search(old or ""):
                    brands[full]["name"] = name
    return added


def _try_ajax_endpoints(brands):
    candidates = [
        "/htmx/load/brands?offset=20",
        "/htmx/load/brands?offset=20&limit=100",
        "/htmx/load/brands?offset=20&count=100",
        "/htmx/load/brands?type=others&offset=20",
        "/htmx/load/brands?type=others&offset=20&limit=100",
        "/htmx/load/tops_shortlist?entity=brands",
        "/htmx/load/trends?object=brands",
        "/htmx/load/tobaccos?type=brands&offset=20",
        "/htmx/load/tobaccos/brands?offset=20",
        "/htmx/load/more?entity=brands&offset=20",
        "/htmx/load/more_brands?offset=20",
        "/api/brands?offset=20",
        "/api/tobaccos?type=brands&offset=20",
    ]
    for path in candidates:
        if _stop_requested:
            return False
        try:
            text = fetch(BASE + path, retries=1, timeout=10)
            soup = BeautifulSoup(text, "lxml")
            added = _add_brands_from_soup(soup, brands)
            if added > 0:
                print(f"    ajax {path}: +{added} (total {len(brands)})")
            if added > 5:
                return True
        except Exception:
            pass
    return False


def _sweep_filters(brands):
    countries = ["", "13", "20", "29", "37", "41", "51", "64", "68", "69",
                 "70", "71", "80", "98", "100", "137", "142", "145", "151",
                 "188", "217", "218", "219"]
    for c in countries:
        if _stop_requested:
            return
        for s in ["desc", "asc"]:
            for t in ["position", "others"]:
                params = [f"s={s}", f"type={t}"]
                if c:
                    params.insert(0, f"c={c}")
                url = f"{BASE}/tobaccos/brands?" + "&".join(params)
                try:
                    soup = soup_of(url)
                    _add_brands_from_soup(soup, brands)
                except Exception:
                    pass
                time.sleep(0.1)
        print(f"    filters c={c or 'all'}: total {len(brands)}")


def _probe_pagination(brands):
    last = len(brands)
    for param in ["offset", "start", "skip", "from"]:
        for n in [40, 60, 100, 200]:
            if _stop_requested:
                return
            for t in ["position", "others"]:
                url = f"{BASE}/tobaccos/brands?type={t}&{param}={n}"
                try:
                    soup = soup_of(url)
                    _add_brands_from_soup(soup, brands)
                except Exception:
                    pass
                time.sleep(0.1)
            if len(brands) - last >= 10:
                print(f"    pagination {param}={n}: total {len(brands)}")
                last = len(brands)
    for param in ["page", "p"]:
        for n in range(2, 20):
            if _stop_requested:
                return
            for t in ["position", "others"]:
                url = f"{BASE}/tobaccos/brands?type={t}&{param}={n}"
                try:
                    soup = soup_of(url)
                    _add_brands_from_soup(soup, brands)
                except Exception:
                    pass
                time.sleep(0.1)
            if len(brands) - last >= 10:
                print(f"    pagination {param}={n}: total {len(brands)}")
                last = len(brands)


def _discover_via_tags(brands):
    """220 тегов вкусов × 20 вкусов. Из .tobacco_list_item берём бренды."""
    print("    fetching tag list…")
    try:
        soup = soup_of(BASE + "/tobaccos")
    except Exception as e:
        print(f"    [!] {e}")
        return

    tag_ids = set()
    for inp in soup.find_all("input", attrs={"data-type": "tags"}):
        v = inp.get("value")
        if v:
            tag_ids.add(v)
    print(f"    found {len(tag_ids)} tags")
    if not tag_ids:
        return

    for tag in tqdm(sorted(tag_ids), desc="Tags", leave=False):
        if _stop_requested:
            return
        url = f"{BASE}/tobaccos?t={tag}"
        try:
            s = soup_of(url)
            _extract_from_items(s, brands)
        except Exception:
            pass
        time.sleep(0.08)
    print(f"    tags done, brands total: {len(brands)}")


def _sweep_other_pages(brands):
    for path in ["/tobaccos", "/tobaccos/new", "/tobaccos/lines",
                 "/hall-of-fame", "/reviews", "/compilation", "/"]:
        if _stop_requested:
            return
        try:
            soup = soup_of(BASE + path)
            added = _extract_from_items(soup, brands)
            print(f"    {path}: +{added} (total {len(brands)})")
        except Exception as e:
            print(f"    {path}: {e}")


def discover_brands():
    brands = {}
    print("  [1/6] base pages…")
    for path in ["/tobaccos/brands", "/tobaccos/brands?type=others"]:
        try:
            soup = soup_of(BASE + path)
            added = _add_brands_from_soup(soup, brands)
            print(f"    {path}: +{added} (total {len(brands)})")
        except Exception as e:
            print(f"    {path}: {e}")

    print("  [2/6] AJAX endpoints…")
    _try_ajax_endpoints(brands)

    if len(brands) < 300:
        print("  [3/6] filter sweep…")
        _sweep_filters(brands)

    if len(brands) < 300:
        print("  [4/6] pagination params…")
        _probe_pagination(brands)

    if len(brands) < 300:
        print("  [5/6] tag sweep…")
        _discover_via_tags(brands)

    if len(brands) < 300:
        print("  [6/6] other pages…")
        _sweep_other_pages(brands)

    print(f"▶ brands found: {len(brands)}")
    return list(brands.values())


# ---------- brand / line / flavor extraction ----------

def _extract_items(soup, brand_slug, lines, flavors):
    items = soup.select(".tobacco_list_item, .new_flavors_item, .last_reviews_item")
    if not items:
        return _extract_from_links(soup, brand_slug, lines, flavors)

    added = 0
    for item in items:
        line_url = line_name = flavor_url = flavor_name = None
        flavor_alt = None

        for a in item.find_all("a", href=True):
            parts = path_parts(a["href"])
            if len(parts) < 3 or parts[0] != "tobaccos" or parts[1] != brand_slug:
                continue
            full = normalize(a["href"])
            name = get_preferred_name(a)

            if len(parts) == 3:
                line_url = full
                line_name = name
            elif len(parts) == 4:
                flavor_url = full
                flavor_name = name
                flavor_alt = get_alt_name(a)

        if line_url:
            line_slug = path_parts(line_url)[2]
            key = (brand_slug, line_slug)
            if key not in lines:
                lines[key] = {"name": line_name or line_slug, "url": line_url}
                added += 1

        if flavor_url:
            fparts = path_parts(flavor_url)
            if len(fparts) == 4:
                line_slug = fparts[2]
                flavor_slug = fparts[3]
                key = (brand_slug, line_slug, flavor_slug)
                if key not in flavors:
                    flavors[key] = {
                        "name": flavor_name or flavor_slug,
                        "alt": flavor_alt,
                        "url": flavor_url,
                    }
                    added += 1
    return added


def _extract_from_links(soup, brand_slug, lines, flavors):
    added = 0
    for a in soup.find_all("a", href=True):
        parts = path_parts(a["href"])
        if len(parts) < 3 or parts[0] != "tobaccos" or parts[1] != brand_slug:
            continue
        full = normalize(a["href"])
        name = get_preferred_name(a)

        if len(parts) == 3:
            key = (brand_slug, parts[2])
            if key not in lines:
                lines[key] = {"name": name or parts[2], "url": full}
                added += 1
        elif len(parts) == 4:
            key = (brand_slug, parts[2], parts[3])
            if key not in flavors:
                flavors[key] = {
                    "name": name or parts[3],
                    "alt": get_alt_name(a),
                    "url": full,
                }
                added += 1
    return added


def collect_brand_data(brand, lines, flavors):
    brand_slug = brand["slug"]

    page = 1
    while not _stop_requested:
        url = brand["url"] + (f"?page={page}" if page > 1 else "")
        try:
            soup = soup_of(url)
        except Exception:
            break
        added = _extract_items(soup, brand_slug, lines, flavors)
        if added == 0:
            break
        page += 1
        if page > 30:
            break
        time.sleep(0.15)

    brand_lines = [(k, v) for k, v in lines.items() if k[0] == brand_slug]
    for line_key, line_data in tqdm(brand_lines, desc=f"  {brand['name']}", leave=False):
        if _stop_requested:
            return
        lp = 1
        while True:
            url = line_data["url"] + (f"?page={lp}" if lp > 1 else "")
            try:
                soup = soup_of(url)
            except Exception:
                break
            added = _extract_items(soup, brand_slug, lines, flavors)
            if added == 0:
                break
            lp += 1
            if lp > 30:
                break
            time.sleep(0.15)


def flush_brand_flavors(brand, lines, flavors, existing_keys):
    brand_slug = brand["slug"]
    new_count = 0
    for (b_slug, l_slug, f_slug), fdata in flavors.items():
        if b_slug != brand_slug:
            continue
        line = lines.get((b_slug, l_slug), {})
        item = {
            "brand": brand["name"],
            "brand_url": brand["url"],
            "line": line.get("name") or l_slug,
            "line_url": line.get("url") or f"{brand['url']}/{l_slug}",
            "flavor": fdata["name"],
            "flavor_url": fdata["url"],
        }
        if fdata.get("alt"):
            item["flavor_alt"] = fdata["alt"]

        key = (item["brand"], item["line"], item["flavor"])
        if key in existing_keys:
            continue
        append_jsonl(item)
        existing_keys.add(key)
        new_count += 1
    return new_count


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.25)
    ap.add_argument("--max-brands", type=int, default=0)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--consolidate-only", action="store_true")
    args = ap.parse_args()

    if args.consolidate_only:
        items = consolidate()
        print(f"Consolidated {len(items)} flavors -> {OUTPUT_FILE}")
        return

    if args.reset:
        for p in (STATE_FILE, JSONL_FILE, OUTPUT_FILE):
            if p.exists():
                p.unlink()
        print("Reset done.")

    state = load_state()
    done = set(state["done_brands"])
    existing_keys = load_existing_keys()
    print(f"▶ Resume: {len(done)} brands, {len(existing_keys)} flavors already")

    print("▶ Discovering brands…")
    brand_list = discover_brands()
    print(f"  brands found: {len(brand_list)}")

    todo = [b for b in brand_list if b["url"] not in done]
    if args.max_brands:
        todo = todo[:args.max_brands]
    print(f"  to process this run: {len(todo)}")

    total_new = 0
    for brand in tqdm(todo, desc="Brands"):
        if _stop_requested:
            break
        try:
            lines, flavors = {}, {}
            collect_brand_data(brand, lines, flavors)
            n = flush_brand_flavors(brand, lines, flavors, existing_keys)
            total_new += n
        except Exception as e:
            print(f"  [!] {brand['name']}: {e}")
            continue

        done.add(brand["url"])
        save_state(state, done)
        time.sleep(args.delay)

    save_state(state, done)
    items = consolidate()
    print(f"\n✅ Done. total={len(items)} flavors, new this run={total_new}")
    print(f"   output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
