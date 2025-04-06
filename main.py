# Main application entry point and coordinator 
import logging
import json
import os
import sys # Додано
import signal # Додано
import threading # Додано
import time # Додано
import sqlite3 # Додано
from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger

# Переносимо імпорти, що потенційно можуть викликати конфлікт, ближче до використання
import telegram_monitor
import signal_interpreter
import bingx_client
# import data_manager # <--- Коментуємо тут
import re
from position_manager import PositionManager
from typing import Optional

# --- Глобальні змінні --- 
db_conn: Optional[sqlite3.Connection] = None
position_manager_instance: Optional[PositionManager] = None
stop_event_main = threading.Event() # Подія для сигналізації про зупинку всім компонентам

# Словник для зберігання пар з каналу 1, для яких очікуємо деталі
# Ключ: нормалізована пара (напр., "INJUSDT"), Значення: Словник з даними ордеру
pending_channel1_details = {}

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
    logHandlerConsole = logging.StreamHandler(sys.stdout) # Явно вказуємо stdout
    logHandlerConsole.setLevel(logging.INFO)
    formatterConsole = logging.Formatter('%(asctime)s - %(levelname)-8s - %(name)-25s - %(message)s') # Вирівнювання
    logHandlerConsole.setFormatter(formatterConsole)
    logger.addHandler(logHandlerConsole)

    # Обробник для запису у файл (рівень INFO або DEBUG з конфігу?)
    # Поки що залишаємо INFO
    try:
        logHandlerFile = logging.FileHandler(log_file, encoding='utf-8', mode='a') # Режим 'a' для дозапису
        logHandlerFile.setLevel(logging.INFO)
        # Використовуємо стандартний форматер для файлу, json може бути незручним для читання
        formatterFile = logging.Formatter('%(asctime)s - %(levelname)-8s - %(name)-25s - %(message)s')
        # formatterFile = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
        logHandlerFile.setFormatter(formatterFile)
        logger.addHandler(logHandlerFile)
        print(f"Логування також ведеться у файл: {log_file}") # Повідомлення в консоль
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
        if 'telegram' not in config:
             logging.warning("Секція 'telegram' відсутня в конфігу.")
             config['telegram'] = {}
             
        return config
    except FileNotFoundError:
        logging.error(f"Файл конфігурації не знайдено: {config_path}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Помилка декодування JSON у файлі: {config_path}")
        return None

# --- Перевірка лімітів слотів --- 
def check_slot_availability(channel_key: str, config: dict, db: sqlite3.Connection) -> bool:
    """Перевіряє, чи є вільний слот для відкриття позиції."""
    logger = logging.getLogger(__name__)
    limits = config.get('position_limits', {})
    active_count = -1
    limit = -1
    group_name = ""

    if channel_key in ['channel_1', 'channel_2', 'channel_4']:
        group_name = 'group_1_2_4'
        limit = limits.get('group_1_2_4_max_open')
        if limit is None:
             logger.warning(f"[Slot Check] Не знайдено ліміт 'group_1_2_4_max_open' в конфігу. Дозволено відкриття.")
             return True
        active_count = data_manager.get_active_position_count(db, 'group_1_2_4')
            
    elif channel_key == 'channel_3':
        group_name = 'channel_3'
        # Ліміт для каналу 3 береться з його налаштувань
        limit = config.get('channels', {}).get(channel_key, {}).get('max_open_positions')
        if limit is None:
             logger.warning(f"[Slot Check] Не знайдено ліміт 'max_open_positions' для {channel_key}. Дозволено відкриття.")
             return True
        active_count = data_manager.get_active_position_count(db, 'channel_3')
        
    else:
        # Для невідомих каналів слоти не перевіряємо (але вони і не мають оброблятись)
        logger.warning(f"[Slot Check] Перевірка слотів для невідомого channel_key: {channel_key}. Дозволено.")
        return True

    if active_count == -1: # Помилка отримання даних з БД
         logger.error(f"[Slot Check] Не вдалося отримати кількість активних позицій для групи '{group_name}'. Блокуємо відкриття.")
         return False
         
    logger.info(f"[Slot Check] Група '{group_name}': Активних = {active_count}, Ліміт = {limit}")
    if active_count < limit:
         logger.info(f"[Slot Check] Є вільний слот для групи '{group_name}'.")
         return True
    else:
         logger.warning(f"[Slot Check] Ліміт слотів ({limit}) для групи '{group_name}' вичерпано. Нова угода не відкривається.")
         return False

