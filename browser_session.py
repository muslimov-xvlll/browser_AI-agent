import asyncio
from playwright.async_api import async_playwright
import os

# Папка, где будут храниться данные браузера (куки, сессии)
USER_DATA_DIR = os.path.join(os.getcwd(), "browser_profile")

async def main():
    async with async_playwright() as p:
        # launch_persistent_context открывает браузер сразу с контекстом
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,          # видимый браузер
            # channel="chrome",            # используем стандартный Chromium
            args=[
                "--disable-blink-features=AutomationControlled"
            ],
            viewport={"width": 1280, "height": 1024},
        )

        # Получаем первую страницу (она уже есть в контексте)
        page = context.pages[0] if context.pages else await context.new_page()

        # Переходим на какой-нибудь сайт, чтобы убедиться, что всё работает
        await page.goto("https://mail.yandex.ru/")

        print("Браузер открыт. Выполните вход вручную, если нужно.")
        print("Когда закончите, закройте окно браузера или нажмите Ctrl+C в терминале.")

        # Держим программу живой, пока браузер открыт
        try:
            await asyncio.Future()  # бесконечное ожидание
        except KeyboardInterrupt:
            print("Завершение работы...")

        await context.close()

if __name__ == "__main__":
    asyncio.run(main())