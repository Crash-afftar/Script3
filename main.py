# Main application entry point and coordinator 
import logging
import logging.handlers
import json
import os
import sys
import signal
import threading
import time
import sqlite3
from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger
import inspect
from telegram.ext import Application

# Імпорт власних модулів
import telegram_monitor
import signal_interpreter
import bingx_client
import data_manager
import re
from position_manager import PositionManager
from typing import Optional

# --- Глобальні змінні --- 
position_manager_instance: Optional[PositionManager] = None
stop_event_main = threading.Event() # Подія для сигналізації про зупинку всім компонентам

# Словник для зберігання пар з каналу 1, для яких очікуємо деталі
pending_channel1_details = {}
# Словник для зберігання пар з каналу 5, для яких очікуємо деталі
pending_channel5_details = {}

# --- Обробник сигналів ОС (Ctrl+C) --- 
def signal_handler(sig, frame):
    logger = logging.getLogger(__name__)
    logger.warning(f"Отримано сигнал {signal.Signals(sig).name}. Ініціюю зупинку...")
    stop_event_main.set() # Встановлюємо подію зупинки

# --- Logging Setup ---
def setup_logging(log_file="bot.log"):
    logger = logging.getLogger()
    # Видаляємо всі попередні обробники, щоб уникнути дублювання
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        
    logger.setLevel(logging.DEBUG) # Встановлюємо рівень DEBUG для детальніших логів у файлі

    # Обробник для консолі (залишаємо INFO рівень)
    logHandlerConsole = logging.StreamHandler(sys.stdout)
    logHandlerConsole.setLevel(logging.INFO)
    formatterConsole = logging.Formatter('%(asctime)s - %(levelname)-8s - %(name)-25s - %(message)s')
    logHandlerConsole.setFormatter(formatterConsole)
    logger.addHandler(logHandlerConsole)

    # Обробник для запису у файл з ротацією
    try:
        logHandlerFile = logging.handlers.TimedRotatingFileHandler(
            filename=log_file, 
            when='midnight',
            interval=1,
            backupCount=7,
            encoding='utf-8'
        )
        logHandlerFile.setLevel(logging.DEBUG)
        formatterFile = logging.Formatter('%(asctime)s - %(levelname)-8s - %(name)-25s - %(message)s')
        logHandlerFile.setFormatter(formatterFile)
        logger.addHandler(logHandlerFile)
        print(f"Логування також ведеться у файл: {log_file}")
    except Exception as e:
        print(f"Помилка при налаштуванні логування у файл {log_file}: {e}. Логування тільки в консоль.")

    return logger

# --- Configuration Loading ---
def load_config(config_path='config.json'):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logging.info(f"Конфігурацію завантажено з {config_path}")
        # Перевірка наявності необхідних секцій
        if 'global_settings' not in config:
             logging.warning("Секція 'global_settings' відсутня в конфігу.")
             config['global_settings'] = {}
        if 'channels' not in config:
             logging.warning("Секція 'channels' відсутня в конфігу.")
             config['channels'] = {}
        if 'position_limits' not in config:
             logging.warning("Секція 'position_limits' відсутня в конфігу.")
             config['position_limits'] = {}
        if 'notifications' not in config:
             logging.warning("Секція 'notifications' відсутня в конфігу.")
             config['notifications'] = {}
             
        return config
    except FileNotFoundError:
        logging.error(f"Файл конфігурації не знайдено: {config_path}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Помилка декодування JSON у файлі: {config_path}")
        return None