# --- Головний обробник повідомлень ---
def handle_new_message(forwarded_channel_title: str, signal_text: str, config: dict, bingx_api_instance: bingx_client.BingXClient):
    """Обробляє переслане повідомлення, отримане від telegram_monitor.

    Args:
        forwarded_channel_title: Назва каналу, з якого переслано повідомлення.
        signal_text: Текст сигналу (з text або caption).
        config: Словник конфігурації.
        bingx_api_instance: Екземпляр BingXClient (може бути None, якщо ініціалізація не вдалась).
    """
    logger = logging.getLogger("MessageHandler") # Окремий логер для обробника
    global db_conn # Використовуємо глобальне з'єднання
    
    if not db_conn:
         logger.critical("[MessageHandler] З'єднання з БД недоступне. Обробка сигналу неможлива.")
         return
         
    # Якщо клієнт біржі не передано (або ініціалізація не вдалась), виходимо
    if not bingx_api_instance:
         logger.error("[MessageHandler] BingX клієнт не доступний. Обробка сигналу неможлива.")
         return

    # 1. Визначаємо джерело сигналу та отримуємо конфігурацію каналу
    channel_key, source_name = signal_interpreter.identify_signal_source(forwarded_channel_title, config)
    if not channel_key:
        # Лог про невідомий канал вже є в identify_signal_source
        return 
        
    channel_config = config.get('channels', {}).get(channel_key)
    if not channel_config:
        logger.error(f"[MessageHandler] Не знайдено конфігурацію для каналу '{source_name}' (key: {channel_key}).")
        return

    logger.info(f"--- Обробка сигналу від: {source_name} ({channel_key}) ---")
    logger.debug(f"""Текст сигналу:
{signal_text}
---------------------""")

    # --- Перевірка слотів ПЕРЕД парсингом та обробкою --- 
    if not check_slot_availability(channel_key, config, db_conn):
         # Повідомлення про брак слотів вже залоговано в check_slot_availability
         return

    # --- Отримання спільних параметрів з конфігу --- 
    try:
        leverage = int(channel_config['leverage'])
        entry_percentage = float(channel_config['entry_percentage'])
        tp_distribution = channel_config.get('tp_distribution', []) 
        total_bankroll = float(config.get('global_settings', {}).get('total_bankroll', 0))
        
        if total_bankroll <= 0:
             logger.error(f"[MessageHandler] total_bankroll ({total_bankroll}) має бути позитивним числом.")
             return
             
    except (KeyError, ValueError, TypeError) as config_err:
        logger.error(f"[MessageHandler] Помилка отримання/конвертації параметрів для {source_name}: {config_err}")
        return
        
    # Розрахунок маржі для ордера
    margin_usdt = total_bankroll * (entry_percentage / 100.0)
    logger.info(f"[MessageHandler] Параметри для {source_name}: Плече={leverage}x, Маржа={margin_usdt:.2f} USDT ({entry_percentage}% від {total_bankroll})")

    # --- Обробка для Каналу 1 (дворівневий сигнал) --- 
    if channel_key == "channel_1":
        # Спочатку пробуємо парсити як вхідний сигнал ("Заполняю...")
        entry_data = signal_interpreter.parse_channel_1_entry(signal_text)
        if entry_data:
            pair_raw = entry_data['pair'] # Напр. "INJUSDT"
            direction = entry_data['direction']
            position_side = direction.upper() # LONG або SHORT
            order_side = 'buy' if position_side == 'LONG' else 'sell'
            # Форматуємо пару для API та для ключа в pending_details
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            pending_key = pair_raw # Використовуємо оригінальну пару як ключ
            
            logger.info(f"[Main C1 Entry] Отримано сигнал ВХОДУ для {pending_key} ({api_symbol}) {position_side}. Ініціюємо вхід по ринку...")
            
            # === Розміщення ринкового ордера ===
            market_order_result = bingx_api_instance.place_market_order_basic(
                symbol=api_symbol,
                side=order_side,
                position_side=position_side,
                margin_usdt=margin_usdt,
                leverage=leverage
            )

            # Перевіряємо результат ордера більш ретельно
            if market_order_result and isinstance(market_order_result, dict) and market_order_result.get('id'):
                # Ордер створено, перевіряємо статус (мав би бути 'closed' для market)
                order_status = market_order_result.get('status', 'unknown')
                filled_amount = market_order_result.get('filled')
                market_symbol = market_order_result.get('symbol') # Має збігатися з api_symbol
                order_id = market_order_result.get('id')
                avg_price = market_order_result.get('average', market_order_result.get('price')) # Ціна виконання
                cost = market_order_result.get('cost') # Фактична вартість

                if order_status == 'closed' and filled_amount is not None and filled_amount > 0 and avg_price is not None:
                    logger.info(f"[Main C1 Entry] Ринковий ордер {order_id} для {pending_key} ({market_symbol}) {position_side} УСПІШНО ВИКОНАНО. Обсяг: {filled_amount}, Ціна: {avg_price:.5f}, Вартість: {cost:.2f} USDT")
                    # Зберігаємо інформацію про виконаний ордер для встановлення TP/SL пізніше
                    pending_channel1_details[pending_key] = {
                        'symbol': market_symbol, # Використовуємо символ з відповіді API
                        'position_side': position_side,
                        'entry_price': avg_price, # Зберігаємо ціну виконання
                        'initial_amount': filled_amount, # Зберігаємо обсяг виконання
                        'leverage': leverage,
                        'initial_margin': margin_usdt, # Зберігаємо плановану маржу
                        'market_order_id': order_id,
                        'timestamp': time.time() # Додаємо час для можливої очистки старих записів
                    }
                    logger.info(f"[Main C1 Entry] Позиція {pending_key} додана до очікування деталей TP/SL.")
                else:
                     logger.error(f"[Main C1 Entry] Ринковий ордер {order_id} для {pending_key} створено, але статус ({order_status}) або виконання ({filled_amount}) некоректні. Деталі: {market_order_result}")
                     # Не додаємо до pending_details, якщо ордер не виконався
            else:
                logger.error(f"[Main C1 Entry] Не вдалося розмістити ринковий ордер для {pending_key} ({api_symbol}) {position_side}. Результат: {market_order_result}")
            return # Завершуємо обробку цього повідомлення

        # Якщо це не вхідний сигнал, пробуємо парсити як деталі
        details_data = signal_interpreter.parse_channel_1_details(signal_text, config)
        if details_data:
            pair_raw = details_data['pair']
            stop_loss = details_data['stop_loss']
            take_profits = details_data.get('take_profits', []) # TP може не бути
            pending_key = pair_raw
            logger.info(f"[Main C1 Details] Отримано деталі TP/SL для {pending_key}: SL={stop_loss}, TP={take_profits}")

            # Перевіряємо, чи очікували ми деталі для цієї пари
            if pending_key in pending_channel1_details:
                position_info = pending_channel1_details[pending_key]
                logger.info(f"[Main C1 Details] Позиція {pending_key} (MarketOrderID: {position_info['market_order_id']}) очікувала на деталі. Встановлюємо TP/SL...")
                
                sl_order = None
                tp_orders_result = [] # Список результатів для TP ордерів
                # --- Встановлення Stop Loss --- 
                if stop_loss:
                    logger.info(f"[Main C1 Details] Встановлення SL={stop_loss} для {position_info['symbol']}...")
                    sl_order = bingx_api_instance.set_stop_loss(
                        symbol=position_info['symbol'],
                        position_side=position_info['position_side'],
                        initial_amount=position_info['initial_amount'],
                        stop_loss_price=stop_loss
                    )
                    if not sl_order or not isinstance(sl_order, dict) or not sl_order.get('id'):
                         logger.error(f"[Main C1 Details] Не вдалося встановити Stop Loss для {pending_key}. Результат: {sl_order}")
                         # Якщо не вдалося встановити SL, чи варто продовжувати? Поки що так.
                    else:
                         logger.info(f"[Main C1 Details] SL ордер {sl_order.get('id')} успішно встановлено для {pending_key}.")
                else:
                    logger.warning(f"[Main C1 Details] Ціна Stop Loss не знайдена в сигналі для {pending_key}. SL не встановлено.")
                
                # --- Встановлення Take Profit --- 
                if take_profits and tp_distribution and sl_order: # Встановлюємо TP тільки якщо є ціни, дистрибуція І SL вдалося встановити!
                    logger.info(f"[Main C1 Details] Встановлення TP={take_profits} (Дистр: {tp_distribution}) для {position_info['symbol']}...")
                    tp_orders_result = bingx_api_instance.set_take_profits(
                        symbol=position_info['symbol'],
                        position_side=position_info['position_side'],
                        initial_amount=position_info['initial_amount'],
                        take_profit_prices=take_profits,
                        tp_distribution=tp_distribution
                    )
                    if not tp_orders_result:
                         logger.warning(f"[Main C1 Details] Не вдалося встановити один або більше ордерів Take Profit для {pending_key}.")
                elif not take_profits:
                     logger.info(f"[Main C1 Details] Ціни Take Profit не знайдено в сигналі для {pending_key}. TP не встановлено.")
                elif not tp_distribution:
                     logger.warning(f"[Main C1 Details] Не знайдено tp_distribution в конфігу для {channel_key} для встановлення TP.")
                elif not sl_order:
                     logger.warning(f"[Main C1 Details] TP не встановлюються, оскільки не вдалося встановити SL для {pending_key}.")
                
                # --- Збереження позиції в БД --- 
                # Зберігаємо ТІЛЬКИ якщо вдалося встановити і SL, і хоча б один TP (якщо TP були в сигналі)
                all_tp_orders_ok = all(isinstance(o, dict) and o.get('id') for o in tp_orders_result)
                tp_ids_to_save = [o['id'] for o in tp_orders_result if isinstance(o, dict) and o.get('id')] if tp_orders_result else []
                
                can_save_to_db = False
                if sl_order and isinstance(sl_order, dict) and sl_order.get('id'):
                    if not take_profits: # Якщо TP не було в сигналі, зберігаємо тільки з SL
                        can_save_to_db = True
                    elif tp_ids_to_save: # Якщо TP були і хоча б один створено
                         can_save_to_db = True
                
                if can_save_to_db:
                    logger.info(f"[Main C1 Details] Збереження активної позиції {pending_key} в базу даних...")
                    db_data = {
                        'signal_channel_key': channel_key,
                        'symbol': position_info['symbol'],
                        'position_side': position_info['position_side'],
                        'entry_price': position_info['entry_price'],
                        'initial_amount': position_info['initial_amount'],
                        'current_amount': position_info['initial_amount'], # Початковий обсяг
                        'initial_margin': position_info['initial_margin'],
                        'leverage': position_info['leverage'],
                        'sl_order_id': sl_order.get('id'),
                        'tp_order_ids': tp_ids_to_save, # Список ID
                        'related_limit_order_id': None, # Не використовується для C1
                        # is_breakeven=0, is_active=1 - за замовчуванням
                    }
                    position_db_id = data_manager.add_new_position(db_conn, db_data)
                    if position_db_id:
                         logger.info(f"[Main C1 Details] Позиція {pending_key} успішно збережена в БД з ID={position_db_id}.")
                    else:
                         logger.error(f"[Main C1 Details] НЕ ВДАЛОСЯ зберегти позицію {pending_key} в БД! PositionManager не буде її відстежувати.")
                         # TODO: Що робити в цьому випадку? Скасувати SL/TP?
                else:
                     logger.error(f"[Main C1 Details] Позиція {pending_key} не буде збережена в БД, оскільки не вдалося встановити SL або TP ордери.")
                     # TODO: Скасувати вже створені ордери (напр. SL)?
                     # bingx_api_instance.cancel_order(position_info['symbol'], sl_order.get('id'))

                # Видаляємо зі списку очікування незалежно від успіху збереження в БД
                try:
                    del pending_channel1_details[pending_key]
                    logger.info(f"[Main C1 Details] Пара {pending_key} видалена зі списку очікування деталей.")
                except KeyError:
                     logger.warning(f"[Main C1 Details] Спроба видалити {pending_key}, але її вже немає в списку очікування.")
                     
            else:
                logger.warning(f"[Main C1] Повідомлення від {source_name} не розпізнано ні як сигнал входу, ні як деталі.")
                return
        
        logger.warning(f"[Main C1] Повідомлення від {source_name} не розпізнано ні як сигнал входу, ні як деталі.")
        return

    # --- Обробка для інших каналів (однорівневі сигнали) ---
    else:
        logger.info(f"[Main Other] Отримано сигнал від {source_name} (key: {channel_key}). Викликаю парсер...")
        parser_func = getattr(signal_interpreter, f"parse_{channel_key}", None)
        
        if not parser_func or not callable(parser_func):
             logger.error(f"[Main Other] Не знайдено функцію парсера 'parse_{channel_key}' для каналу '{source_name}'")
             return
             
        try:
            signal_data = parser_func(signal_text, config)
            if not signal_data:
                 logger.warning(f"[Main Other] Парсер для каналу {source_name} не зміг розпізнати дані в тексті.")
                 return
                 
            logger.info(f"[Main Other] Розпізнано сигнал від '{source_name}': { {k: v for k, v in signal_data.items() if k not in ['raw_text', 'source', 'source_name', 'type']} }")
            
            # Отримуємо необхідні дані з сигналу
            pair_raw = signal_data.get('pair')
            direction = signal_data.get('direction')
            stop_loss = signal_data.get('stop_loss')
            take_profits = signal_data.get('take_profits', [])
            limit_order_price = signal_data.get('limit_order_price') # Для каналу 3

            if not all([pair_raw, direction, stop_loss]): 
                 logger.error(f"[Main Other] Сигнал від {source_name} не містить обов'язкових полів (pair, direction, stop_loss). Сигнал: {signal_data}")
                 return
                 
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            position_side = direction.upper()
            order_side = 'buy' if position_side == 'LONG' else 'sell'
            
            market_order_result = None
            limit_order_result = None # Для каналу 3
            sl_order = None
            tp_orders_result = []
            position_db_id = None
            entry_price_final = None
            amount_final = None

            # --- Спеціальна логіка для Каналу 3 (Джимми) з ліміткою --- 
            if channel_key == 'channel_3' and limit_order_price is not None:
                logger.info(f"[Main C3 Special] Застосування логіки Market + Limit для {pair_raw} ({api_symbol}) {position_side} з лімітом {limit_order_price}")
                # В ТЗ вказано: Market Order + Limit Order по нижній межі діапазону
                # Поки що реалізуємо тільки Market Order, Limit ігноруємо (або додамо пізніше)
                # TODO: Додати логіку розміщення лімітного ордеру для C3
                logger.warning("[Main C3 Special] Логіка розміщення лімітного ордеру для каналу 3 ще не реалізована. Відкриваємо тільки Market.")
                limit_order_price = None # Тимчасово ігноруємо лімітку

            # --- Розміщення Market Order (для всіх каналів, крім C1 вхід) --- 
            logger.info(f"[Main Other] Розміщення ринкового ордера для {pair_raw} ({api_symbol}) {position_side}...")
            market_order_result = bingx_api_instance.place_market_order_basic(
                symbol=api_symbol,
                side=order_side,
                position_side=position_side,
                margin_usdt=margin_usdt,
                leverage=leverage
            )
            
            if market_order_result and isinstance(market_order_result, dict) and market_order_result.get('id'):
                order_status = market_order_result.get('status', 'unknown')
                filled_amount = market_order_result.get('filled')
                market_symbol = market_order_result.get('symbol') 
                order_id = market_order_result.get('id')
                avg_price = market_order_result.get('average', market_order_result.get('price'))
                cost = market_order_result.get('cost')
                
                if order_status == 'closed' and filled_amount is not None and filled_amount > 0 and avg_price is not None:
                    logger.info(f"[Main Other] Ринковий ордер {order_id} ({market_symbol}) {position_side} УСПІШНО ВИКОНАНО. Обсяг: {filled_amount}, Ціна: {avg_price:.5f}, Вартість: {cost:.2f} USDT")
                    entry_price_final = avg_price
                    amount_final = filled_amount
                    
                    # --- Встановлення Stop Loss --- 
                    logger.info(f"[Main Other] Встановлення SL={stop_loss} для {market_symbol}...")
                    sl_order = bingx_api_instance.set_stop_loss(
                        symbol=market_symbol,
                        position_side=position_side,
                        initial_amount=amount_final,
                        stop_loss_price=stop_loss
                    )
                    if not sl_order or not isinstance(sl_order, dict) or not sl_order.get('id'):
                        logger.error(f"[Main Other] Не вдалося встановити Stop Loss для {pair_raw}. Результат: {sl_order}")
                        # TODO: Скасувати вхідний ордер?
                        return # Зупиняємо обробку, якщо SL не вдалося
                    else:
                         logger.info(f"[Main Other] SL ордер {sl_order.get('id')} успішно встановлено для {pair_raw}.")

                    # --- Встановлення Take Profit --- 
                    if take_profits and tp_distribution:
                        logger.info(f"[Main Other] Встановлення TP={take_profits} (Дистр: {tp_distribution}) для {market_symbol}...")
                        tp_orders_result = bingx_api_instance.set_take_profits(
                            symbol=market_symbol,
                            position_side=position_side,
                            initial_amount=amount_final,
                            take_profit_prices=take_profits,
                            tp_distribution=tp_distribution
                        )
                        if not tp_orders_result:
                            logger.warning(f"[Main Other] Не вдалося встановити один або більше ордерів Take Profit для {pair_raw}.")
                    elif not take_profits:
                        logger.info(f"[Main Other] Ціни Take Profit не знайдено в сигналі для {pair_raw}. TP не встановлено.")
                    elif not tp_distribution:
                        logger.warning(f"[Main Other] Не знайдено tp_distribution в конфігу для {channel_key} для встановлення TP.")
                        
                    # --- Збереження позиції в БД --- 
                    tp_ids_to_save = [o['id'] for o in tp_orders_result if isinstance(o, dict) and o.get('id')] if tp_orders_result else []
                    
                    logger.info(f"[Main Other] Збереження активної позиції {pair_raw} в базу даних...")
                    db_data = {
                        'signal_channel_key': channel_key,
                        'symbol': market_symbol, # Використовуємо символ з API
                        'position_side': position_side,
                        'entry_price': entry_price_final,
                        'initial_amount': amount_final,
                        'current_amount': amount_final, 
                        'initial_margin': margin_usdt, # Орієнтовна маржа
                        'leverage': leverage,
                        'sl_order_id': sl_order.get('id'),
                        'tp_order_ids': tp_ids_to_save,
                        'related_limit_order_id': None # TODO: Оновити, якщо C3 лімітка буде реалізована
                    }
                    position_db_id = data_manager.add_new_position(db_conn, db_data)
                    if position_db_id:
                         logger.info(f"[Main Other] Позиція {pair_raw} успішно збережена в БД з ID={position_db_id}.")
                    else:
                         logger.error(f"[Main Other] НЕ ВДАЛОСЯ зберегти позицію {pair_raw} в БД! PositionManager не буде її відстежувати.")
                         # TODO: Скасувати SL/TP?
                else:
                     logger.error(f"[Main Other] Ринковий ордер {order_id} для {pair_raw} створено, але статус ({order_status}) або виконання ({filled_amount}) некоректні. Деталі: {market_order_result}")
            else:
                logger.error(f"[Main Other] Не вдалося розмістити ринковий ордер для {pair_raw} ({api_symbol}) {position_side}. Результат: {market_order_result}")
                
        except Exception as e:
             logger.error(f"[Main Other] Неочікувана помилка під час обробки сигналу від {source_name}: {e}", exc_info=True)
             # TODO: Подумати про скасування частково розміщених ордерів у разі помилки

    logger.info(f"--- Завершено обробку сигналу від: {source_name} ({channel_key}) ---")

