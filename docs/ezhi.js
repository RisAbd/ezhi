// Каталог Ezhi прямо в браузере, без своего сервера.
// Форум diesel.elcat.kg отдаёт страницы с Access-Control-Allow-Origin: *, поэтому страница сама
// скачивает темы продавца, разбирает посты (перенос ezhi_catalog.py один в один: те же id товаров)
// и держит всё в Cache Storage браузера. Корзины — в localStorage, поделиться — ссылкой.
"use strict";

const EZ = (() => {
  const BASE = "https://diesel.elcat.kg/index.php";
  const SELLER = "147817";            // Ezhi
  const SEED = ["293246934"];         // Arduino+датчики, STM32, Raspberry, ESP32, LCD, OLED...
  const PER_PAGE = 20;                // IP.Board 3: следующая страница = &st=20, 40, ...
  const MAX_AGE_H = 3;                // старше — перекачать с форума
  const DELAY_MS = 300;               // пауза между запросами к форуму
  const CACHE = "ezhi-v1";

  // ---------- как в ezhi_catalog.py (\w Питона = буквы и цифры любого алфавита) ----------
  const W = "[\\p{L}\\p{N}_]";
  const NUM = "(\\d{1,3}(?:[ \\u00a0]\\d{3})*|\\d+)";
  const PRICE = `(?:(?:^|(?<=\\s))[-–—:]\\s*|[-–—:]\\s+)${NUM}\\s*(?:c|с|сом|som)(?=$|[\\s.,;/)(])`;
  const priceG = () => new RegExp(PRICE, "giu");
  const PRICE_1 = new RegExp(PRICE, "iu");
  const PRICE_ONLY = new RegExp(`^(?:цена\\s*:?\\s*)?${NUM}\\s*(?:c|с|сом|som)\\.?$`, "iu");
  const JUNK = /^\d+ скачивани|^Прикрепленн|^Размер файла|^\d+(?:[.,]\d+)? ?(?:КБ|МБ|KB|MB)$/iu;
  const WORD = new RegExp(W, "u");
  const STRUCK_PRICE = new RegExp(`${NUM}\\s*(?:c|с|сом)(?!${W})`, "gu");
  const SEP = new Set("-_=—–*. ");
  const onlySep = (s) => [...s].every((c) => SEP.has(c));
  const stripChars = (s, chars) => {
    const set = new Set(chars), a = [...s];
    let i = 0, j = a.length;
    while (i < j && set.has(a[i])) i++;
    while (j > i && set.has(a[j - 1])) j--;
    return a.slice(i, j).join("");
  };
  const lstripChars = (s, chars) => {
    const set = new Set(chars), a = [...s];
    let i = 0;
    while (i < a.length && set.has(a[i])) i++;
    return a.slice(i).join("");
  };
  const toInt = (s) => parseInt(s.replace(/\D/g, ""), 10);

  let ta = null;
  function unescapeHtml(s) {                       // html.unescape
    if (!s.includes("&")) return s;
    ta ||= document.createElement("textarea");
    ta.innerHTML = s;
    return ta.value;
  }

  // SHA-1 (id товара = первые 10 знаков sha1("тема|пост|название|строка"), как в Питоне)
  function sha1hex(str) {
    const b = new TextEncoder().encode(str), n = b.length;
    const words = new Uint32Array((((n + 8) >> 6) + 1) * 16);
    for (let i = 0; i < n; i++) words[i >> 2] |= b[i] << (24 - (i % 4) * 8);
    words[n >> 2] |= 0x80 << (24 - (n % 4) * 8);
    words[words.length - 1] = n * 8;
    let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
    const w = new Uint32Array(80);
    for (let i = 0; i < words.length; i += 16) {
      for (let t = 0; t < 16; t++) w[t] = words[i + t];
      for (let t = 16; t < 80; t++) { const x = w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16]; w[t] = (x << 1) | (x >>> 31); }
      let a = h0, bb = h1, c = h2, d = h3, e = h4;
      for (let t = 0; t < 80; t++) {
        const f = t < 20 ? (bb & c) | (~bb & d) : t < 40 ? bb ^ c ^ d : t < 60 ? (bb & c) | (bb & d) | (c & d) : bb ^ c ^ d;
        const k = t < 20 ? 0x5a827999 : t < 40 ? 0x6ed9eba1 : t < 60 ? 0x8f1bbcdc : 0xca62c1d6;
        const tmp = (((a << 5) | (a >>> 27)) + f + e + k + w[t]) >>> 0;
        e = d; d = c; c = (bb << 30) | (bb >>> 2); bb = a; a = tmp;
      }
      h0 = (h0 + a) >>> 0; h1 = (h1 + bb) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0;
    }
    return [h0, h1, h2, h3, h4].map((x) => (x >>> 0).toString(16).padStart(8, "0")).join("");
  }

  const pageUrl = (topic, st) => `${BASE}?showtopic=${topic}` + (st ? `&st=${st}` : "");

  // Мобильный браузер форум кормит мобильной версией (по User-Agent, из fetch его не сменить):
  // посты там в <div class='topic_reply' id='entryN'>, текст поста тот же, что в полной версии.
  function splitPostsMobile(page) {
    const out = [], re = /<div class='topic_reply[^']*' id='entry(\d+)'>/g;
    let m;
    while ((m = re.exec(page))) {
      const start = m.index + m[0].length;
      const nxt = page.indexOf("<div class='topic_reply", start);
      const block = page.slice(start, nxt !== -1 ? nxt : page.length);
      const author = block.match(/showuser=(\d+)/);
      const date = block.match(/<span class='subtext'>([^<]+)/);
      const b = block.match(/<div id="post-\d+" class='post[^']*'>/);
      let body = b ? block.slice(b.index + b[0].length) : "";
      body = body.split(/<div id="post-\d+-controls"/)[0];
      out.push([m[1], author ? author[1] : "", date ? date[1].trim() : "", body]);
    }
    return out;
  }

  function splitPosts(page) {                      // [(post_id, author_id, date, body_html)]
    if (!page.includes("<div class='post_block") && page.includes("<div class='topic_reply")) return splitPostsMobile(page);
    const out = [], re = /<div class='post_block[^']*' id='post_id_(\d+)'>/g;
    let m;
    while ((m = re.exec(page))) {
      const start = m.index + m[0].length;
      const nxt = page.indexOf("<div class='post_block", start);
      const block = page.slice(start, nxt !== -1 ? nxt : page.length);
      const author = block.match(/hovercard-id="(\d+)"/);
      const date = block.match(/class="published"[^>]*title=["']([^"']+)/);
      let body = "";
      const k = block.indexOf('itemprop="commentText"');
      if (k >= 0) {
        const rest = block.slice(k + 'itemprop="commentText"'.length), g = rest.indexOf(">");
        body = g >= 0 ? rest.slice(g + 1) : rest;
      }
      body = body.split(/<!--\s*(?:\/post|Post end)|<div class='signature'|<ul id='postControlsNormal/)[0];
      out.push([m[1], author ? author[1] : "", date ? date[1] : "", body]);
    }
    return out;
  }

  function bodyLines(body) {                       // текст поста построчно; картинка — «[img URL]»
    let b = body.replace(/<script[\s\S]*?<\/script>/gi, "");
    b = b.replace(/<img[^>]*src=["']([^"']+)["'][^>]*>/g, "\n[img $1]\n");
    b = b.replace(/<a[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi, "$2 <<$1>>");
    b = b.replace(/<br\s*\/?>|<\/p>|<\/li>|<\/div>|<\/h\d>/gi, "\n");
    b = b.replace(/<(?:strike|s|del)(?:\s[^>]*)?>/gi, "\x01");
    b = b.replace(/<\/(?:strike|s|del)>/gi, "\x02");
    b = unescapeHtml(b.replace(/<[^>]+>/g, "")).replace(/ /g, " ");
    const lines = [];
    let struck = false;
    for (const raw of b.split("\n")) {
      let out = struck ? "~~" : "";
      for (const ch of raw) {
        if (ch === "\x01") { out += struck ? "" : "~~"; struck = true; }
        else if (ch === "\x02") { out += struck ? "~~" : ""; struck = false; }
        else out += ch;
      }
      if (struck) out += "~~";
      let ln = out.replace(/\s+/gu, " ").trim();
      ln = ln.replace(/~~\s*~~/gu, " ").trim();
      if (ln.replace(/~~/g, "").trim().startsWith("[img ")) ln = ln.replace(/~~/g, "").trim();
      if (ln && !ln.startsWith("commentText")) lines.push(lstripChars(ln, "'> ").trim());
    }
    return lines.filter((ln) => ln.replace(/~~/g, "").trim());
  }

  function isVariant(ln) {                          // «4000W - 330 с», «(SMD) - 20 с»
    if (!PRICE_1.test(ln)) return false;
    const name = stripChars(ln.split(priceG())[0], " -–—:");
    return !name || name.length <= 14 || (name.length <= 30 && /^[\d(]/.test(name));
  }

  function quote(t) {                              // urllib.parse.quote(t, safe="") + «-» → %2D
    return encodeURIComponent(t).replace(/[!'()*]/g, (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase())
      .replace(/-/g, "%2D");
  }

  function exactUrl(topic, st, postId, group, name) {
    const cut = (t, n = 48) => {
      let out = "";
      for (const w of t.split(/\s+/u).filter(Boolean)) {
        if (out && out.length + 1 + w.length > n) break;
        out = `${out} ${w}`.trim();
      }
      return out;
    };
    const frag = group ? `${quote(cut(group))},${quote(cut(name, 32))}` : quote(cut(name));
    return `${BASE}?showtopic=${topic}` + (st ? `&st=${st}` : "") + `#entry${postId}:~:text=${frag}`;
  }

  function parseItems(linesIn, topic, title, postId, date, st) {
    const items = [];
    let sinceImg = [], current = null, group = "", groupExtra = [], imgRun = [], tail = [], lastGroup = "";
    let groupOut = false, struckText = [], fully = false, struckPrices = [];
    const clean = (x) => x.replace(/\s*<<[^>]+>>/g, "").trim();
    const makeItem = (full, ln, prices, grp, name, details) => {
      const gone = prices.filter((p) => struckPrices.includes(p));
      const left0 = prices.filter((p) => !gone.includes(p));
      const left = left0.length ? left0 : prices;
      const it = {
        id: sha1hex(`${topic}|${postId}|${full}|${ln}`).slice(0, 10),
        name: full, line: ln, group: grp, prices, price: Math.min(...left), prices_out: gone,
        out: fully || groupOut || gone.length === prices.length,
        struck_parts: fully ? [] : struckText.filter((x) => !PRICE_ONLY.test(x)),
        details, images: [], links: [], topic, topic_title: title, post_id: postId, date, st,
        url: `${BASE}?showtopic=${topic}&view=findpost&p=${postId}`,
        url_exact: exactUrl(topic, st, postId, grp, name),
      };
      items.push(it);
      sinceImg.push(it);
      return it;
    };
    const filtered = linesIn.filter((ln) => !["Скрытый текст", "Спойлер"].includes(ln.replace(/~~/g, "")));
    const rawLines = filtered, lines = filtered.map((ln) => ln.replace(/~~/g, "").trim());
    for (let k = 0; k < lines.length; k++) {
      const ln = lines[k], raw = rawLines[k];
      struckText = [...raw.matchAll(/~~(.*?)~~/g)].map((m) => m[1].trim()).filter(Boolean);
      fully = struckText.length > 0 && !WORD.test(raw.replace(/~~.*?~~/g, ""));
      struckPrices = struckText.flatMap((x) => [...x.matchAll(STRUCK_PRICE)].map((m) => toInt(m[1])));
      if (ln.startsWith("[img ")) {
        const url = ln.slice(5, -1);
        if (url.includes("style_images") || url.includes("emoticons")) continue;
        imgRun.push(url);
        current = null; group = ""; groupExtra = []; tail = []; groupOut = false;
        continue;
      }
      if ((!WORD.test(ln) && !([...ln].length >= 3 && onlySep(ln))) || JUNK.test(ln)) continue;
      if (imgRun.length) {
        if (sinceImg.length) for (const it of sinceImg) it.images = [...imgRun];
        sinceImg = []; imgRun = [];
      }
      if (onlySep(ln)) {
        current = null; group = ""; groupExtra = []; lastGroup = ""; tail = []; groupOut = false;
        continue;
      }
      const prices = [...ln.matchAll(priceG())].map((m) => toInt(m[1]));
      const only = prices.length ? null : ln.match(PRICE_ONLY);
      if (only) {
        const price = [toInt(only[1])];
        if (current === null && group) {
          current = makeItem(group, ln, price, group, group, [...groupExtra]);
          lastGroup = group; groupExtra = []; tail = [];
          continue;
        }
        if (current !== null && tail.length) {
          current.details.splice(current.details.length - tail.length, tail.length);
          const g = current.group;
          const vname = [...tail.join(" ")].slice(0, 100).join("");
          const [full, det] = g ? [`${g} — ${vname}`, []] : [tail[0], tail.slice(1)];
          current = makeItem(full, ln, price, g, g ? vname : tail[0], det);
          tail = [];
          continue;
        }
      }
      if (prices.length) {
        let name = stripChars(ln.split(priceG())[0], " -–—:") || ln;
        if (!group && lastGroup && isVariant(ln)) group = lastGroup;
        lastGroup = group;
        let extra = [...groupExtra];
        const last = extra[extra.length - 1];
        if (group && extra.length && last.length <= 20 && !/\d/.test(last) && isVariant(ln)) {
          name = `${last.replace(/:+$/, "").trim()} ${name}`;
          extra = extra.slice(0, -1);
        }
        current = makeItem(group ? `${group} — ${name}` : name, ln, prices, group, name, extra);
        tail = [];
      } else if (current !== null && isVariant(k + 1 < lines.length ? lines[k + 1] : "")) {
        current = null; group = clean(ln); groupExtra = []; tail = []; groupOut = fully;
      } else if (current === null) {
        if (!group) { group = clean(ln); groupOut = fully; }
        else groupExtra.push(clean(ln));
      } else {
        current.details.push((fully ? "✕ " : "") + clean(ln));
        tail.push(clean(ln));
        current.links.push(...[...ln.matchAll(/<<([^>]+)>>/g)].map((m) => m[1])
          .filter((u) => !u.includes("diesel.elcat.kg/index.php?s=")));
      }
    }
    if (imgRun.length && sinceImg.length) for (const it of sinceImg) it.images = [...imgRun];
    return items;
  }

  // ---------- кеш браузера ----------
  async function cacheGet(key) {
    try {
      const c = await caches.open(CACHE), r = await c.match(key);
      return r ? { text: await r.text(), at: Number(r.headers.get("x-at")) || 0 } : null;
    } catch { return null; }
  }
  async function cachePut(key, text) {
    try {
      const c = await caches.open(CACHE);
      await c.put(key, new Response(text, { headers: { "x-at": String(Date.now()), "content-type": "text/plain;charset=utf-8" } }));
    } catch {}
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function getPage(topic, st, fresh) {
    const url = pageUrl(topic, st);
    if (!fresh) { const c = await cacheGet(url); if (c) return c.text; }
    const r = await fetch(url, { credentials: "omit", cache: "no-store" });
    if (!r.ok) throw new Error(`форум ответил ${r.status}`);
    const text = await r.text();
    await cachePut(url, text);
    await sleep(DELAY_MS);
    return text;
  }

  async function crawlTopic(topic, onPage, fetcher) {
    let title = "", starter = null, items = [], st = 0;
    const links = new Set();
    for (;;) {
      const page = await fetcher(topic, st);
      onPage?.();
      if (!title) {
        const t = page.match(/<title>([^<]*)/);
        title = t ? unescapeHtml(t[1]).split(" - Барахолка")[0].split(" - Diesel Forum")[0] : topic;
      }
      const posts = splitPosts(page);
      if (!posts.length) break;
      if (starter === null) starter = posts[0][1];
      for (const [postId, author, date, body] of posts) {
        if (author !== SELLER) continue;
        for (const m of body.matchAll(/showtopic=(\d+)/g)) links.add(m[1]);
        items = items.concat(parseItems(bodyLines(body), topic, title, postId, date, st));
      }
      if (!new RegExp(`showtopic=${topic}(?:&amp;|&)[^'"]*st=${st + PER_PAGE}\\b`).test(page)) break;
      st += PER_PAGE;
    }
    return { title, starter, items, links };
  }

  // полный обход: темы продавца от стартовой по ссылкам между ними → компактный каталог
  async function crawl(progress, fetcher = (topic, st) => getPage(topic, st, true)) {
    const todo = [...SEED], seen = new Set(), topics = [], items = [];
    let pages = 0;
    while (todo.length) {
      const topic = todo.shift();
      if (seen.has(topic)) continue;
      seen.add(topic);
      let r;
      try { r = await crawlTopic(topic, () => progress?.(++pages), fetcher); }
      catch (e) { if (!topics.length && topic === SEED[0]) throw e; continue; }
      if (r.starter !== SELLER) continue;
      topics.push({ topic, title: r.title, url: `${BASE}?showtopic=${topic}` });
      items.push(...r.items);
      todo.push(...[...r.links].filter((x) => !seen.has(x)).sort());
    }
    const uniq = new Map();
    for (const it of items) if (!uniq.has(it.id)) uniq.set(it.id, it);
    return compact({ crawledAt: Date.now(), topics, items: [...uniq.values()] });
  }

  // тот же формат, что у server.py /api/catalog + где искать строку в посте (st, pi, l)
  function compact(d) {
    const tix = new Map(d.topics.map((t, i) => [t.topic, i]));
    return {
      crawledAt: d.crawledAt,
      topics: d.topics.map((t) => ({ topic: t.topic, title: t.title, url: t.url })),
      items: d.items.map((it) => ({
        id: it.id, n: it.name, g: it.group || "", p: it.prices, d: it.details, t: tix.get(it.topic) ?? -1,
        u: it.url_exact || it.url, im: it.images, o: !!it.out, px: it.prices_out || [], xs: it.struck_parts || [],
        pi: it.post_id, st: it.st || 0, l: it.line,
      })),
    };
  }

  // ---------- каталог: снимок с сайта, кеш браузера, обновление с форума ----------
  async function loadCatalog() {
    const [cached, snap] = await Promise.all([
      cacheGet("catalog").then((c) => (c ? JSON.parse(c.text) : null)).catch(() => null),
      fetch("data/catalog.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    if (cached && (!snap || cached.crawledAt >= snap.crawledAt)) return cached;
    return snap || cached;
  }
  const isStale = (cat) => !cat || Date.now() - cat.crawledAt > MAX_AGE_H * 3600e3;
  async function refresh(progress) {
    const cat = await crawl(progress);
    if (!cat.items.length) throw new Error("на форуме не нашлось товаров");
    await cachePut("catalog", JSON.stringify(cat));
    return cat;
  }

  // ---------- корзина в ссылке: #c=<id>.<id>*2&n=<название>&m=<заметка> ----------
  // id сокращаются до самого короткого префикса, однозначного в текущем каталоге (не короче 6)
  function prefixLen(ids) {
    for (let L = 6; L < 10; L++) if (new Set(ids.map((x) => x.slice(0, L))).size === ids.length) return L;
    return 10;
  }
  function cartToHash(cart, allIds) {
    const L = prefixLen(allIds);
    const items = Object.entries(cart.items).map(([id, q]) => id.slice(0, L) + (q > 1 ? "*" + q : "")).join(".");
    let h = "c=" + items;
    if (cart.name) h += "&n=" + encodeURIComponent(cart.name);
    if (cart.note) h += "&m=" + encodeURIComponent(cart.note);
    return h;
  }
  function hashToCart(hash, allIds) {
    const p = new URLSearchParams(hash.replace(/^#/, ""));
    if (!p.has("c")) return null;
    const items = {};
    for (const part of p.get("c").split(".").filter(Boolean)) {
      const [pre, q] = part.split("*");
      const id = allIds.find((x) => x.startsWith(pre)) || pre;
      items[id] = (items[id] || 0) + Math.max(1, parseInt(q || "1", 10) || 1);
    }
    return { name: p.get("n") || "Корзина по ссылке", note: p.get("m") || "", items };
  }

  return { BASE, SELLER, MAX_AGE_H, pageUrl, splitPosts, bodyLines, parseItems, sha1hex, cacheGet, getPage,
           crawl, compact, loadCatalog, isStale, refresh, cartToHash, hashToCart };
})();