# --- Перевірка лімітів слотів ---
def check_slot_availability(config: dict) -> bool:
    """Перевіряє, чи є вільний слот для відкриття позиції.
       Використовує загальний ліміт для всіх каналів.
    """
    logger = logging.getLogger(__name__)
    limits = config.get('position_limits', {})
    limit = limits.get('total_max_open')
    active_count = -1
    conn: Optional[sqlite3.Connection] = None
    available = False # За замовчуванням - не доступно

    if limit is None:
        logger.warning(f"[Slot Check] Не знайдено загальний ліміт 'total_max_open' в конфігу. Дозволено відкриття.")
        return True # Дозволяємо, якщо ліміт не задано

    try:
        # Створюємо тимчасове з'єднання
        conn = data_manager.get_db_connection()
        if not conn:
            logger.error(f"[Slot Check] Не вдалося створити тимчасове з'єднання з БД для перевірки слотів.")
            return False

        # Отримуємо загальну кількість активних позицій (для всіх каналів)
        active_count = data_manager.get_total_active_position_count(conn)

        if active_count == -1: # Помилка отримання даних з БД
            logger.error(f"[Slot Check] Не вдалося отримати загальну кількість активних позицій. Блокуємо відкриття.")
            available = False
        else:
            logger.info(f"[Slot Check] Загальний ліміт: Активних = {active_count}, Ліміт = {limit}")
            if active_count < limit:
                logger.info(f"[Slot Check] Є вільний слот.")
                available = True
            else:
                logger.warning(f"[Slot Check] Загальний ліміт слотів ({limit}) вичерпано. Нова угода не відкривається.")
                available = False

    except sqlite3.Error as db_err:
         logger.error(f"[Slot Check] Помилка БД під час перевірки слотів: {db_err}", exc_info=True)
         available = False
    except Exception as e:
         logger.error(f"[Slot Check] Неочікувана помилка під час перевірки слотів: {e}", exc_info=True)
         available = False
    finally:
        # Гарантовано закриваємо тимчасове з'єднання
        if conn:
            conn.close()
            logger.debug("[Slot Check] Тимчасове з'єднання з БД закрито.")

    return available

