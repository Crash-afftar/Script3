# Module for handling Telegram channel monitoring and message parsing 

import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.constants import ChatType, MessageOriginType
from typing import Callable, Optional # Додаємо Callable та Optional
import threading # Додаємо threading
import asyncio # Додаємо імпорт asyncio

logger = logging.getLogger(__name__)

# Функція, яку буде викликано при отриманні нового повідомлення або посту в каналі
async def post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if not message:
        logger.debug("Оновлення не містить ні channel_post, ні message.")
        return

    # Перевіряємо, чи це переслане повідомлення з каналу
    forwarded_channel_title = None
    if message.forward_origin and message.forward_origin.type == MessageOriginType.CHANNEL:
        if message.forward_origin.chat and message.forward_origin.chat.title:
             forwarded_channel_title = message.forward_origin.chat.title
             logger.debug(f"Повідомлення переслано з каналу: '{forwarded_channel_title}'")
        else:
             logger.warning("Знайдено forward_origin типу CHANNEL, але без назви чату.")
             return # Не можемо визначити джерело
    # Додатково можна перевірити message.forward_from_chat, якщо потрібно
    # elif message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL:
    #     forwarded_channel_title = message.forward_from_chat.title

    if not forwarded_channel_title:
        logger.info("Повідомлення не є пересланим з каналу. Ігнорується.")
        return

    # Визначаємо текст сигналу (з text або caption)
    signal_text = message.text or message.caption
    if not signal_text:
        logger.debug("Переслане повідомлення не містить текстового контенту (ні text, ні caption).")
        return

    chat_id = message.chat_id
    logger.debug(f"Переслане повідомлення з текстовим контентом з чату {chat_id}.")

    # Перевіряємо ID цільового каналу (куди було переслано)
    target_chat_id = context.bot_data.get("target_chat_id")
    if target_chat_id is None:
         logger.error("Не вдалося отримати target_chat_id з контексту бота.")
         return

    if chat_id != target_chat_id:
        logger.debug(f"Пост/повідомлення з нецільового чату {chat_id} (очікувався {target_chat_id}). Ігнорується.")
        return

    # Якщо чат правильний, логуємо та передаємо далі
    logger.info(f"Отримано пересланий пост з каналу '{forwarded_channel_title}' в цільовому чаті ({chat_id}). Текст: {signal_text[:100]}...")

    # Отримуємо функцію-обробник з контексту
    main_handler = context.bot_data.get("main_message_handler")
    if main_handler:
        try:
            # Передаємо назву джерела та текст сигналу
            main_handler(forwarded_channel_title, signal_text)
        except Exception as e:
            logger.error(f"Помилка під час виклику головного обробника: {e}", exc_info=True)
    else:
        logger.warning("Головний обробник повідомлень не знайдено в контексті бота.")

def start_monitoring(token: str, config: dict, target_chat_id: int, main_message_handler: Callable) -> Optional[Application]:
    """Створює та налаштовує екземпляр Application для Telegram бота.
    
    Args:
        token: Токен Telegram бота.
        config: Словник конфігурації.
        target_chat_id: ID цільового чату/каналу для моніторингу.
        main_message_handler: Функція, яку потрібно викликати при отриманні нового повідомлення.
        
    Returns:
        Налаштований об'єкт Application або None у разі помилки.
    """
    try:
        logger.info(f"Ініціалізація Application для Telegram монітора (чат {target_chat_id})...")
        application = Application.builder().token(token).build()

        # Зберігаємо необхідні дані в контексті бота
        application.bot_data["main_message_handler"] = main_message_handler
        application.bot_data["config"] = config
        application.bot_data["target_chat_id"] = target_chat_id

        # Додаємо обробник повідомлень
        handler = MessageHandler((filters.TEXT | filters.CAPTION) & filters.UpdateType.CHANNEL_POST, post_handler)
        application.add_handler(handler)
        
        logger.info("Екземпляр Application для Telegram успішно створено та налаштовано.")
        return application
        
    except Exception as e:
        logger.critical(f"Помилка під час створення/налаштування Telegram Application: {e}", exc_info=True)
        return None

# --- ОНОВЛЕНА ФУНКЦІЯ ДЛЯ ЗАПУСКУ В ОКРЕМОМУ ПОТОЦІ ---
def run_telegram_polling(application: Application, stop_event: Optional[threading.Event] = None):
    """Запускає polling для переданого Application в окремому потоці,
       створюючи та керуючи власним циклом подій asyncio.
    Args:
        application: Налаштований об'єкт Application.
        stop_event: (Не використовується run_polling напряму, але може знадобитися для інших підходів).
    """
    if not application:
        logger.error("[run_telegram_polling] Передано невалідний об'єкт application.")
        return

    # Створюємо новий цикл подій для цього потоку
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.info(f"[run_telegram_polling] Створено та встановлено новий цикл подій asyncio для потоку {threading.current_thread().name}.")

    try:
        logger.info("[run_telegram_polling] Запуск Telegram монітора (application.run_polling())...")
        # Запускаємо polling всередині створеного циклу подій
        # run_polling тепер має знайти поточний цикл подій
        application.run_polling()
        logger.info("[run_telegram_polling] application.run_polling() завершено.")
    except Exception as e:
        logger.error(f"[run_telegram_polling] Помилка під час роботи run_polling: {e}", exc_info=True)
    finally:
        # Гарантовано закриваємо цикл подій при виході з потоку
        logger.info(f"[run_telegram_polling] Закриття циклу подій asyncio для потоку {threading.current_thread().name}.")
        loop.close()
        logger.info(f"[run_telegram_polling] Потік polling завершує роботу.")

# Функція для явної зупинки (може знадобитися в майбутньому, поки не використовуємо)
# async def stop_telegram_polling(application: Application):
#     if application:
#         logger.info("[stop_telegram_polling] Виклик application.shutdown()...")
#         await application.shutdown()
#         logger.info("[stop_telegram_polling] application.shutdown() завершено.") 