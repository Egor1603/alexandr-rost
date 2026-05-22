#!/usr/bin/env python3
"""
Парсит стихи с stihi.ru через Playwright (настоящий браузер).
Запускается через GitHub Actions и локально.

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
        try:
            items = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return {i["path"]: i for i in items if isinstance(i, dict)}
        except Exception:
            pass
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
        "// Генерируется автоматически через GitHub Actions.",
        "// Чтобы добавить стихотворение вручную — см. README.md",
        "",
        "const POEMS = [",
    ]
    for p in poems:
        title = p.get("title", "").replace("\\","\\\\").replace('"','\\"')
        text  = (p.get("text", "")
                 .replace("\\","\\\\")
                 .replace('"','\\"')
                 .replace("\r","")
                 .replace("\n","\\n"))
        lines.append(f'  {{ date: "{p["date"]}", title: "{title}", text: "{text}" }},')
    lines.append("];")
    POEMS_JS.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅  poems.js записан ({len(poems)} стихотворений)")

# ── Сбор ссылок со страниц автора ────────────────────────────────────────────
async def collect_links(page) -> list[tuple[str, str]]:
    """Возвращает [(path, date), ...] новые первые."""
    seen  = set()
    links = []
    offset = 0

    while True:
        url = AUTHOR_URL if offset == 0 else f"{AUTHOR_URL}&s={offset}"
        print(f"  Страница: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
        except Exception as e:
            print(f"  ⚠ Не удалось загрузить страницу: {e}")
            break

        # Берём HTML и ищем все ссылки на стихи через regex
        # (надёжнее CSS-селектора при нестандартной разметке stihi.ru)
        html = await page.content()
        found_paths = re.findall(r'href="/(20\d\d/\d\d/\d\d/\d+)"', html)

        new_on_page = 0
        for path in found_paths:
            if path in seen:
                continue
            seen.add(path)
            parts = path.split("/")
            date  = f"{parts[2]}.{parts[1]}.{parts[0]}"
            links.append((path, date))
            new_on_page += 1

        print(f"  +{new_on_page} ссылок (всего {len(links)})")

        if new_on_page == 0:
            break
        offset += 50

    return links

# ── Парсинг одного стихотворения ──────────────────────────────────────────────
async def parse_poem(page, path: str) -> dict:
    url = f"{BASE_URL}/{path}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(600)

    # Заголовок — тег h1
    h1 = await page.query_selector("h1")
    title = (await h1.inner_text()).strip() if h1 else ""

    # Текст стихотворения через JS:
    # stihi.ru кладёт текст между <em>Автор</em> и © Copyright
    text = await page.evaluate("""() => {
        const html = document.body.innerHTML;

        // Позиция после первого </em> (это строка с именем автора)
        const start = html.indexOf('</em>');
        if (start === -1) return '';
        let slice = html.slice(start + 5);

        // Обрезаем до копирайта
        const copyIdx = slice.search(/©/);
        if (copyIdx > 0) slice = slice.slice(0, copyIdx);

        // Заменяем <br> на перевод строки, убираем все теги
        const div = document.createElement('div');
        div.innerHTML = slice
            .replace(/<br\\s*\\/?>/gi, '\\n')
            .replace(/<p[^>]*>/gi, '\\n')
            .replace(/<\\/p>/gi, '\\n')
            .replace(/<[^>]+>/g, '');

        return div.textContent
            .split('\\n')
            .map(l => l.trim())
            // Склеиваем: пустая строка → двойной перенос (граница строфы)
            .reduce((acc, line) => {
                if (line === '') {
                    // Добавляем двойной перенос только если последний символ не уже \n\n
                    if (acc && !acc.endsWith('\\n\\n')) acc += '\\n\\n';
                } else {
                    if (acc && !acc.endsWith('\\n')) acc += '\\n';
                    acc += line;
                }
                return acc;
            }, '')
            .replace(/\\n{3,}/g, '\\n\\n')
            .trim();
    }""")

    return {"title": title, "text": text or ""}

# ── Основной поток ────────────────────────────────────────────────────────────
async def main():
    cache = load_cache()
    print(f"Кэш: {len(cache)} стихотворений\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx = await browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        # 1. Собираем список всех стихов
        print("📋  Собираю список стихотворений...")
        all_links = await collect_links(page)
        print(f"\n   Итого ссылок: {len(all_links)}")

        # 2. Загружаем только те, которых нет в кэше
        to_fetch = [(p, d) for p, d in all_links if p not in cache]
        print(f"   Нужно загрузить: {len(to_fetch)}\n")

        for i, (path, date) in enumerate(to_fetch, 1):
            print(f"[{i}/{len(to_fetch)}] {path} … ", end="", flush=True)
            try:
                result = await parse_poem(page, path)
                cache[path] = {"path": path, "date": date, **result}
                print(f"✓  {result['title'][:55] or '(без названия)'}")
            except Exception as e:
                cache[path] = {"path": path, "date": date, "title": "", "text": ""}
                print(f"✗  {e}")
            # Сохраняем кэш после каждого — устойчиво к прерываниям
            save_cache(cache)
            await asyncio.sleep(0.8)

        await browser.close()

    # 3. Собираем итоговый список в оригинальном порядке (новые первые)
    ordered = [cache[p] for p, _ in all_links if p in cache]
    write_poems_js(ordered)

    if not to_fetch:
        print("ℹ️   Новых стихотворений нет — poems.js не изменился.")

asyncio.run(main())