# --- Головний обробник повідомлень ---
def handle_new_message(forwarded_channel_title: str, signal_text: str, config: dict, bingx_api_instance: bingx_client.BingXClient):
    """Обробляє переслане повідомлення, отримане від telegram_monitor.

    Args:
        forwarded_channel_title: Назва каналу, з якого переслано повідомлення.
        signal_text: Текст сигналу (з text або caption).
        config: Словник конфігурації.
        bingx_api_instance: Екземпляр BingXClient (може бути None, якщо ініціалізація не вдалась).
    """
    logger = logging.getLogger("MessageHandler")
    
    if not bingx_api_instance:
         logger.error("[MessageHandler] BingX клієнт не доступний. Обробка сигналу неможлива.")
         return

    channel_key, source_name = signal_interpreter.identify_signal_source(forwarded_channel_title, config)
    if not channel_key:
        return
        
    channel_config = config.get('channels', {}).get(channel_key)
    if not channel_config:
        logger.error(f"[MessageHandler] Не знайдено конфігурацію для каналу '{source_name}' (key: {channel_key}).")
        return

    logger.info(f"--- Обробка сигналу від: {source_name} ({channel_key}) ---")
    logger.debug(f"\"\"\"Текст сигналу:\n{signal_text}\n---------------------\"\"\"")

    # --- Перевірка слотів ПЕРЕД парсингом (для всіх каналів) ---
    if not check_slot_availability(config):
         return # Вихід, якщо немає вільних слотів

    # --- Обробка для Каналу 1 (Даніель - двохетапний) ---
    if channel_key == "channel_1":
        # --- Визначення параметрів для каналу 1 ---
        leverage = channel_config.get('leverage', 10)
        entry_percentage = channel_config.get('entry_percentage', 5.0)
        margin_usdt = config.get('global_settings', {}).get('total_bankroll', 100) * (entry_percentage / 100.0)
        tp_distribution = channel_config.get('tp_distribution', [])
        # -----------------------------------------
        
        # Спочатку пробуємо парсити як вхідний сигнал ("Заполняю...")
        entry_data = signal_interpreter.parse_channel_1_entry(signal_text)
        if entry_data:
            pair_raw = entry_data['pair'] # Напр. "INJUSDT"
            direction = entry_data['direction']
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            position_side = direction.upper()
            order_side = 'buy' if position_side == 'LONG' else 'sell'
            
            logger.info(f"[Main C1 Entry] Отримано вхідний сигнал для {pair_raw} ({api_symbol}) {position_side}. Ініціюємо вхід по ринку...")
            market_order_result = bingx_api_instance.place_market_order_basic(
                symbol=api_symbol,
                side=order_side,
                position_side=position_side,
                margin_usdt=margin_usdt,
                leverage=leverage
            )
            if market_order_result and market_order_result.get('id') and market_order_result.get('filled') > 0:
                filled_amount = market_order_result.get('filled')
                market_symbol = market_order_result.get('symbol')
                logger.info(f"[Main C1 Entry] Ринковий ордер виконано. Обсяг: {filled_amount}. Очікуємо деталі сигналу для {market_symbol}...")
                # Зберігаємо дані виконаного ордера для подальшого використання
                pending_channel1_details[market_symbol] = {
                        'position_side': position_side,
                    'initial_amount': filled_amount,
                    'margin_usdt': margin_usdt,
                    'leverage': leverage,
                    'entry_price': market_order_result.get('average', market_order_result.get('price')),
                    'market_order_id': market_order_result.get('id'),
                    'timestamp': time.time() # Додаємо час для можливого очищення старих
                }
            else:
                 logger.error(f"[Main C1 Entry] Не вдалося виконати ринковий ордер для {api_symbol}. Відповідь: {market_order_result}")

        else: # Якщо це не entry сигнал, пробуємо парсити як details
            details_data = signal_interpreter.parse_channel_1_details(signal_text, config)
            if details_data:
                pair_raw = details_data['pair']
                api_symbol_details = bingx_api_instance._format_symbol_for_swap(pair_raw)
                # Шукаємо відповідний запис в pending
                if api_symbol_details in pending_channel1_details:
                    logger.info(f"[Main C1 Details] Знайдено деталі для {api_symbol_details}. Встановлюємо SL/TP...")
                    entry_info = pending_channel1_details.pop(api_symbol_details) # Видаляємо після використання
                    
                    # Параметри з попереднього етапу
                    market_symbol = api_symbol_details # Мають співпадати
                    position_side = entry_info['position_side']
                    initial_amount = entry_info['initial_amount']
                    entry_price = entry_info['entry_price']
                    # leverage, margin_usdt з entry_info для точності, якщо потрібно

                    # Дані з поточного повідомлення (details)
                    sl_price = details_data.get('stop_loss') 
                    tp_prices = details_data.get('take_profits', [])

                    # Розміщення SL
                    sl_order = None
                    if sl_price is not None:
                        sl_order = bingx_api_instance.set_stop_loss(
                            symbol=market_symbol,
                            position_side=position_side,
                            sl_price=sl_price,
                            amount=initial_amount
                        )
                        if not sl_order or not sl_order.get('id'):
                            logger.error(f"[Main C1 Details] Не вдалося розмістити SL ордер для {market_symbol}. SL Ціна: {sl_price}. Відповідь: {sl_order}")
                            sl_order = None # Reset to None if failed

                    # Оновлено розміщення TP за допомогою set_take_profits
                    tp_orders = []
                    if tp_prices and initial_amount > 0 and tp_distribution:
                        logger.info(f"[Main C1 Details] Спроба встановити {len(tp_prices)} TP ордер(ів) для {market_symbol}...")
                        tp_orders = bingx_api_instance.set_take_profits(
                            symbol=market_symbol,
                            position_side=position_side,
                            take_profit_prices=tp_prices,
                            tp_distribution=tp_distribution,
                            initial_amount=initial_amount
                        )
                        if not tp_orders or len(tp_orders) != len(tp_prices):
                            logger.error(f"[Main C1 Details] Не вдалося створити повний набір ({len(tp_orders)}/{len(tp_prices)}) TP ордерів для {market_symbol}. TP Ціни: {tp_prices}. Відповідь: {tp_orders}")
                            tp_orders = [] # Reset TP orders if any failed
                        else:
                            logger.info(f"[Main C1 Details] Успішно створено {len(tp_orders)} TP ордер(ів) для {market_symbol}.")
                    elif tp_prices:
                        logger.warning(f"[Main C1 Details] TP ціни ({tp_prices}) є, але або обсяг ({initial_amount}) нульовий, або tp_distribution ({tp_distribution}) порожній. TP не встановлюються.")
                    
                    # ЗАПИС В БАЗУ ДАНИХ
                    if sl_order and tp_orders and len(tp_orders) == len(tp_prices):
                        conn_add: Optional[sqlite3.Connection] = None
                        try:
                            conn_add = data_manager.get_db_connection()
                            if not conn_add:
                                logger.critical(f"[Main C1 Details] Не вдалося створити з'єднання з БД для збереження позиції {market_symbol}!")
                                # Скасування створених ордерів
                                cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                                if cancel_ids:
                                    bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                            else:
                                position_data_to_db = {
                                    'signal_channel_key': channel_key,
                                    'symbol': market_symbol,
                                    'position_side': position_side,
                                    'entry_price': entry_price,
                                    'initial_amount': initial_amount,
                                    'current_amount': initial_amount,
                                    'initial_margin': entry_info['margin_usdt'],
                                    'leverage': entry_info['leverage'],
                                    'sl_order_id': sl_order['id'],
                                    'tp_order_ids': [tp['id'] for tp in tp_orders],
                                    'related_limit_order_id': None,
                                    'is_breakeven': 0,
                                    'is_active': 1
                                }
                                new_pos_id = data_manager.add_new_position(conn_add, position_data_to_db)
                                if new_pos_id:
                                    logger.info(f"[Main C1 Details] Позиція {market_symbol} ({position_side}) успішно збережена в БД з ID {new_pos_id}.")
                                    # Перевіряємо ще раз і видаляємо, якщо ключ досі існує
                                    if api_symbol_details in pending_channel1_details:
                                        del pending_channel1_details[api_symbol_details]
                                else:
                                    logger.error(f"[Main C1 Details] Не вдалося зберегти позицію {market_symbol} в БД!")
                                    # Скасування створених ордерів
                                    cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                                    if cancel_ids:
                                        bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                        except sqlite3.Error as db_err:
                            logger.error(f"[Main C1 Details] Помилка БД при збереженні позиції {market_symbol}: {db_err}", exc_info=True)
                            # Скасування створених ордерів
                            cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                            if cancel_ids:
                                bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                        finally:
                            if conn_add:
                                conn_add.close()
                    else: # Відступ 24 (відповідає if sl_order and tp_orders...)
                        logger.error(f"[Main C1 Details] Не вдалося створити повний набір SL/TP ордерів для {market_symbol}. SL: {bool(sl_order)}, TP: {len(tp_orders)}/{len(tp_prices)}. Збереження в БД скасовано.")
                        # Скасування SL, якщо він був створений
                        if sl_order and sl_order.get('id'):
                            logger.warning(f"[Main C1 Details] Скасування частково створеного SL ордера: {sl_order['id']}")
                            bingx_api_instance.cancel_order(market_symbol, sl_order['id'])
                else: # Відступ 20 (відповідає if api_symbol_details...)
                    logger.warning(f"[Main C1 Details] Отримано деталі для {api_symbol_details}, але немає відповідного запису в pending_channel1_details.")
            else: # Відступ 16 (відповідає if details_data:)
                logger.debug(f"[Main C1] Повідомлення не розпізнано як 'details' для каналу 1 (після перевірки entry).") # Повідомлення, якщо details_data не отримано
    # else: # Відступ 8 (відповідає if entry_data:)
    #     logger.debug(f"[Main C1] Повідомлення не розпізнано як 'entry' для каналу 1.")

    # --- Обробка для Каналу 2 (Мартин - повний сигнал) ---

    logger.info(f"--- Завершено обробку сигналу від: {source_name} ({channel_key}) ---")

