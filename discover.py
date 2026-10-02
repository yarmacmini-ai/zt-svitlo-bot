"""Розвідка API обленерго. Працює і локально, і на Railway: усе пишеться в лог.

Показує: чи відкривається сайт з цього сервера (перевірка геоблокування),
усі запити сторінки до API і вміст JSON-відповідей.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from source import USER_AGENT, ZTOE_URL

MAX_BODY = 4000  # символів з кожної відповіді в лог


async def main():
    out = Path("discovery")
    out.mkdir(exist_ok=True)
    json_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(user_agent=USER_AGENT)

        async def on_response(resp):
            nonlocal json_count
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            ctype = resp.headers.get("content-type") or ""
            print(f"[REQ] {resp.status} {resp.request.method} {resp.url} ({ctype})", flush=True)
            if "json" in ctype:
                try:
                    data = await resp.json()
                except Exception as e:
                    print(f"  не JSON: {e}", flush=True)
                    return
                text = json.dumps(data, ensure_ascii=False)
                (out / f"{json_count:02d}.json").write_text(
                    json.dumps({"url": resp.url, "data": data}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                json_count += 1
                print(f"[JSON] {resp.url}\n{text[:MAX_BODY]}"
                      f"{' …(обрізано)' if len(text) > MAX_BODY else ''}", flush=True)

        page.on("response", on_response)
        try:
            main_resp = await page.goto(ZTOE_URL, wait_until="networkidle", timeout=90_000)
            print(f"[PAGE] статус головної сторінки: {main_resp.status if main_resp else 'немає'}",
                  flush=True)
        except Exception as e:
            print(f"[PAGE] сайт не відкрився: {e!r} — можливе геоблокування", flush=True)
        await page.wait_for_timeout(5000)
        title = await page.title()
        body = (await page.inner_text("body"))[:1500]
        print(f"[PAGE] заголовок: {title}\n[PAGE] текст сторінки:\n{body}", flush=True)
        await browser.close()

    print(f"[DONE] JSON-відповідей: {json_count}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
