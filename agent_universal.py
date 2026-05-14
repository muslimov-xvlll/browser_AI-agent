# agent_universal.py
import asyncio
import json
import os
import re
from io import BytesIO
from dotenv import load_dotenv
from google import genai
from google.genai import types
from playwright.async_api import async_playwright
from PIL import Image

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

PRIMARY_MODEL = os.getenv("PRIMARY_AGENT_MODEL")
# FALLBACK_MODELS = os.getenv("FALLBACK_AGENT_MODEL")
USER_DATA_DIR = os.path.join(os.getcwd(), "browser_profile_clean")

PLANNER_PROMPT = """Ты — AI-планировщик для браузерного агента. Составь план из 1-5 небольших шагов для выполнения задачи или выхода из тупика.
План должен быть универсальным, без привязки к CSS-селекторам или URL.
Используй общие формулировки: "открыть объект", "нажать кнопку", "ввести запрос", "прочитать содержимое".
Отвечай ТОЛЬКО JSON: {"plan": ["шаг 1", "шаг 2", ...]}.
"""

EXECUTOR_PROMPT = """Ты — AI-агент-исполнитель в браузере. Твоя задача — выполнить поручение пользователя, следуя плану.
Ты получаешь скриншот всей страницы (включая модальные окна внизу), историю действий и текущий план. Используй инструменты для взаимодействия со страницей.

Инструменты навигации:
- go_to_url(url) — перейти на указанный URL.

Инструменты мыши (все клики — force click с автоматическим fallback на JS):
- left_click(text) — левый клик по элементу, содержащему указанный текст.
- right_click(text) — правый клик по элементу (вызывает контекстное меню). Надёжно работает через принудительное наведение и проверку появления меню.
- hover_over(text) — навести курсор на элемент (без клика). Используй для разведки скрытых элементов.

Инструменты анализа и навигации:
- get_context() — возвращает краткую информацию о текущем контексте страницы: где ты находишься (список писем, просмотр письма, группировка писем и т.д.). Используй, если не уверен, в каком разделе находишься.
- find_element(text) — находит элемент по тексту или aria-label и возвращает его точный CSS-селектор.
- click(selector) — кликает по CSS-селектору (только после find_element).
- type_text(selector, text) — вводит текст в поле.
- scroll(direction, amount)
- wait(seconds)
- ask_user(message)
- finish(message)

УНИВЕРСАЛЬНЫЕ ПРИНЦИПЫ:
1. **Следуй плану.** Если план не срабатывает, попробуй альтернативу.
2. **Не повторяйся.** Запоминай, какие объекты уже обработаны (смотри историю). Не обрабатывай одно и то же дважды.
3. **Выбирай правильный инструмент:**
   - Для перехода на сайт — go_to_url.
   - Для перехода, открытия объекта, ввода текста — left_click.
   - Для вызова контекстного меню — right_click.
   - Для разведки — hover_over.
   - Если не уверен в текущем контексте (например, после клика что-то изменилось, но не так, как ожидалось) — используй get_context.
4. **После деструктивных действий (пометка спамом, удаление, отправка) проверяй, не появилось ли модальное окно подтверждения.** Оно может быть в нижней части страницы. Если видишь такое окно — нажми кнопку подтверждения (например, «Да», «Подтвердить», «ОК»), чтобы завершить действие.
5. **Проверяй результат важных действий.** После клика, который должен изменить состояние (переместить, удалить, пометить), убедись, что ожидаемое изменение произошло. Например, после нажатия «Это спам!» письмо должно исчезнуть из списка или появиться модальное окно. Если изменений нет — действие не сработало, попробуй другой подход.
6. **Если инструмент мыши возвращает ошибку (timeout/not found):**
   - СРАЗУ используй find_element с тем же текстом, чтобы получить точный селектор.
   - Затем используй click с этим селектором.

Всегда используй русский язык в reasoning. Отвечай ТОЛЬКО JSON без комментариев.

Формат ответа:
{
    "reasoning": "твои мысли",
    "action": "right_click",
    "text": "Поделитесь впечатлением",
    ...
}
"""

# ========== Инструменты навигации ==========

async def go_to_url(page, url):
    await page.goto(url, wait_until="domcontentloaded")
    return f"Перешли на {url}"

# ========== Инструменты мыши (надёжный force click + JS fallback) ==========

