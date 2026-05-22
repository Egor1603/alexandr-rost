#!/usr/bin/env python3
"""
Парсит стихи с stihi.ru через Playwright (настоящий браузер).
Запускается локально и через GitHub Actions.

Локально:
  pip install playwright
  playwright install chromium
  python scripts/fetch_poems.py
"""

import asyncio, json, re, sys
from pathlib import Path
from playwright.async_api import async_playwright

ROOT       = Path(__file__).parent.parent
POEMS_JS   = ROOT / "poems.js"
CACHE_FILE = Path(__file__).parent / "poems_cache.json"

AUTHOR_URL = "https://stihi.ru/avtor/nebog"
BASE_URL   = "https://stihi.ru"

# ── Кэш ──────────────────────────────────────────────────────────────────────
def load_cache() -> dict:
    if CACHE_FILE.exists():
        items = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return {i["path"]: i for i in items}
    return {}

def save_cache(cache: dict):
    CACHE_FILE.write_text(
        json.dumps(list(cache.values()), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ── Запись poems.js ───────────────────────────────────────────────────────────
def write_poems_js(poems: list):
    lines = [
        "// poems.js — стихотворения Александра Роста",
        "// Генерируется автоматически. Не редактировать руками.",
        "// Чтобы добавить стихотворение вручную — см. README.md",
        "",
        "const POEMS = [",
    ]
    for p in poems:
        title = p["title"].replace("\\","\\\\").replace('"','\\"')
        text  = (p["text"]
                 .replace("\\","\\\\")
                 .replace('"','\\"')
                 .replace("\r","")
                 .replace("\n","\\n"))
        lines.append(f'  {{ date: "{p["date"]}", title: "{title}", text: "{text}" }},')
    lines.append("];")
    POEMS_JS.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅  poems.js обновлён ({len(poems)} стихотворений)")

# ── Сбор ссылок со всех страниц автора ───────────────────────────────────────
async def collect_links(page) -> list[tuple[str, str]]:
    """Возвращает [(path, date), ...] в порядке от новых к старым."""
    links = []
    offset = 0
    while True:
        url = AUTHOR_URL if offset == 0 else f"{AUTHOR_URL}&s={offset}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1000)

        # Все ссылки вида /2024/03/26/8975
        anchors = await page.query_selector_all('a[href^="/20"], a[href^="/201"], a[href^="/202"]')
        found = 0
        for a in anchors:
            href = await a.get_attribute("href")
            if not href:
                continue
            m = re.match(r"^/(20\d\d/\d\d/\d\d/\d+)$", href)
            if not m:
                continue
            path = m.group(1)
            # Дата из пути
            parts = path.split("/")
            date  = f"{parts[2]}.{parts[1]}.{parts[0]}"
            if (path, date) not in links:
                links.append((path, date))
                found += 1

        print(f"  Страница offset={offset}: +{found} ссылок (всего {len(links)})")
        if found == 0:
            break
        offset += 50

    return links

# ── Парсинг одного стихотворения ──────────────────────────────────────────────
async def parse_poem(page, path: str) -> dict:
    url = f"{BASE_URL}/{path}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(500)

    # Заголовок
    h1 = await page.query_selector("h1")
    title = (await h1.inner_text()).strip() if h1 else ""

    # Текст стихотворения
    # На stihi.ru текст идёт после <em> с именем автора,
    # до строки с © Copyright. Берём через JavaScript.
    text = await page.evaluate("""() => {
        // Ищем все текстовые узлы внутри body
        const body = document.body;
        const html = body.innerHTML;

        // Находим позицию после авторской em-ссылки
        const afterAuthor = html.indexOf('</em>');
        if (afterAuthor === -1) return '';
        const afterSlice = html.slice(afterAuthor + 5);

        // Находим позицию до копирайта
        const copy = afterSlice.search(/©\s*(Copyright|Все права)/i);
        const raw = copy > -1 ? afterSlice.slice(0, copy) : afterSlice.slice(0, 3000);

        // Конвертируем br в переносы, убираем теги
        const div = document.createElement('div');
        div.innerHTML = raw
            .replace(/<br\s*\/?>/gi, '\n')
            .replace(/<[^>]+>/g, '');
        return div.textContent
            .split('\n')
            .map(l => l.trim())
            .join('\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }""")

    return {"title": title, "text": text or ""}


# ── Главная функция ───────────────────────────────────────────────────────────
async def main():
    cache = load_cache()
    print(f"Кэш: {len(cache)} стихотворений")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        # 1. Собираем все ссылки
        print("\n📋  Собираю список стихотворений...")
        all_links = await collect_links(page)
        print(f"   Всего ссылок: {len(all_links)}")

        # 2. Загружаем только новые
        to_fetch = [(p, d) for p, d in all_links if p not in cache]
        print(f"   Новых: {len(to_fetch)}\n")

        for i, (path, date) in enumerate(to_fetch, 1):
            print(f"[{i}/{len(to_fetch)}] {path} ... ", end="", flush=True)
            try:
                result = await parse_poem(page, path)
                cache[path] = {"path": path, "date": date, **result}
                print(f"✓  {result['title'][:55]}")
            except Exception as e:
                cache[path] = {"path": path, "date": date, "title": "", "text": ""}
                print(f"✗  {e}")
            save_cache(cache)
            await asyncio.sleep(0.8)  # вежливая пауза

        await browser.close()

    # 3. Пишем poems.js в порядке all_links (новые первые)
    ordered = [cache[p] for p, _ in all_links if p in cache]
    write_poems_js(ordered)

    if not to_fetch:
        print("Новых стихотворений нет.")

asyncio.run(main())
