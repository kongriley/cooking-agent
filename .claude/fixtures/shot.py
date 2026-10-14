"""Screenshot a page with the installed Chrome: shot.py url out.png W H [x y w h] [--dark]; CLICK=<button> to open one first."""
import asyncio, os, sys
from playwright.async_api import async_playwright

async def main():
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    url, out, W, H = a[0], a[1], int(a[2]), int(a[3])
    clip = dict(zip('x y width height'.split(), map(int, a[4:8]))) if len(a) >= 8 else None
    async with async_playwright() as p:
        b = await p.chromium.launch(channel='chrome')
        pg = await b.new_page(viewport={'width': W, 'height': H}, device_scale_factor=2 if clip else 1,
                              color_scheme='dark' if '--dark' in sys.argv else 'light')
        await pg.goto(url); await pg.wait_for_timeout(2500)
        if os.environ.get('CLICK'):
            await pg.get_by_role('button', name=os.environ['CLICK'], exact=True).first.click(); await pg.wait_for_timeout(700)
        await pg.screenshot(path=out, clip=clip)
        await b.close()
asyncio.run(main())