async def left_click(page, text):
    try:
        locator = page.locator(f"text={text}").first
        if await locator.count() == 0:
            locator = page.locator(f"[aria-label*='{text}']").first
        if await locator.count() > 0:
            try:
                await locator.click(force=True, timeout=3000)
                return "success"
            except Exception:
                result = await page.evaluate("""
                    (text) => {
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (el.textContent?.includes(text) || el.getAttribute('aria-label')?.includes(text)) {
                                el.click();
                                return 'success';
                            }
                        }
                        return 'not found';
                    }
                """, text)
                return result
        return "not found"
    except Exception as e:
        return f"Ошибка левого клика: {e}"

async def right_click(page, text):
    """Правый клик с гарантированным появлением контекстного меню."""
    try:
        locator = page.locator(f"text={text}").first
        if await locator.count() == 0:
            locator = page.locator(f"[aria-label*='{text}']").first
        if await locator.count() > 0:
            # Пробуем Playwright правый клик с force
            try:
                await locator.hover(force=True, timeout=2000)
                await page.wait_for_timeout(300)
                await locator.click(button='right', force=True, timeout=3000)
                await page.wait_for_timeout(500)
                menu = await page.locator('[role="menu"], [role="listbox"], .context-menu, .popup').first
                if await menu.count() > 0 and await menu.is_visible():
                    return "success"
                return "success (но меню не обнаружено)"
            except Exception:
                pass

            # Fallback: программный правый клик через JS с полным циклом событий
            result = await page.evaluate("""
                (text) => {
                    const all = document.querySelectorAll('*');
                    let target = null;
                    for (const el of all) {
                        if (el.textContent?.includes(text) || el.getAttribute('aria-label')?.includes(text)) {
                            target = el;
                            break;
                        }
                    }
                    if (target) {
                        const rect = target.getBoundingClientRect();
                        const x = rect.left + rect.width / 2;
                        const y = rect.top + rect.height / 2;
                        target.dispatchEvent(new PointerEvent('contextmenu', {
                            bubbles: true,
                            clientX: x,
                            clientY: y,
                            button: 2,
                            buttons: 2
                        }));
                        target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 2}));
                        target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 2}));
                        return 'success (JS fallback)';
                    }
                    return 'not found';
                }
            """, text)
            return result
        return "not found"
    except Exception as e:
        return f"Ошибка правого клика: {e}"

async def hover_over(page, text):
    try:
        locator = page.locator(f"text={text}").first
        if await locator.count() == 0:
            locator = page.locator(f"[aria-label*='{text}']").first
        if await locator.count() > 0:
            await locator.hover(force=True, timeout=3000)
            return "success"
        return "not found"
    except Exception as e:
        return f"Ошибка наведения: {e}"

async def get_context(page):
    """Определяет текущий контекст страницы (список, просмотр, группировка)."""
    context_info = await page.evaluate("""
        () => {
            const url = window.location.href;
            const hash = window.location.hash;
            
            if (hash.includes('/message/') || hash.includes('#/message/')) {
                const subjectEl = document.querySelector('[class*="mail-Message-Title"], [class*="MessageHeader"], h1');
                const subject = subjectEl ? subjectEl.innerText.trim() : 'неизвестно';
                return `просмотр письма: "${subject}"`;
            }
            
            if (hash.includes('search') || hash.includes('query')) {
                return 'результаты поиска или группировка писем';
            }
            
            const activeFolder = document.querySelector('[class*="Folder_selected"], [aria-selected="true"]');
            if (activeFolder) {
                const folderName = activeFolder.innerText.trim();
                return `папка "${folderName}"`;
            }
            
            return 'главная страница или список входящих';
        }
    """)
    return context_info

async def find_element(page, text):
    selector = await page.evaluate("""
        (text) => {
            const allElements = document.querySelectorAll('*');
            let target = null;

            for (const el of allElements) {
                if (el.getAttribute('aria-label') === text) {
                    target = el;
                    break;
                }
            }
            if (!target) {
                for (const el of allElements) {
                    if (el.textContent.trim() === text) {
                        target = el;
                        break;
                    }
                }
            }
            if (!target) {
                for (const el of allElements) {
                    if (el.textContent.includes(text) || (el.getAttribute('aria-label') && el.getAttribute('aria-label').includes(text))) {
                        target = el;
                        break;
                    }
                }
            }

            if (!target) return null;

            if (target.id) return `#${target.id}`;
            if (target.getAttribute('data-testid')) return `[data-testid="${target.getAttribute('data-testid')}"]`;
            if (target.getAttribute('aria-labelledby')) return `[aria-labelledby="${target.getAttribute('aria-labelledby')}"]`;
            if (target.className && typeof target.className === 'string') {
                const classes = target.className.trim().split(/\\s+/).filter(c => c.length > 0);
                if (classes.length > 0) return `.${classes[0]}`;
            }
            return target.tagName.toLowerCase();
        }
    """, text)
    return selector if selector else "not found"