# --- Основна частина програми --- 
def main():
    global db_conn, position_manager_instance # Дозволяємо змінювати глобальні змінні
    
    # Налаштування логування
    logger = setup_logging()
    logger.info("===== Запуск торгового бота ====")

    # Завантаження конфігурації
    config = load_config()
    if not config:
        logger.critical("Не вдалося завантажити конфігурацію. Завершення роботи.")
        sys.exit(1)

    # Завантаження змінних середовища (API ключі)
    dotenv_loaded = load_dotenv() 
    logger.info(f"Результат load_dotenv(): {dotenv_loaded}")
    logger.info("--- Перевірка змінних в os.environ після load_dotenv() ---")
    bingx_vars = {k: v for k, v in os.environ.items() if 'BINGX' in k}
    logger.info(f"Знайдені BINGX змінні: {bingx_vars}")
    logger.info("---------------------------------------------------------")
    logger.info("Спроба отримати ключі через os.environ.get()...")
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    logger.info(f"DEBUG: Ключ з os.environ.get: {'присутній' if api_key else 'ВІДСУТНІЙ'}") 
    logger.info(f"DEBUG: Секрет з os.environ.get: {'присутній' if api_secret else 'ВІДСУТНІЙ'}")
    if not api_key or not api_secret:
        logger.critical("API ключі BingX не знайдено (перевірка через os.environ.get). Завершення роботи.")
        sys.exit(1)

    # --- ІМПОРТ ТА ІНІЦІАЛІЗАЦІЯ БД --- 
    try:
        # Імпортуємо data_manager тут, безпосередньо перед використанням
        import data_manager 
        logger.info("Модуль data_manager успішно імпортовано.")
        # --- ДІАГНОСТИКА: Друкуємо шлях до імпортованого модуля --- 
        try:
            logger.info(f"DEBUG: data_manager.__file__ = {data_manager.__file__}")
        except AttributeError:
            logger.error("DEBUG: Не вдалося отримати data_manager.__file__")
        # ------------------------------------------------------
        
        logger.info("Ініціалізація бази даних...")
        db_conn = data_manager.get_db_connection()
        if not db_conn:
            logger.critical("Не вдалося підключитися до бази даних. Завершення роботи.")
            sys.exit(1)
        if not data_manager.initialize_database(db_conn):
            logger.critical("Не вдалося ініціалізувати таблиці бази даних. Завершення роботи.")
            db_conn.close()
            sys.exit(1)
        logger.info("База даних успішно ініціалізована.")
    except ImportError as imp_err:
         logger.critical(f"Не вдалося імпортувати data_manager: {imp_err}", exc_info=True)
         sys.exit(1)
    except AttributeError as attr_err:
         # Ловимо AttributeError саме тут, щоб побачити контекст
         logger.critical(f"Помилка атрибуту при роботі з data_manager: {attr_err}", exc_info=True)
         if db_conn: db_conn.close()
         sys.exit(1)
    except Exception as db_init_err:
         logger.critical(f"Неочікувана помилка при ініціалізації БД: {db_init_err}", exc_info=True)
         if db_conn: db_conn.close()
         sys.exit(1)
    # ------------------------------------

    # Ініціалізація BingX API клієнта
    bingx_api = None
    try:
        logger.info("Ініціалізація BingX API клієнта...")
        bingx_api = bingx_client.BingXClient(api_key, api_secret, logger)
        logger.info("BingX API клієнт успішно ініціалізовано.")
    except Exception as e:
        logger.critical(f"Критична помилка при ініціалізації BingX API: {e}", exc_info=True)
        if db_conn:
             db_conn.close() 
        sys.exit(1)
        
    # Ініціалізація та запуск PositionManager
    try:
        logger.info("Ініціалізація PositionManager...")
        position_manager_instance = PositionManager(bingx_api, config, db_conn)
        position_manager_instance.start_monitoring()
        logger.info("PositionManager успішно запущено.")
    except Exception as e:
        logger.critical(f"Критична помилка при ініціалізації PositionManager: {e}", exc_info=True)
        if db_conn:
             db_conn.close() 
        sys.exit(1)

    # Налаштування обробки сигналів ОС для коректного завершення
    signal.signal(signal.SIGINT, signal_handler)  # Обробка Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler) # Обробка сигналу завершення
    logger.info("Обробники сигналів SIGINT та SIGTERM встановлено.")

    # Запуск моніторингу Telegram
    # Передаємо функцію handle_new_message як callback
    # Передаємо stop_event для можливості зупинки моніторингу
    logger.info("Запуск моніторингу Telegram...")
    try:
        telegram_monitor.start_monitoring(
            config=config, 
            message_handler_func=lambda title, text: handle_new_message(title, text, config, bingx_api),
            stop_event=stop_event_main # Передаємо подію зупинки
        )
        logger.info("Моніторинг Telegram запущено. Бот працює.")
        # Головний цикл очікування завершення (поки stop_event не встановлено)
        while not stop_event_main.is_set():
             time.sleep(1) # Просто чекаємо
             
        logger.info("Головний цикл завершено через подію зупинки.")
             
    except Exception as e:
        logger.critical(f"Критична помилка під час роботи моніторингу Telegram: {e}", exc_info=True)
        stop_event_main.set() # Сигналізуємо про зупинку іншим компонентам
    finally:
        # --- Коректне завершення роботи --- 
        logger.info("===== Початок процедури завершення роботи бота ====")
        
        # 1. Зупиняємо PositionManager (якщо він був запущений)
        if position_manager_instance:
            logger.info("Зупинка PositionManager...")
            position_manager_instance.stop_monitoring()
            logger.info("PositionManager зупинено.")
            
        # 2. Зупиняємо Telegram Monitor (він мав би сам зупинитися по stop_event, але про всяк випадок)
        # Поточна реалізація telegram_monitor не має явного методу stop(), 
        # але він використовує stop_event, тому має завершитись.
        logger.info("Очікування можливого завершення Telegram Monitor...")
        # Тут можна додати join для потоку Telegram, якщо він створюється явно
        time.sleep(2) # Даємо час на завершення
            
        # 3. Закриваємо з'єднання з БД (якщо воно було відкрито)
        if db_conn:
            logger.info("Закриття з'єднання з базою даних...")
            db_conn.close()
            logger.info("З'єднання з базою даних закрито.")
            
        logger.info("===== Завершення роботи бота ====")
        print("Бот завершив роботу.")

if __name__ == "__main__":
    main() 