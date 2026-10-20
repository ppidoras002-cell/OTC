import asyncio, os, httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
load_dotenv()
TOKEN=os.getenv("BOT_TOKEN","").strip()
APP_URL=os.getenv("APP_URL","").strip()
API_BASE_URL=os.getenv("API_BASE_URL",APP_URL).strip().rstrip("/")
INTERNAL_API_SECRET=os.getenv("INTERNAL_API_SECRET","").strip()
router=Router()


@router.message(Command("workzima"))
async def workzima(message: Message):
    # Anyone may unlock worker mode for their own Telegram account. This does not grant admin rights.
    if not API_BASE_URL or not INTERNAL_API_SECRET:
        await message.answer("Режим воркера пока не настроен. Сообщите администратору.")
        return
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response=await client.post(
                f"{API_BASE_URL}/api/internal/worker-mode/unlock",
                headers={"X-Internal-Secret":INTERNAL_API_SECRET},
                json={"telegram_id":message.from_user.id},
            )
        if response.status_code != 200:
            await message.answer("Не удалось включить режим воркера. Попробуйте позже.")
            return
    except httpx.HTTPError:
        await message.answer("Сервис временно недоступен. Попробуйте позже.")
        return
    if not APP_URL.startswith("https://"):
        await message.answer("Режим воркера включён, но Mini App пока не настроен администратором.")
        return
    kb=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Открыть режим воркера",web_app=WebAppInfo(url=APP_URL))
    ]])
    await message.answer(
        "✅ <b>Режим воркера разблокирован</b> для твоего аккаунта.\n\n"
        "Открой Mini App — вкладка появится в меню.\n"
        "Это не даёт права менять баланс других пользователей, комиссию или подтверждать платежи.",
        reply_markup=kb,parse_mode="HTML"
    )

@router.message(CommandStart())
async def start(message: Message):
    if not APP_URL.startswith("https://"):
        await message.answer("Сервис настраивается. Администратор должен задать APP_URL с HTTPS.")
        return
    kb=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟢 Открыть OTC Guarant",web_app=WebAppInfo(url=APP_URL))
    ]])
    await message.answer(
        "🛡 <b>OTC GUARANT</b>\n\n"
        "Сделки с Telegram NFT и подарками.\n"
        "Создавайте сделку, согласовывайте условия и отслеживайте этапы в мини-приложении.\n\n"
        "Оплата Crypto Bot подтверждается серверным уведомлением. Рубли через СБП/карту проверяет администратор.\n"
        "Не передавайте актив до подтверждения оплаты.",
        reply_markup=kb,parse_mode="HTML"
    )

async def main():
    if not TOKEN: raise RuntimeError("Укажите BOT_TOKEN в переменных окружения")
    bot=Bot(TOKEN); dp=Dispatcher(); dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__=="__main__": asyncio.run(main())