async def click_selector(page, selector):
    try:
        locator = page.locator(selector)
        await locator.click(force=True, timeout=3000)
        return f"Клик по '{selector}'"
    except Exception:
        result = await page.evaluate("""
            (selector) => {
                const el = document.querySelector(selector);
                if (el) {
                    el.click();
                    return 'success';
                }
                return 'not found';
            }
        """, selector)
        return f"Клик по '{selector}' (JS fallback: {result})"

async def get_screenshot(page):
    screenshot_bytes = await page.screenshot(full_page=False)
    img = Image.open(BytesIO(screenshot_bytes))
    img = img.resize((1024, 768), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

# ========== Основной цикл ==========

async def main():
    task = input("Введите задачу для агента: ")
    history = []
    processed_emails = []
    plan = []

    print("🧠 Планировщик составляет план...")
    try:
        plan_response = client.models.generate_content(
            model=PRIMARY_MODEL,
            contents=f"Задача: {task}",
            config=types.GenerateContentConfig(
                system_instruction=PLANNER_PROMPT,
                temperature=0.2,
                max_output_tokens=512,
            )
        )
        plan_json = json.loads(re.search(r'\{.*\}', plan_response.text, re.DOTALL).group(0))
        plan = plan_json.get("plan", [])
        print(f"📋 План: {plan}")
    except Exception as e:
        print(f"⚠️ Не удалось составить план: {e}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR, headless=False, viewport={"width": 1280, "height": 1024}
        )
        page = await context.new_page()
        await page.goto("about:blank")

        last_actions = []

        for step in range(25):
            print(f"\n--- Шаг {step+1} ---")
            screenshot_bytes = await get_screenshot(page)

            history_text = "\n".join([f"{i+1}. {h['tool']} | {h['args']} → {h['result']}" for i, h in enumerate(history)])
            plan_text = "План:\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(plan)) if plan else ""
            processed_text = "Уже обработаны объекты: " + ", ".join(processed_emails) if processed_emails else ""

            prompt = f"Задача: {task}\n\n{plan_text}\n{processed_text}\n{history_text}\nURL: {page.url}"

            print("🤖 Исполнитель думает...")
            response = None
            for attempt in range(3):
                try:
                    response = client.models.generate_content(
                        model=PRIMARY_MODEL,
                        contents=[types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"), prompt],
                        config=types.GenerateContentConfig(
                            system_instruction=EXECUTOR_PROMPT,
                            temperature=0.2,
                            max_output_tokens=1024,
                        )
                    )
                    break
                except Exception as e:
                    error_str = str(e)
                    if "500" in error_str or "503" in error_str or "429" in error_str:
                        wait = 3 * (attempt + 1)
                        print(f"⚠️ Ошибка API, попытка {attempt+1}/3, жду {wait}с...")
                        await asyncio.sleep(wait)
                    else:
                        print(f"❌ Ошибка вызова: {e}")
                        await asyncio.sleep(2)

            if not response or not response.text:
                print("❌ Пустой ответ")
                continue

            print(f"📡 Модель: {PRIMARY_MODEL}")
            try:
                text = response.text.strip()
                last_brace = text.rfind('}')
                if last_brace != -1:
                    text = text[:last_brace+1]
                match = re.search(r'\{.*\}', text, re.DOTALL)
                decision = json.loads(match.group(0)) if match else json.loads(text)
                if "action" not in decision:
                    raise ValueError("Нет action")
            except Exception as e:
                print(f"❌ Ошибка JSON: {e}")
                continue

            action = decision.get("action")
            reasoning = decision.get("reasoning", "")
            print(f"Мысль: {reasoning}")

            if action in ("left_click", "right_click", "hover_over"):
                obj_name = decision.get("text", "")
                if obj_name and obj_name not in processed_emails:
                    processed_emails.append(obj_name)

            last_actions.append(action)
            if len(last_actions) > 3:
                last_actions.pop(0)
            if len(last_actions) == 3 and all(a == action for a in last_actions) and action != "finish":
                print("⚠️ Обнаружено зацикливание! Запрашиваю новый план...")
                replan_prompt = f"Задача: {task}\n\n{history_text}\n\nТекущий план зашёл в тупик. Предложи новый план из 1-3 небольших шагов."
                try:
                    replan_response = client.models.generate_content(
                        model=PRIMARY_MODEL,
                        contents=replan_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=PLANNER_PROMPT,
                            temperature=0.2,
                            max_output_tokens=512,
                        )
                    )
                    new_plan_json = json.loads(re.search(r'\{.*\}', replan_response.text, re.DOTALL).group(0))
                    plan = new_plan_json.get("plan", [])
                    print(f"📋 Новый план: {plan}")
                except Exception as e:
                    print(f"⚠️ Не удалось пересмотреть план: {e}")
                last_actions.clear()

            args = {}
            result = ""

            if action == "finish":
                msg = decision.get("message", "Готово")
                print(f"✅ {msg}")
                history.append({"tool": action, "args": "{}", "result": msg})
                break
            elif action == "go_to_url":
                url = decision.get("url")
                if not url: continue
                args = {"url": url}
                result = await go_to_url(page, url)
                print(f"Using tool: go_to_url\nInput: {{\"url\": \"{url}\"}}\nResult: {result}")
            elif action == "left_click":
                text = decision.get("text", "")
                args = {"text": text}
                result = await left_click(page, text)
                print(f"Using tool: left_click\nInput: {{\"text\": \"{text}\"}}\nResult: {result}")
            elif action == "hover_over":
                text = decision.get("text", "")
                args = {"text": text}
                result = await hover_over(page, text)
                print(f"Using tool: hover_over\nInput: {{\"text\": \"{text}\"}}\nResult: {result}")
            elif action == "right_click":
                text = decision.get("text", "")
                args = {"text": text}
                result = await right_click(page, text)
                print(f"Using tool: right_click\nInput: {{\"text\": \"{text}\"}}\nResult: {result}")
            elif action == "get_context":
                args = {}
                result = await get_context(page)
                print(f"Using tool: get_context\nInput: {{}}\nResult: {result}")
            elif action == "find_element":
                text = decision.get("text", "")
                args = {"text": text}
                selector = await find_element(page, text)
                result = selector if selector else "not found"
                print(f"Using tool: find_element\nInput: {{\"text\": \"{text}\"}}\nResult: {result}")
            elif action == "click":
                selector = decision.get("selector")
                if not selector: continue
                args = {"selector": selector}
                result = await click_selector(page, selector)
                print(f"Using tool: click\nInput: {{\"selector\": \"{selector}\"}}\nResult: {result}")
            elif action == "type_text":
                selector = decision.get("selector")
                txt = decision.get("text", "")
                args = {"selector": selector, "text": txt}
                try:
                    await page.fill(selector, txt, timeout=3000)
                    result = f"Введено в '{selector}'"
                except Exception as e:
                    result = f"Ошибка ввода: {e}"
                print(f"Using tool: type_text\nInput: {{\"selector\": \"{selector}\", \"text\": \"{txt}\"}}\nResult: {result}")
            elif action == "scroll":
                direction = decision.get("direction", "down")
                amount = decision.get("amount", 300)
                args = {"direction": direction, "amount": amount}
                await page.evaluate(f"window.scrollBy(0, {amount if direction == 'down' else -amount})")
                result = f"Прокрутка {direction} на {amount}px"
                print(f"Using tool: scroll\nInput: {{\"direction\": \"{direction}\", \"amount\": {amount}}}\nResult: {result}")
            elif action == "wait":
                sec = decision.get("seconds", 2)
                args = {"seconds": sec}
                await asyncio.sleep(sec)
                result = f"Ожидание {sec} сек"
                print(f"Using tool: wait\nInput: {{\"seconds\": {sec}}}\nResult: {result}")
            elif action == "ask_user":
                q = decision.get("message", "Что делать?")
                ans = input(f"❓ Агент: {q}\n> ")
                result = f"Ответ: {ans}"
                print(f"Using tool: ask_user\nInput: {{\"question\": \"{q}\"}}\nResult: {result}")
            else:
                result = f"Неизвестный инструмент: {action}"

            history.append({"tool": action, "args": json.dumps(args, ensure_ascii=False), "result": result[:200]})
            await asyncio.sleep(1)

        print("\n--- Работа агента завершена ---")
        try:
            await asyncio.Future()  # Бесконечное ожидание
        except KeyboardInterrupt:
            print("Завершение работы...")
        await context.close()

if __name__ == "__main__":
    asyncio.run(main())