# --- Основна частина програми --- 
def main():
    global position_manager_instance
    # Додаємо глобальні змінні для потоку та application Telegram
    telegram_application: Optional[Application] = None
    telegram_thread: Optional[threading.Thread] = None

    logger = setup_logging()
    logger.info("===== Запуск торгового бота ====")

    config = load_config()
    if not config:
        logger.critical("Не вдалося завантажити конфігурацію. Завершення роботи.")
        sys.exit(1)

    # Завантаження змінних середовища
    dotenv_loaded = load_dotenv()
    logger.info(f"Результат load_dotenv(): {dotenv_loaded}")
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_target_chat_id_str = os.getenv('TELEGRAM_TARGET_CHAT_ID')

    if not all([api_key, api_secret]):
        logger.critical("API ключі BingX не знайдено. Перевірте BINGX_API_KEY та BINGX_API_SECRET в .env")
        sys.exit(1)
    if not telegram_bot_token:
        logger.critical("Не знайдено токен Telegram бота. Перевірте TELEGRAM_BOT_TOKEN в .env")
        sys.exit(1)
    if not telegram_target_chat_id_str:
        logger.critical("Не знайдено ID цільового чату Telegram. Перевірте TELEGRAM_TARGET_CHAT_ID в .env")
        sys.exit(1)

    try:
        telegram_target_chat_id = int(telegram_target_chat_id_str)
    except ValueError:
        logger.critical(f"TELEGRAM_TARGET_CHAT_ID ('{telegram_target_chat_id_str}') не є валідним числом.")
        sys.exit(1)

    logger.info("API ключі BingX, токен Telegram бота та ID цільового чату завантажено.")

    # ІНІЦІАЛІЗАЦІЯ БД
    db_conn_init_check = None
    try:
        logger.info("Ініціалізація бази даних (перевірка/створення таблиці)...")
        db_conn_init_check = data_manager.get_db_connection()
        if not db_conn_init_check:
            logger.critical("Не вдалося підключитися до бази даних для ініціалізації. Завершення роботи.")
            sys.exit(1)
        if not data_manager.initialize_database(db_conn_init_check):
            logger.critical("Не вдалося ініціалізувати таблиці бази даних. Завершення роботи.")
            sys.exit(1)
        logger.info("База даних успішно ініціалізована (або вже існувала).")
    except Exception as db_init_err:
         logger.critical(f"Неочікувана помилка при ініціалізації БД: {db_init_err}", exc_info=True)
         sys.exit(1)
    finally:
         if db_conn_init_check:
             db_conn_init_check.close()
             logger.info("Тимчасове з'єднання для ініціалізації БД закрито.")

    # Ініціалізація BingX API клієнта
    bingx_api = None
    try:
        logger.info("Ініціалізація BingX API клієнта...")
        bingx_api = bingx_client.BingXClient(api_key, api_secret, logger)
        logger.info("BingX API клієнт успішно ініціалізовано.")
    except Exception as e:
        logger.critical(f"Критична помилка при ініціалізації BingX API: {e}", exc_info=True)
        sys.exit(1)

    # Ініціалізація та запуск PositionManager
    try:
        logger.info("Ініціалізація PositionManager...")
        position_manager_instance = PositionManager(bingx_api, config)
        position_manager_instance.start_monitoring()
        logger.info("PositionManager успішно запущено.")
    except Exception as e:
        logger.critical(f"Критична помилка при ініціалізації PositionManager: {e}", exc_info=True)
        sys.exit(1)

    # Налаштування обробки сигналів ОС
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info("Обробники сигналів SIGINT та SIGTERM встановлено.")

    # Функція-обгортка message_handler_wrapper
    def message_handler_wrapper(forwarded_channel_title, signal_text):
        try:
             handle_new_message(forwarded_channel_title, signal_text, config, bingx_api)
        except Exception as handler_err:
             logger.error(f"Неочікувана помилка всередині message_handler_wrapper: {handler_err}", exc_info=True)

    # --- Оновлений запуск Telegram монітора ---
    logger.info("Створення та налаштування Telegram Application...")
    try:
        # 1. Отримуємо налаштований об'єкт application
        telegram_application = telegram_monitor.start_monitoring(
            token=telegram_bot_token,
            config=config,
            target_chat_id=telegram_target_chat_id,
            main_message_handler=message_handler_wrapper
        )

        if not telegram_application:
            logger.critical("Не вдалося створити Telegram Application. Завершення роботи.")
            # Зупиняємо Position Manager, якщо він вже запущений
            if position_manager_instance:
                 position_manager_instance.stop_monitoring()
            sys.exit(1)

        logger.info("Telegram Application успішно створено.")

        # 2. Створюємо та запускаємо потік для polling
        logger.info("Створення потоку для Telegram polling...")
        telegram_thread = threading.Thread(
            target=telegram_monitor.run_telegram_polling,
            args=(telegram_application,),
            name="TelegramPollingThread",
            daemon=True # Робимо потік демоном
        )
        telegram_thread.start()
        logger.info("Потік для Telegram polling запущено.")

        # --- Основний цикл очікування ---
        logger.info("Бот працює. Очікування сигналу зупинки (Ctrl+C)...")
        while not stop_event_main.is_set():
             # Перевірка стану PositionManager
             if position_manager_instance and position_manager_instance.thread and not position_manager_instance.thread.is_alive():
                 logger.warning("Потік Position Manager неактивний! Можливо, сталася помилка.")
                 # TODO: Додати логіку перезапуску?

             # Перевірка стану потоку Telegram
             if telegram_thread and not telegram_thread.is_alive():
                  logger.warning("Потік Telegram Polling неактивний! Можливо, сталася помилка або він завершився сам.")
                  stop_event_main.set() # Ініціюємо зупинку, якщо потік Telegram впав
                  break # Виходимо з циклу очікування

             time.sleep(5) # Невелике очікування

        logger.info("Головний цикл завершено через подію зупинки.")

    except Exception as e:
        logger.critical(f"Критична помилка під час запуску компонентів: {e}", exc_info=True)
        stop_event_main.set() # Сигналізуємо про зупинку іншим компонентам
    finally:
        # --- Оновлена процедура завершення роботи ---
        logger.info("===== Початок процедури завершення роботи бота ====")
        
        # Переконуємось, що подія зупинки встановлена
        if not stop_event_main.is_set():
             logger.warning("Блок finally в main досягнуто без встановленої події зупинки. Встановлюємо її примусово.")
             stop_event_main.set()
             
        # 1. Зупиняємо PositionManager
        if position_manager_instance:
            logger.info("Зупинка PositionManager...")
            position_manager_instance.stop_monitoring()
            logger.info("PositionManager зупинено.")
            
        # 2. Зупиняємо Telegram Monitor - тепер покладаємось на обробку сигналу в run_polling
        logger.info("Telegram Monitor мав би зупинитися через сигнал ОС або завершення run_polling.")
        # Прибираємо явний виклик stop(), бо він асинхронний і некоректний тут
        # if telegram_application:
        #      try:
        #          logger.info("Виклик application.stop() (для python-telegram-bot < 20)...")
        #          if hasattr(telegram_application, 'stop') and callable(telegram_application.stop):
        #              telegram_application.stop()
        #              logger.info("application.stop() викликано.")
        #          else:
        #              logger.info("Метод application.stop() не знайдено або не викликається.")
        #      except Exception as stop_err:
        #          logger.error(f"Помилка під час спроби викликати stop для Telegram: {stop_err}")
        
        # 3. Очікуємо завершення потоку Telegram
        if telegram_thread and telegram_thread.is_alive():
            logger.info(f"Очікування завершення потоку Telegram ({telegram_thread.name})...")
            telegram_thread.join(timeout=10.0) # Чекаємо до 10 секунд
            if telegram_thread.is_alive():
                logger.warning(f"Потік Telegram ({telegram_thread.name}) не завершився за 10 секунд!")
            else:
                logger.info(f"Потік Telegram ({telegram_thread.name}) успішно завершено.")
        elif telegram_thread:
             logger.info(f"Потік Telegram ({telegram_thread.name}) вже був завершений.")
        else:
             logger.info("Потік Telegram не було запущено.")
            
        logger.info("===== Завершення роботи бота ====")
        print("Бот завершив роботу.")

# Переконайтесь, що цей рядок є в кінці файлу
if __name__ == "__main__":
    main()