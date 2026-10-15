#!/usr/bin/env python3
"""Каталог товаров продавца Ezhi с барахолки diesel.elcat.kg.

Продавец держит товар в нескольких темах-каталогах, которые ссылаются друг
на друга. Скрипт обходит их от стартовой темы, берёт только посты продавца,
вытаскивает строки с ценой и складывает всё в catalog.json / .csv / .md.

    python3 ezhi_catalog.py crawl            # обойти темы (кеш страниц в cache/)
    python3 ezhi_catalog.py crawl --refresh  # скачать заново, не из кеша
    python3 ezhi_catalog.py images           # скачать фото товаров в images/
    python3 ezhi_catalog.py search ms5611 bmp280 "pro mini"

Только стандартная библиотека. Между запросами пауза, страницы кешируются.
"""
import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://diesel.elcat.kg/index.php"
SELLER_ID = "147817"          # Ezhi
SEED_TOPICS = ["293246934"]   # Arduino+датчики, STM32, Raspberry, ESP32, LCD, OLED...
POSTS_PER_PAGE = 20           # IP.Board 3: следующая страница = &st=20, 40, ...
DELAY = 1.5                   # секунд между запросами
UA = "Mozilla/5.0 (X11; Linux x86_64) ezhi-catalog/1.0"

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
IMAGES = HERE / "images"
IMAGE_DELAY = 0.3             # секунд между скачиваниями фото

# «- 900 c», «– 1 200 с», «- 350 сом»; c бывает латинской. Перед тире — пробел
# или начало строки, либо пробел после тире («3.3v- 80 с»); иначе «2S-35C-7.4V»
# читается как цена 35.
PRICE_RE = re.compile(r"(?:(?:^|(?<=\s))[-–—:]\s*|[-–—:]\s+)(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)\s*(?:c|с|сом|som)(?=$|[\s.,;/)(])", re.I)
TOPIC_RE = re.compile(r"showtopic=(\d+)")
# цена отдельной строкой под названием: «330 с», «Цена 370 с», «Цена: 1 200 сом»
PRICE_ONLY_RE = re.compile(r"^(?:цена\s*:?\s*)?(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)\s*(?:c|с|сом|som)\.?$", re.I)
JUNK_RE = re.compile(r"^\d+ скачивани|^Прикрепленн|^Размер файла|^\d+(?:[.,]\d+)? ?(?:КБ|МБ|KB|MB)$", re.I)


