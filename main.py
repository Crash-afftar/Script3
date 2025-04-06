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
import inspect # <--- ДОДАЄМО ІМПОРТ
import threading
import time
from telegram.ext import Application # Додаємо Application для type hinting

# Повертаємо імпорт data_manager на глобальний рівень
import telegram_monitor
import signal_interpreter
import bingx_client
import data_manager # <--- Повертаємо сюди
import re
from position_manager import PositionManager
from typing import Optional

# --- Глобальні змінні --- 
# db_conn: Optional[sqlite3.Connection] = None
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
    logHandlerConsole.setLevel(logging.INFO) # <-- Повертаємо на INFO
    formatterConsole = logging.Formatter('%(asctime)s - %(levelname)-8s - %(name)-25s - %(message)s') # Вирівнювання
    logHandlerConsole.setFormatter(formatterConsole)
    logger.addHandler(logHandlerConsole)

    # Обробник для запису у файл (рівень INFO або DEBUG з конфігу?)
    # Поки що залишаємо INFO
    try:
        logHandlerFile = logging.FileHandler(log_file, encoding='utf-8', mode='a') # Режим 'a' для дозапису
        logHandlerFile.setLevel(logging.DEBUG) # <-- Залишаємо DEBUG
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
# Змінюємо сигнатуру: db_conn більше не потрібен, функція створить тимчасове з'єднання
def check_slot_availability(channel_key: str, config: dict) -> bool:
    """Перевіряє, чи є вільний слот для відкриття позиції.
       Створює ТИМЧАСОВЕ з'єднання з БД.
    """
    logger = logging.getLogger(__name__)
    limits = config.get('position_limits', {})
    active_count = -1
    limit = -1
    group_name = ""
    conn: Optional[sqlite3.Connection] = None
    available = False # За замовчуванням - не доступно

    try:
        # === Створюємо тимчасове з'єднання ===
        conn = data_manager.get_db_connection()
        if not conn:
            logger.error(f"[Slot Check] Не вдалося створити тимчасове з'єднання з БД для перевірки слотів.")
            return False # Якщо немає з'єднання, вважаємо, що слоту немає

        # === Логіка перевірки слотів (використовуємо тимчасове з'єднання conn) ===
        if channel_key in ['channel_1', 'channel_2', 'channel_4']:
            group_name = 'group_1_2_4'
            limit = limits.get('group_1_2_4_max_open')
            if limit is None:
                 logger.warning(f"[Slot Check] Не знайдено ліміт 'group_1_2_4_max_open' в конфігу. Дозволено відкриття.")
                 available = True # Дозволяємо, якщо ліміт не задано
            else:
                active_count = data_manager.get_active_position_count(conn, 'group_1_2_4')

        elif channel_key == 'channel_3':
            group_name = 'channel_3'
            limit = config.get('channels', {}).get(channel_key, {}).get('max_open_positions')
            if limit is None:
                 logger.warning(f"[Slot Check] Не знайдено ліміт 'max_open_positions' для {channel_key}. Дозволено відкриття.")
                 available = True
            else:
                active_count = data_manager.get_active_position_count(conn, 'channel_3')

        else:
            logger.warning(f"[Slot Check] Перевірка слотів для невідомого channel_key: {channel_key}. Дозволено.")
            available = True

        # Якщо ліміт встановлено, перевіряємо кількість
        if not available and limit is not None:
            if active_count == -1: # Помилка отримання даних з БД
                 logger.error(f"[Slot Check] Не вдалося отримати кількість активних позицій для групи '{group_name}'. Блокуємо відкриття.")
                 available = False
            else:
                 logger.info(f"[Slot Check] Група '{group_name}': Активних = {active_count}, Ліміт = {limit}")
                 if active_count < limit:
                     logger.info(f"[Slot Check] Є вільний слот для групи '{group_name}'.")
                     available = True
                 else:
                     logger.warning(f"[Slot Check] Ліміт слотів ({limit}) для групи '{group_name}' вичерпано. Нова угода не відкривається.")
                     available = False

    except sqlite3.Error as db_err:
         logger.error(f"[Slot Check] Помилка БД під час перевірки слотів: {db_err}", exc_info=True)
         available = False # Блокуємо у разі помилки БД
    except Exception as e:
         logger.error(f"[Slot Check] Неочікувана помилка під час перевірки слотів: {e}", exc_info=True)
         available = False
    finally:
        # === Гарантовано закриваємо тимчасове з'єднання ===
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
    logger = logging.getLogger("MessageHandler") # Окремий логер для обробника
    # Прибираємо global db_conn, кожна операція буде зі своїм з'єднанням
    
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

    # --- Перевірка слотів ПЕРЕД парсингом (викликаємо оновлену функцію БЕЗ db_conn) ---
    if not check_slot_availability(channel_key, config):
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
        
    margin_usdt = total_bankroll * (entry_percentage / 100.0)
    logger.info(f"[MessageHandler] Параметри для {source_name}: Плече={leverage}x, Маржа={margin_usdt:.2f} USDT ({entry_percentage}% від {total_bankroll})")

    # --- Обробка для Каналу 1 (дворівневий сигнал) ---
    if channel_key == "channel_1":
        # Спочатку пробуємо парсити як вхідний сигнал ("Заполняю...")
        # Передаємо config
        entry_data = signal_interpreter.parse_channel_1_entry(signal_text, config)
        if entry_data:
            pair_raw = entry_data['pair'] # Напр. "INJUSDT"
            direction = entry_data['direction']
            position_side = direction.upper() # LONG або SHORT
            order_side = 'buy' if position_side == 'LONG' else 'sell'
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            pending_key = pair_raw # Використовуємо оригінальну пару як ключ

            logger.info(f"[Main C1 Entry] Отримано сигнал ВХОДУ для {pending_key} ({api_symbol}) {position_side}. Ініціюємо вхід по ринку...")

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
                    logger.info(f"[Main C1 Entry] Ринковий ордер {order_id} для {pending_key} ({market_symbol}) {position_side} УСПІШНО ВИКОНАНО. Обсяг: {filled_amount}, Ціна: {avg_price:.5f}, Вартість: {cost:.2f} USDT")
                    pending_channel1_details[pending_key] = {
                        'symbol': market_symbol,
                        'position_side': position_side,
                        'entry_price': avg_price,
                        'initial_amount': filled_amount,
                        'leverage': leverage,
                        'initial_margin': margin_usdt,
                        'market_order_id': order_id,
                        'timestamp': time.time()
                    }
                    logger.info(f"[Main C1 Entry] Позиція {pending_key} додана до очікування деталей TP/SL.")
                else:
                    logger.error(f"[Main C1 Entry] Ринковий ордер {order_id} для {pending_key} ({market_symbol}) не виконався коректно або статус не 'closed'. Статус: {order_status}, Виконано: {filled_amount}, Ціна: {avg_price}. Ордер: {market_order_result}")
            else:
                 logger.error(f"[Main C1 Entry] Не вдалося розмістити ринковий ордер для {pending_key} ({api_symbol}) або відповідь API некоректна. Відповідь: {market_order_result}")
        # Якщо це не entry_data, можливо це details_data
        else:
            # Передаємо config
            details_data = signal_interpreter.parse_channel_1_details(signal_text, config)
            if details_data:
                pair_raw = details_data['pair']
                pending_key = pair_raw
                logger.info(f"[Main C1 Details] Отримано деталі TP/SL для {pending_key}.")

                if pending_key in pending_channel1_details:
                    entry_info = pending_channel1_details[pending_key]
                    market_symbol = entry_info['symbol']
                    position_side = entry_info['position_side']
                    entry_price = entry_info['entry_price']
                    initial_amount = entry_info['initial_amount']
                    leverage = entry_info['leverage']
                    initial_margin = entry_info['initial_margin']
                    # market_order_id = entry_info['market_order_id'] # Не використовується прямо, але є

                    sl_price = details_data.get('sl')
                    tp_prices = details_data.get('tp', [])

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

                    # Розміщення TP
                    tp_orders = []
                    if tp_prices and initial_amount > 0 and tp_distribution and len(tp_distribution) == len(tp_prices):
                        remaining_amount_tp = initial_amount
                        for i, tp_price in enumerate(tp_prices):
                            tp_percentage = tp_distribution[i]
                            tp_amount = initial_amount * (tp_percentage / 100.0)
                            # Останній TP забирає весь залишок
                            if i == len(tp_prices) - 1:
                                tp_amount = remaining_amount_tp

                            if tp_amount > 1e-9: # Розміщуємо тільки якщо обсяг > 0
                                tp_order = bingx_api_instance.place_tp_order(
                                    symbol=market_symbol,
                                    position_side=position_side,
                                    tp_price=tp_price,
                                    amount=tp_amount
                                )
                                if tp_order and tp_order.get('id'):
                                    tp_orders.append(tp_order)
                                    remaining_amount_tp -= tp_amount
                                else:
                                    logger.error(f"[Main C1 Details] Не вдалося розмістити TP{i+1} ордер для {market_symbol}. TP Ціна: {tp_price}, Обсяг: {tp_amount}. Відповідь: {tp_order}")
                                    tp_orders = [] # Reset TP orders if any failed
                                    break
                            else:
                                logger.warning(f"[Main C1 Details] Пропуск розміщення TP{i+1} для {market_symbol} через нульовий або від'ємний розрахований обсяг: {tp_amount}")

                    # ЗАПИС В БАЗУ ДАНИХ (тільки якщо SL і всі TP створені)
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
                                    'initial_margin': initial_margin,
                                    'leverage': leverage,
                                    'sl_order_id': sl_order['id'],
                                    'tp_order_ids': [tp['id'] for tp in tp_orders],
                                    'related_limit_order_id': None,
                                    'is_breakeven': 0,
                                    'is_active': 1
                                }
                                new_pos_id = data_manager.add_new_position(conn_add, position_data_to_db)
                                if new_pos_id:
                                    logger.info(f"[Main C1 Details] Позиція {market_symbol} ({position_side}) успішно збережена в БД з ID {new_pos_id}.")
                                    if pending_key in pending_channel1_details:
                                        del pending_channel1_details[pending_key]
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
                    else: # Якщо SL або TP не створено
                         logger.error(f"[Main C1 Details] Не вдалося створити повний набір SL/TP ордерів для {market_symbol}. SL створено: {bool(sl_order)}, TP створено: {len(tp_orders)}/{len(tp_prices)}. Збереження в БД скасовано.")
                         # Скасування вже створених ордерів
                         cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                         if cancel_ids:
                              logger.warning(f"[Main C1 Details] Скасування частково створених ордерів: {cancel_ids}")
                              bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                else: # Якщо pending_key не в pending_channel1_details
                    logger.warning(f"[Main C1 Details] Отримано деталі для {pending_key}, але відповідного запису про вхід не знайдено в очікуванні.")
            # Якщо це не details_data
            else:
                 logger.warning(f"[Main C1] Не вдалося розпарсити повідомлення як сигнал входу або деталей для каналу 1.")

    # --- Обробка для Каналу 2 (один сигнал з усім) ---
    elif channel_key == "channel_2":
        # Передаємо config
        signal_data = signal_interpreter.parse_channel_2(signal_text, config)
        if signal_data:
            pair_raw = signal_data['pair']
            direction = signal_data['direction']
            entry_price_signal = signal_data.get('entry') # Може бути відсутнє
            sl_price = signal_data.get('sl')
            tp_prices = signal_data.get('tp', [])
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            position_side = direction.upper()
            order_side = 'buy' if position_side == 'LONG' else 'sell'

            logger.info(f"[Main C2] Отримано сигнал для {pair_raw} ({api_symbol}) {position_side}. Вхід={entry_price_signal}, SL={sl_price}, TP={tp_prices}. Ініціюємо вхід по ринку...")

            # РОБОТА З БІРЖЕЮ (Розміщення ордерів)
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
                 market_symbol = market_order_result.get('symbol') # Використовуємо з відповіді
                 order_id = market_order_result.get('id')
                 avg_price = market_order_result.get('average', market_order_result.get('price')) # Ціна виконання
                 cost = market_order_result.get('cost')

                 if order_status == 'closed' and filled_amount is not None and filled_amount > 0 and avg_price is not None:
                    logger.info(f"[Main C2] Ринковий ордер {order_id} для {market_symbol} {position_side} УСПІШНО ВИКОНАНО. Обсяг: {filled_amount}, Ціна: {avg_price:.5f}, Вартість: {cost:.2f} USDT")

                    # Розміщення SL
                    sl_order = None
                    if sl_price is not None:
                         sl_order = bingx_api_instance.set_stop_loss(market_symbol, position_side, sl_price, filled_amount)
                         if not sl_order or not sl_order.get('id'):
                             logger.error(f"[Main C2] Не вдалося розмістити SL ордер для {market_symbol}. SL Ціна: {sl_price}. Відповідь: {sl_order}")
                             sl_order = None

                    # Розміщення TP
                    tp_orders = []
                    if tp_prices and filled_amount > 0 and tp_distribution and len(tp_distribution) == len(tp_prices):
                        remaining_amount_tp = filled_amount
                        for i, tp_price in enumerate(tp_prices):
                            tp_percentage = tp_distribution[i]
                            tp_amount = filled_amount * (tp_percentage / 100.0)
                            if i == len(tp_prices) - 1:
                                tp_amount = remaining_amount_tp

                            if tp_amount > 1e-9:
                                tp_order = bingx_api_instance.place_tp_order(market_symbol, position_side, tp_price, tp_amount)
                                if tp_order and tp_order.get('id'):
                                    tp_orders.append(tp_order)
                                    remaining_amount_tp -= tp_amount
                                else:
                                    logger.error(f"[Main C2] Не вдалося розмістити TP{i+1} ордер для {market_symbol}. TP Ціна: {tp_price}, Обсяг: {tp_amount}. Відповідь: {tp_order}")
                                    tp_orders = []
                                    break
                            else:
                                logger.warning(f"[Main C2] Пропуск розміщення TP{i+1} для {market_symbol} через нульовий обсяг: {tp_amount}")


                    # ЗАПИС В БАЗУ ДАНИХ (тільки якщо SL і всі TP створені)
                    if sl_order and tp_orders and len(tp_orders) == len(tp_prices):
                        conn_add_c2: Optional[sqlite3.Connection] = None
                        try:
                            conn_add_c2 = data_manager.get_db_connection()
                            if not conn_add_c2:
                                logger.critical(f"[Main C2] Не вдалося створити з'єднання з БД для збереження позиції {market_symbol}!")
                                # Скасування ордерів
                                cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                                if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                            else:
                                position_data_to_db = {
                                    'signal_channel_key': channel_key,
                                    'symbol': market_symbol,
                                    'position_side': position_side,
                                    'entry_price': avg_price, # Фактична ціна входу
                                    'initial_amount': filled_amount,
                                    'current_amount': filled_amount,
                                    'initial_margin': margin_usdt, # Планована маржа
                                    'leverage': leverage,
                                    'sl_order_id': sl_order['id'],
                                    'tp_order_ids': [tp['id'] for tp in tp_orders],
                                    'related_limit_order_id': None,
                                    'is_breakeven': 0,
                                    'is_active': 1
                                }
                                new_pos_id = data_manager.add_new_position(conn_add_c2, position_data_to_db)
                                if new_pos_id:
                                     logger.info(f"[Main C2] Позиція {market_symbol} ({position_side}) успішно збережена в БД з ID {new_pos_id}.")
                                else:
                                     logger.error(f"[Main C2] Не вдалося зберегти позицію {market_symbol} в БД!")
                                     # Скасування ордерів
                                     cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                                     if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                        except sqlite3.Error as db_err:
                            logger.error(f"[Main C2] Помилка БД при збереженні позиції {market_symbol}: {db_err}", exc_info=True)
                            # Скасування ордерів
                            cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                            if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                        finally:
                            if conn_add_c2:
                                conn_add_c2.close()
                    else: # Якщо SL або TP не створено
                         logger.error(f"[Main C2] Не вдалося створити повний набір SL/TP ордерів для {market_symbol}. SL створено: {bool(sl_order)}, TP створено: {len(tp_orders)}/{len(tp_prices)}. Збереження в БД скасовано.")
                         # Скасування ордерів
                         cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                         if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                 else: # Якщо ринковий ордер не виконався
                     logger.error(f"[Main C2] Ринковий ордер {order_id} для {market_symbol} не виконався коректно. Статус: {order_status}, Виконано: {filled_amount}. Ордер: {market_order_result}")
            else: # Якщо ринковий ордер не вдалося розмістити
                 logger.error(f"[Main C2] Не вдалося розмістити ринковий ордер для {api_symbol}. Відповідь: {market_order_result}")
        # Якщо signal_data не отримано
        else:
            logger.warning(f"[Main C2] Не вдалося розпарсити сигнал для каналу 2.")

    # --- Обробка для Каналу 3 (лімітний вхід) ---
    elif channel_key == "channel_3":
        # Передаємо config
        signal_data = signal_interpreter.parse_channel_3_entry(signal_text, config)
        if signal_data:
            pair_raw = signal_data['pair']
            direction = signal_data['direction']
            entry_price = signal_data.get('entry') # Обов'язкове для ліміту
            sl_price = signal_data.get('sl')
            tp_prices = signal_data.get('tp', [])
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            position_side = direction.upper()
            order_side = 'buy' if position_side == 'LONG' else 'sell'

            if entry_price is None:
                logger.error("[Main C3] Не знайдено ціну входу (entry) в сигналі, яка є обов'язковою для лімітного ордера.")
                return # Виходимо з handle_new_message

            logger.info(f"[Main C3] Отримано сигнал для {pair_raw} ({api_symbol}) {position_side}. Лімітний вхід={entry_price}, SL={sl_price}, TP={tp_prices}.")

            # РОБОТА З БІРЖЕЮ
            limit_order = None
            sl_order = None
            tp_orders = []

            # Розрахунок обсягу для лімітного ордера
            limit_amount = bingx_api_instance.calculate_order_size(margin_usdt, leverage, entry_price)
            if limit_amount is None or limit_amount <= 0:
                logger.error(f"[Main C3] Не вдалося розрахувати обсяг для лімітного ордера {api_symbol} або він <= 0. Маржа: {margin_usdt}, Плече: {leverage}, Ціна: {entry_price}")
                return # Виходимо з handle_new_message

            # Розміщення лімітного ордера
            limit_order = bingx_api_instance.place_limit_order(api_symbol, order_side, position_side, entry_price, limit_amount)
            if not limit_order or not limit_order.get('id'):
                logger.error(f"[Main C3] Не вдалося розмістити лімітний ордер для {api_symbol}. Ціна: {entry_price}, Обсяг: {limit_amount}. Відповідь: {limit_order}")
                return # Зупиняємось, якщо лімітка не створена

            logger.info(f"[Main C3] Лімітний ордер {limit_order['id']} для {api_symbol} успішно розміщено.")

            # Розміщення SL (з таким же обсягом, як лімітка)
            if sl_price is not None:
                sl_order = bingx_api_instance.set_stop_loss(api_symbol, position_side, sl_price, limit_amount)
                if not sl_order or not sl_order.get('id'):
                     logger.error(f"[Main C3] Не вдалося розмістити SL ордер для {api_symbol}. SL Ціна: {sl_price}. Відповідь: {sl_order}")
                     sl_order = None # Reset

            # Розміщення TP (з обсягами, що відповідають розподілу від лімітного обсягу)
            if tp_prices and limit_amount > 0 and tp_distribution and len(tp_distribution) == len(tp_prices):
                remaining_amount_tp = limit_amount
                for i, tp_price in enumerate(tp_prices):
                    tp_percentage = tp_distribution[i]
                    tp_amount = limit_amount * (tp_percentage / 100.0)
                    if i == len(tp_prices) - 1:
                        tp_amount = remaining_amount_tp

                    if tp_amount > 1e-9:
                        tp_order = bingx_api_instance.place_tp_order(api_symbol, position_side, tp_price, tp_amount)
                        if tp_order and tp_order.get('id'):
                             tp_orders.append(tp_order)
                             remaining_amount_tp -= tp_amount
                        else:
                             logger.error(f"[Main C3] Не вдалося розмістити TP{i+1} ордер для {api_symbol}. TP Ціна: {tp_price}, Обсяг: {tp_amount}. Відповідь: {tp_order}")
                             tp_orders = [] # Reset
                             break
                    else:
                        logger.warning(f"[Main C3] Пропуск розміщення TP{i+1} для {api_symbol} через нульовий обсяг: {tp_amount}")

            # ЗАПИС В БАЗУ ДАНИХ (тільки якщо ЛІМІТ, SL і всі TP створені)
            if limit_order and sl_order and tp_orders and len(tp_orders) == len(tp_prices):
                conn_add_c3: Optional[sqlite3.Connection] = None
                try:
                    conn_add_c3 = data_manager.get_db_connection()
                    if not conn_add_c3:
                        logger.critical(f"[Main C3] Не вдалося створити з'єднання з БД для збереження позиції {api_symbol}!")
                        # Скасування всіх ордерів
                        cancel_ids = [o['id'] for o in [limit_order, sl_order] + tp_orders if o and o.get('id')]
                        if cancel_ids: bingx_api_instance.cancel_multiple_orders(api_symbol, cancel_ids)
                    else:
                        position_data_to_db = {
                            'signal_channel_key': channel_key,
                            'symbol': api_symbol,
                            'position_side': position_side,
                            'entry_price': entry_price, # Ціна лімітного ордера
                            'initial_amount': limit_amount, # Обсяг лімітного ордера
                            'current_amount': limit_amount, # Поки що поточний = початковому
                            'initial_margin': margin_usdt,
                            'leverage': leverage,
                            'sl_order_id': sl_order['id'],
                            'tp_order_ids': [tp['id'] for tp in tp_orders],
                            'related_limit_order_id': limit_order['id'], # Зберігаємо ID лімітки
                            'is_breakeven': 0,
                            'is_active': 1 # Позиція вважається активною, поки лімітка не виконана/скасована
                        }
                        new_pos_id = data_manager.add_new_position(conn_add_c3, position_data_to_db)
                        if new_pos_id:
                            logger.info(f"[Main C3] Позиція {api_symbol} ({position_side}) успішно збережена в БД з ID {new_pos_id} (очікує виконання лімітного ордера {limit_order['id']}).")
                        else: # Якщо не вдалося додати в БД
                            logger.error(f"[Main C3] Не вдалося зберегти позицію {api_symbol} в БД!")
                            # Скасування всіх ордерів
                            cancel_ids = [o['id'] for o in [limit_order, sl_order] + tp_orders if o and o.get('id')]
                            if cancel_ids: bingx_api_instance.cancel_multiple_orders(api_symbol, cancel_ids)
                except sqlite3.Error as db_err:
                    logger.error(f"[Main C3] Помилка БД при збереженні позиції {api_symbol}: {db_err}", exc_info=True)
                    # Скасування всіх ордерів
                    cancel_ids = [o['id'] for o in [limit_order, sl_order] + tp_orders if o and o.get('id')]
                    if cancel_ids: bingx_api_instance.cancel_multiple_orders(api_symbol, cancel_ids)
                finally:
                    if conn_add_c3:
                        conn_add_c3.close()
            else: # Якщо не створено повний набір ордерів
                 logger.error(f"[Main C3] Не вдалося створити повний набір ордерів (Limit, SL, TP) для {api_symbol}. Limit: {bool(limit_order)}, SL: {bool(sl_order)}, TP: {len(tp_orders)}/{len(tp_prices)}. Збереження в БД скасовано.")
                 # Скасування вже створених ордерів
                 cancel_ids = [o['id'] for o in [limit_order, sl_order] + tp_orders if o and o.get('id')]
                 if cancel_ids:
                      logger.warning(f"[Main C3] Скасування частково створених ордерів: {cancel_ids}")
                      bingx_api_instance.cancel_multiple_orders(api_symbol, cancel_ids)
        # Якщо signal_data не отримано
        else:
            logger.warning(f"[Main C3] Не вдалося розпарсити сигнал для каналу 3.")

    # --- Обробка для Каналу 4 (простий ринковий) ---
    elif channel_key == "channel_4":
        # Передаємо config
        signal_data = signal_interpreter.parse_channel_4(signal_text, config)
        if signal_data:
            pair_raw = signal_data['pair']
            direction = signal_data['direction']
            sl_price = signal_data.get('stop_loss')
            api_symbol = bingx_api_instance._format_symbol_for_swap(pair_raw)
            position_side = direction.upper()
            order_side = 'buy' if position_side == 'LONG' else 'sell'

            logger.info(f"[Main C4] Отримано сигнал для {pair_raw} ({api_symbol}) {position_side}. SL={sl_price}. Ініціюємо вхід по ринку...")

            # РОБОТА З БІРЖЕЮ
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
                    logger.info(f"[Main C4] Ринковий ордер {order_id} для {market_symbol} {position_side} УСПІШНО ВИКОНАНО. Обсяг: {filled_amount}, Ціна: {avg_price:.5f}, Вартість: {cost:.2f} USDT")

                    # Розміщення SL
                    sl_order = None
                    tp_orders = [] # Додано ініціалізацію

                    # Розміщення SL
                    if sl_price is not None:
                        sl_order = bingx_api_instance.set_stop_loss(market_symbol, position_side, sl_price, filled_amount)
                        if not sl_order or not sl_order.get('id'):
                             logger.error(f"[Main C4] Не вдалося розмістити SL ордер для {market_symbol}. SL Ціна: {sl_price}. Відповідь: {sl_order}")
                             sl_order = None

                    # --- Додано розміщення TP --- 
                    tp_prices = signal_data.get('take_profits', [])
                    # --- Додано детальне логування перед умовою TP ---
                    logger.debug(f"[Main C4 Pre-TP Check] tp_prices={tp_prices} (type: {type(tp_prices)}), filled_amount={filled_amount} (type: {type(filled_amount)}), tp_distribution={tp_distribution} (type: {type(tp_distribution)})")
                    logger.debug(f"[Main C4 Pre-TP Check] Умови: bool(tp_prices)={bool(tp_prices)}, filled_amount > 0={filled_amount > 0}, bool(tp_distribution)={bool(tp_distribution)}")
                    # --- Кінець доданого логування ---
                    if tp_prices and filled_amount > 0 and tp_distribution:
                        logger.info(f"[Main C4] Спроба встановити {len(tp_prices)} TP ордер(ів) для {market_symbol}...")
                        tp_orders = bingx_api_instance.set_take_profits(
                            symbol=market_symbol, 
                            position_side=position_side, 
                            take_profit_prices=tp_prices,
                            tp_distribution=tp_distribution, 
                            initial_amount=filled_amount
                        )
                        if not tp_orders or len(tp_orders) != len(tp_prices):
                             logger.error(f"[Main C4] Не вдалося створити повний набір ({len(tp_orders)}/{len(tp_prices)}) TP ордерів для {market_symbol}. TP Ціни: {tp_prices}. Відповідь: {tp_orders}")
                             # Якщо ТП не створено, вважаємо це помилкою
                             # sl_order = None # Не скасовуємо SL, бо позиція вже відкрита. Лише логуємо
                             tp_orders = [] # Скидаємо список ТП
                        else:
                             logger.info(f"[Main C4] Успішно створено {len(tp_orders)} TP ордер(ів) для {market_symbol}.")
                    elif tp_prices:
                         logger.warning(f"[Main C4] TP ціни ({tp_prices}) є, але або обсяг ({filled_amount}) нульовий, або tp_distribution ({tp_distribution}) порожній. TP не встановлюються.")
                    # --- Кінець доданого коду TP ---
                    
                    # ЗАПИС В БАЗУ ДАНИХ (тільки якщо SL створено, TP опціонально) 
                    # Логіка для каналу 4: записуємо, якщо є хоча б SL. 
                    # Якщо ТП були в сигналі, але не створені, буде попередження вище.
                    if sl_order: 
                        conn_add_c4: Optional[sqlite3.Connection] = None
                        try:
                             conn_add_c4 = data_manager.get_db_connection()
                             if not conn_add_c4:
                                 logger.critical(f"[Main C4] Не вдалося створити з'єднання з БД для збереження позиції {market_symbol}!")
                                 # Скасувати SL і TP?
                                 cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                                 if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                             else:
                                position_data_to_db = {
                                    'signal_channel_key': channel_key,
                                    'symbol': market_symbol,
                                    'position_side': position_side,
                                    'entry_price': avg_price,
                                    'initial_amount': filled_amount,
                                    'current_amount': filled_amount,
                                    'initial_margin': margin_usdt,
                                    'leverage': leverage,
                                    'sl_order_id': sl_order['id'],
                                    'tp_order_ids': [tp['id'] for tp in tp_orders], # Використовуємо список tp_orders
                                    'related_limit_order_id': None,
                                    'is_breakeven': 0,
                                    'is_active': 1
                                }
                                new_pos_id = data_manager.add_new_position(conn_add_c4, position_data_to_db)
                                if new_pos_id:
                                    logger.info(f"[Main C4] Позиція {market_symbol} ({position_side}) успішно збережена в БД з ID {new_pos_id}.")
                                else: # Якщо не вдалося додати в БД
                                    logger.error(f"[Main C4] Не вдалося зберегти позицію {market_symbol} в БД!")
                                    # Скасувати SL і TP
                                    cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                                    if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                        except sqlite3.Error as db_err:
                            logger.error(f"[Main C4] Помилка БД при збереженні позиції {market_symbol}: {db_err}", exc_info=True)
                            # Скасувати SL і TP
                            cancel_ids = [o['id'] for o in [sl_order] + tp_orders if o and o.get('id')]
                            if cancel_ids: bingx_api_instance.cancel_multiple_orders(market_symbol, cancel_ids)
                        finally:
                            if conn_add_c4:
                                conn_add_c4.close()
                    # Якщо SL не вдалося створити
                    elif sl_price is not None: # Тільки якщо SL був у сигналі, але не створився
                         logger.error(f"[Main C4] Не вдалося створити SL ордер для {market_symbol}. Збереження в БД скасовано.")
                         logger.critical(f"[Main C4] !!! ПОТРІБНО ВРУЧНУ ОБРОБИТИ ВІДКРИТУ ПОЗИЦІЮ {market_symbol} ({position_side}) БЕЗ SL !!!")
                    # Якщо SL і не було в сигналі - це нормально для каналу 4? Якщо так, можна записати в БД
                    # elif sl_price is None: 
                    #     logger.info(f"[Main C4] SL не було в сигналі. Записуємо позицію без SL/TP...")
                    #     # ... (додати логіку збереження без SL) ...

                 # Якщо ринковий ордер не виконався
                 else:
                     logger.error(f"[Main C4] Ринковий ордер {order_id} для {market_symbol} не виконався коректно. Статус: {order_status}, Виконано: {filled_amount}. Ордер: {market_order_result}")
            else: # Якщо ринковий ордер не вдалося розмістити
                 logger.error(f"[Main C4] Не вдалося розмістити ринковий ордер для {api_symbol}. Відповідь: {market_order_result}")
        # Якщо signal_data не отримано
        else:
             logger.warning(f"[Main C4] Не вдалося розпарсити сигнал для каналу 4.")

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