def fetch(url, cache_name, max_age=None):
    """Страница из cache/, если она моложе max_age секунд (None — любой давности)."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / cache_name
    if path.exists() and (max_age is None or time.time() - path.stat().st_mtime < max_age):
        return path.read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read().decode("utf-8", "replace")
    path.write_text(data, encoding="utf-8")
    time.sleep(DELAY)
    return data


def page_url(topic, st):
    return f"{BASE}?showtopic={topic}" + (f"&st={st}" if st else "")


def split_posts(page):
    """[(post_id, author_id, date, body_html)] для одной страницы темы."""
    out = []
    for m in re.finditer(r"<div class='post_block[^']*' id='post_id_(\d+)'>", page):
        start = m.end()
        nxt = page.find("<div class='post_block", start)
        block = page[start:nxt if nxt != -1 else len(page)]
        author = re.search(r'hovercard-id="(\d+)"', block)
        date = re.search(r'class="published"[^>]*title=["\']([^"\']+)', block)
        body = block.split('itemprop="commentText"', 1)
        body = body[1].split(">", 1)[-1] if len(body) > 1 else ""   # хвост тега <div ... class='post entry-content'>
        body = re.split(r"<!--\s*(?:/post|Post end)|<div class='signature'|<ul id='postControlsNormal", body)[0]
        out.append((m.group(1), author.group(1) if author else "", date.group(1) if date else "", body))
    return out


def body_lines(body):
    """Текст поста построчно; картинка — отдельная строка «[img URL]»."""
    b = re.sub(r"(?is)<script.*?</script>", "", body)
    b = re.sub(r"<img[^>]*src=[\"']([^\"']+)[\"'][^>]*>", r"\n[img \1]\n", b)
    b = re.sub(r"(?is)<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 <<\1>>", b)
    b = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", b)
    b = re.sub(r"(?i)<(?:strike|s|del)(?:\s[^>]*)?>", "\x01", b)   # зачёркнуто = нет в наличии
    b = re.sub(r"(?i)</(?:strike|s|del)>", "\x02", b)
    b = html.unescape(re.sub(r"<[^>]+>", "", b)).replace(" ", " ")
    lines, struck = [], False
    for ln in b.split("\n"):
        out = "~~" if struck else ""
        for ch in ln:
            if ch == "\x01":
                out += "" if struck else "~~"
                struck = True
            elif ch == "\x02":
                out += "~~" if struck else ""
                struck = False
            else:
                out += ch
        if struck:
            out += "~~"                       # зачёркивание продолжится на следующей строке
        ln = re.sub(r"\s+", " ", out).strip()
        ln = re.sub(r"~~\s*~~", " ", ln).strip()
        if ln.replace("~~", "").strip().startswith("[img "):
            ln = ln.replace("~~", "").strip()
        if ln and not ln.startswith("commentText"):
            lines.append(ln.lstrip("'> ").strip())
    return [ln for ln in lines if ln.replace("~~", "").strip()]


def _is_variant(ln):
    """Строка с ценой, чьё «название» — только вариант: «4000W», «120 мм», «(SMD)»."""
    if not PRICE_RE.search(ln):
        return False
    name = PRICE_RE.split(ln)[0].strip(" -–—:")
    return not name or len(name) <= 14 or (len(name) <= 30 and bool(re.match(r"^[\d(]", name)))


def _assign_images(group_items, imgs):
    """Фото идут пачкой после своих товаров. Без угадывания, какое чьё:
    каждый товар пачки получает всю пачку в порядке поста."""
    for it in group_items:
        it["images"] = list(imgs)


def exact_url(topic, st, post_id, group, name):
    """Ссылка прямо на строку товара в посте: якорь поста + text fragment
    (#:~:text=), браузер прокрутит к тексту и подсветит его."""
    def cut(t, n=48):             # по границе слова, иначе браузер не найдёт
        words, out = t.split(), ""
        for w in words:
            if out and len(out) + 1 + len(w) > n:
                break
            out = f"{out} {w}".strip()
        return out
    enc = lambda t: urllib.parse.quote(t, safe="").replace("-", "%2D")
    frag = f"{enc(cut(group))},{enc(cut(name, 32))}" if group else enc(cut(name))
    return f"{BASE}?showtopic={topic}" + (f"&st={st}" if st else "") + f"#entry{post_id}:~:text={frag}"


def parse_items(lines, topic, title, post_id, date, st=0):
    """Строка с ценой = товар; строки без цены после неё = описание;
    строка без цены после картинки или разделителя = заголовок группы
    («Модуль заряда TP4056…», а ниже «microUSB - 40 с»); пачка фото после
    товаров относится к ним (см. _assign_images). Цена отдельной строкой
    («330 с») закрывает вариант: его название — строки с прошлой цены."""
    items, since_img, current, group, group_extra = [], [], None, "", []
    img_run, tail = [], []   # tail — строки описания, пришедшие после цены текущего товара
    last_group = ""   # группа переживает картинку: «Диммер» → 10000W, 6000W, [img], 4000W
    clean = lambda x: re.sub(r"\s*<<[^>]+>>", "", x).strip()

    def make_item(full, ln, prices, grp, name, details):
        gone = [p for p in prices if p in struck_prices]
        left = [p for p in prices if p not in gone] or prices
        it = {
            "id": hashlib.sha1(f"{topic}|{post_id}|{full}|{ln}".encode()).hexdigest()[:10],
            "name": full, "line": ln, "group": grp,
            "prices": prices, "price": min(left), "prices_out": gone,
            "out": fully or group_out or len(gone) == len(prices),
            "struck_parts": [] if fully else [x for x in struck_text if not PRICE_ONLY_RE.match(x)],
            "details": details, "images": [], "links": [],
            "topic": topic, "topic_title": title,
            "post_id": post_id, "date": date, "st": st,
            "url": f"{BASE}?showtopic={topic}&view=findpost&p={post_id}",
            "url_exact": exact_url(topic, st, post_id, grp, name),
        }
        items.append(it)
        since_img.append(it)
        return it

    lines = [ln for ln in lines if ln.replace("~~", "") not in ("Скрытый текст", "Спойлер")]  # подпись свёрнутого блока
    group_out = False
    raw_lines, lines = lines, [ln.replace("~~", "").strip() for ln in lines]
    for k, ln in enumerate(lines):
        raw = raw_lines[k]
        struck_text = [x.strip() for x in re.findall(r"~~(.*?)~~", raw) if x.strip()]
        fully = bool(struck_text) and not re.search(r"\w", re.sub(r"~~.*?~~", "", raw))
        struck_prices = [int(re.sub(r"\D", "", m)) for x in struck_text
                         for m in re.findall(r"(\d{1,3}(?:[ \u00a0]\d{3})*|\d+)\s*(?:c|с|сом)(?!\w)", x)]
        if ln.startswith("[img "):
            url = ln[5:-1]
            if "style_images" in url or "emoticons" in url:
                continue
            img_run.append(url)
            current, group, group_extra, tail, group_out = None, "", [], [], False
            continue
        if (not re.search(r"\w", ln) and not (len(ln) >= 3 and set(ln) <= set("-_=—–*. "))) or JUNK_RE.match(ln):
            continue                                  # «.», «]», «23 скачиваний» между фото — пачку не рвёт
        if img_run:                                   # пачка фото кончилась
            if since_img:
                _assign_images(since_img, img_run)
            since_img, img_run = [], []
        if set(ln) <= set("-_=—–*. "):
            current, group, group_extra, last_group, tail, group_out = None, "", [], "", [], False
            continue
        prices = [int(re.sub(r"\D", "", p)) for p in PRICE_RE.findall(ln)]
        only = PRICE_ONLY_RE.match(ln) if not prices else None
        if only:
            price = [int(re.sub(r"\D", "", only.group(1)))]
            if current is None and group:
                # заголовок (+ характеристики) → «200 с»: первый вариант товара
                current = make_item(group, ln, price, group, group, list(group_extra))
                last_group, group_extra, tail = group, [], []
                continue
            if current is not None and tail:
                # строки после прошлой цены — это следующий вариант: «0 - 50 ампер / с шунтом / 300 с»
                del current["details"][-len(tail):]
                g = current["group"]
                vname = " ".join(tail)[:100]
                full, det = (f"{g} — {vname}", []) if g else (tail[0], tail[1:])
                current = make_item(full, ln, price, g, vname if g else tail[0], det)
                tail = []
                continue
        if prices:
            name = PRICE_RE.split(ln)[0].strip(" -–—:") or ln
            if not group and last_group and _is_variant(ln):
                group = last_group
            last_group = group
            extra = list(group_extra)
            # короткая подпись над вариантами («ширина», «Цвет:») — часть названия варианта: «ширина 2,5см»
            if group and extra and len(extra[-1]) <= 20 and not re.search(r"\d", extra[-1]) and _is_variant(ln):
                name = f"{extra[-1].rstrip(':').strip()} {name}"
                extra = extra[:-1]
            current = make_item(f"{group} — {name}" if group else name, ln, prices, group, name, extra)
            tail = []
        elif current is not None and _is_variant(lines[k + 1] if k + 1 < len(lines) else ""):
            # заголовок новой группы сразу после товара, без картинки между ними:
            # «Диммер 220V» → «4000W - 330 с», «3800W - 280 с»
            current, group, group_extra, tail, group_out = None, clean(ln), [], [], fully
        elif current is None:
            if not group:
                group, group_out = clean(ln), fully
            else:
                group_extra.append(clean(ln))
        else:
            current["details"].append(("✕ " if fully else "") + clean(ln))
            tail.append(clean(ln))
            current["links"] += [u for u in re.findall(r"<<([^>]+)>>", ln) if "diesel.elcat.kg/index.php?s=" not in u]
    if img_run and since_img:
        _assign_images(since_img, img_run)
    return items


def crawl_topic(topic, max_age=None):
    """(title, starter_id, items, linked_topics) по всем страницам темы."""
    title, starter, items, links, st = "", None, [], set(), 0
    while True:
        page = fetch(page_url(topic, st), f"{topic}_{st}.html", max_age)
        if not title:
            t = re.search(r"<title>([^<]*)", page)
            title = html.unescape(t.group(1)).split(" - Барахолка")[0].split(" - Diesel Forum")[0] if t else topic
        posts = split_posts(page)
        if not posts:
            break
        if starter is None:
            starter = posts[0][1]
        for post_id, author, date, body in posts:
            if author != SELLER_ID:
                continue
            links |= set(TOPIC_RE.findall(body))
            items += parse_items(body_lines(body), topic, title, post_id, date, st)
        has_next = re.search(rf"showtopic={topic}(?:&amp;|&)[^'\"]*st={st + POSTS_PER_PAGE}\b", page)
        if not has_next:
            break
        st += POSTS_PER_PAGE
    return title, starter, items, links


def crawl(args):
    max_age = 0 if args.refresh else (args.max_age * 3600 if args.max_age else None)
    data = collect(max_age)
    write_outputs(data)
    print(f"готово: {len(data['topics'])} тем, {len(data['items'])} товаров → catalog.json/.csv/.md")


def collect(max_age=None, log=print):
    """Обойти темы продавца; страницы старше max_age секунд скачиваются заново."""
    todo, seen, topics, items = list(SEED_TOPICS), set(), [], []
    while todo:
        topic = todo.pop(0)
        if topic in seen:
            continue
        seen.add(topic)
        try:
            title, starter, its, links = crawl_topic(topic, max_age)
        except Exception as e:  # недоступная тема не должна валить обход
            print(f"  ! {topic}: {e}", file=sys.stderr)
            continue
        if starter != SELLER_ID:
            log(f"  - {topic}: не тема продавца ({title[:60]})")
            continue
        log(f"  + {topic}: {len(its):4d} товаров  {title[:80]}")
        topics.append({"topic": topic, "title": title, "items": len(its),
                       "url": f"{BASE}?showtopic={topic}"})
        items += its
        todo += sorted(links - seen)
    data = {"crawled": time.strftime("%Y-%m-%d %H:%M"), "seller": SELLER_ID,
            "topics": topics, "items": items}
    uniq = {}
    for it in items:                      # одна и та же строка дважды в одном посте
        uniq.setdefault(it["id"], it)
    data["items"] = list(uniq.values())
    attach_local_images(data)
    return data


def image_file(url):
    """Локальное имя картинки: images/<месяц>_<файл>, без _thumb (берём полную)."""
    m = re.search(r"/uploads/([^/]+)/([^/?#]+)$", url)
    name = f"{m.group(1)}_{m.group(2)}" if m else re.sub(r"\W+", "_", url)[-80:]
    return IMAGES / name.replace("_thumb.", ".")


def attach_local_images(data):
    for it in data["items"]:
        it["images_local"] = [str(image_file(u).relative_to(HERE))
                              for u in it["images"] if image_file(u).exists()]


def images(args):
    """Скачать фото товаров в images/ (уже скачанные пропускаются)."""
    data = json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
    IMAGES.mkdir(exist_ok=True)
    urls = sorted({u for it in data["items"] for u in it["images"]})
    todo = [u for u in urls if not image_file(u).exists()]
    print(f"фото: всего {len(urls)}, скачать {len(todo)}")
    for n, url in enumerate(todo, 1):
        full = url.replace("_thumb.", ".")
        for u in ([full, url] if full != url else [url]):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    image_file(url).write_bytes(r.read())
                break
            except Exception as e:
                err = e
        else:
            print(f"  ! {url}: {err}", file=sys.stderr)
        if n % 100 == 0:
            print(f"  {n}/{len(todo)}")
        time.sleep(IMAGE_DELAY)
    attach_local_images(data)
    write_outputs(data)
    print(f"готово: {sum(1 for u in urls if image_file(u).exists())}/{len(urls)} фото в images/")


def write_outputs(data):
    items, topics = data["items"], data["topics"]
    (HERE / "catalog.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(HERE / "catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["topic_title", "name", "price_som", "all_prices", "details", "url",
                    "image", "image_local", "date"])
        for it in items:
            w.writerow([it["topic_title"], it["name"], it["price"], " / ".join(map(str, it["prices"])),
                        " | ".join(it["details"]), it["url"], (it["images"] or [""])[0],
                        (it.get("images_local") or [""])[0], it["date"][:10]])
    with open(HERE / "catalog.md", "w", encoding="utf-8") as f:
        f.write(f"# Каталог Ezhi (diesel.elcat.kg), обход {data['crawled']}\n\n")
        for t in topics:
            f.write(f"## [{t['title']}]({t['url']})\n\n")
            for it in (i for i in items if i["topic"] == t["topic"]):
                f.write(f"- {it['name']} — **{' / '.join(map(str, it['prices']))} с**")
                if it["details"]:
                    f.write(f" — _{'; '.join(it['details'][:4])}_")
                f.write(f" [пост]({it['url']})")
                photos = it.get("images_local") or it["images"]
                if photos:
                    f.write(f" [фото]({photos[0]})")
                f.write("\n")
            f.write("\n")


def compact(data, crawled_at_ms):
    """Каталог для сайта (docs/data/catalog.json) — тот же формат, что строит docs/ezhi.js в браузере."""
    tix = {t["topic"]: i for i, t in enumerate(data["topics"])}
    return {
        "crawledAt": crawled_at_ms,
        "topics": [{"topic": t["topic"], "title": t["title"], "url": t["url"]} for t in data["topics"]],
        "items": [{
            "id": it["id"], "n": it["name"], "g": it.get("group", ""), "p": it["prices"], "d": it["details"],
            "t": tix.get(it["topic"], -1), "u": it.get("url_exact") or it["url"], "im": it["images"],
            "o": bool(it.get("out")), "px": it.get("prices_out") or [], "xs": it.get("struck_parts") or [],
            "pi": it["post_id"], "st": it.get("st", 0), "l": it["line"],
        } for it in data["items"]],
    }


def snapshot(args):
    """Обойти форум заново и записать снимок каталога для сайта."""
    data = collect(0 if args.refresh else None)
    out = HERE / "docs" / "data" / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(compact(data, int(time.time() * 1000)), ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"снимок: {len(data['topics'])} тем, {len(data['items'])} товаров → {out.relative_to(HERE)}")


def search(args):
    data = json.loads((HERE / "catalog.json").read_text(encoding="utf-8"))
    print(f"(каталог от {data['crawled']})")
    for q in args.query:
        words = q.lower().split()
        hits = [it for it in data["items"]
                if all(w in (it["name"] + " " + it["line"] + " " + " ".join(it["details"])).lower() for w in words)]
        print(f"\n== {q}: {len(hits)}")
        for it in hits:
            print(f"  {it['price']:>6} с  {it['name'][:90]}  [{it['line'][:60]}]\n          {it['url']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("--refresh", action="store_true", help="не брать страницы из кеша")
    c.add_argument("--max-age", type=float, metavar="ЧАСОВ", help="перекачать страницы старше N часов")
    sub.add_parser("images", help="скачать фото товаров в images/")
    sn = sub.add_parser("snapshot", help="снимок каталога для сайта → docs/data/catalog.json")
    sn.add_argument("--refresh", action="store_true", help="не брать страницы из кеша")
    s = sub.add_parser("search")
    s.add_argument("query", nargs="+", help="слова через пробел в одном аргументе = И")
    args = ap.parse_args()
    {"crawl": crawl, "images": images, "search": search, "snapshot": snapshot}[args.cmd](args)


if __name__ == "__main__":
    main()
