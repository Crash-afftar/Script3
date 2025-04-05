# Main application entry point and coordinator 
import logging
import json
import os
from dotenv import load_dotenv
from pythonjsonlogger import jsonlogger
import telegram_monitor
import signal_interpreter
import bingx_client
import data_manager
import re

# Словник для зберігання пар з каналу 1, для яких очікуємо деталі
# Ключ: нормалізована пара (напр., "INJUSDT"), Значення: True (або можна зберігати час)
pending_channel1_details = {}

# --- Logging Setup ---
def setup_logging(log_file="bot.log"):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG) # Встановлюємо рівень DEBUG для детальніших логів у файлі

    # Обробник для консолі (залишаємо INFO рівень)
    logHandlerConsole = logging.StreamHandler()
    logHandlerConsole.setLevel(logging.INFO)
    formatterConsole = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s') # Простіший формат для консолі
    logHandlerConsole.setFormatter(formatterConsole)
    logger.addHandler(logHandlerConsole)

    # Обробник для запису у файл (рівень INFO)
    try:
        logHandlerFile = logging.FileHandler(log_file, encoding='utf-8')
        logHandlerFile.setLevel(logging.INFO)
        formatterFile = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
        logHandlerFile.setFormatter(formatterFile)
        logger.addHandler(logHandlerFile)
        print(f"Логування також ведеться у файл: {log_file}") # Повідомлення в консоль
    except Exception as e:
        # Якщо не вдалося створити файл логів, продовжити тільки з консоллю
        print(f"Помилка при налаштуванні логування у файл {log_file}: {e}. Логування тільки в консоль.")

    return logger

# --- Configuration Loading ---
def load_config(config_path='config.json'):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logging.info(f"Конфігурацію завантажено з {config_path}")
        return config
    except FileNotFoundError:
        logging.error(f"Файл конфігурації не знайдено: {config_path}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Помилка декодування JSON у файлі: {config_path}")
        return None

# --- Головний обробник повідомлень ---
def handle_new_message(forwarded_channel_title: str, signal_text: str, config: dict, bingx_api_instance: bingx_client.BingXClient):
    """Обробляє переслане повідомлення, отримане від telegram_monitor.

    Args:
        forwarded_channel_title: Назва каналу, з якого переслано повідомлення.
        signal_text: Текст сигналу (з text або caption).
        config: Словник конфігурації.
        bingx_api_instance: Екземпляр BingXClient (може бути None, якщо ініціалізація не вдалась).
    """
    logger = logging.getLogger(__name__)
    
    # Якщо клієнт біржі не передано (або ініціалізація не вдалась), виходимо
    if not bingx_api_instance:
         logger.error("[Main Handler] BingX клієнт не доступний. Обробка сигналу неможлива.")
         return

    # 1. Визначаємо джерело сигналу та отримуємо конфігурацію каналу
    channel_key, source_name = signal_interpreter.identify_signal_source(forwarded_channel_title, config)
    if not channel_key:
        return # Лог про невідомий канал вже є в identify_signal_source
        
    channel_config = config.get('channels', {}).get(channel_key)
    if not channel_config:
        logger.error(f"[Main Handler] Не знайдено конфігурацію для каналу '{source_name}' (key: {channel_key}).")
        return

    # --- Отримання спільних параметрів з конфігу --- 
    try:
        leverage = int(channel_config['leverage'])
        entry_percentage = float(channel_config['entry_percentage'])
        tp_distribution = channel_config.get('tp_distribution', []) # Може бути відсутнім
        total_bankroll = float(config.get('global_settings', {}).get('total_bankroll', 0))
        
        if total_bankroll <= 0:
             logger.error(f"[Main Handler] total_bankroll ({total_bankroll}) має бути позитивним числом.")
             return
             
    except (KeyError, ValueError, TypeError) as config_err:
        logger.error(f"[Main Handler] Помилка отримання або конвертації параметрів (leverage, entry_percentage, tp_distribution, total_bankroll) з конфігу для каналу {source_name}: {config_err}")
        return
        
    # Розрахунок маржі для ордера
    margin_usdt = total_bankroll * (entry_percentage / 100.0)
    logger.info(f"[Main Handler] Для каналу '{source_name}': Плече={leverage}x, Маржа={margin_usdt:.2f} USDT ({entry_percentage}% від {total_bankroll})")

    # --- Обробка для Каналу 1 (дворівневий сигнал) ---
    if channel_key == "channel_1":
        # Спочатку пробуємо парсити як вхідний сигнал ("Заполняю...")
        entry_data = signal_interpreter.parse_channel_1_entry(signal_text)
        if entry_data:
            pair = entry_data['pair']
            direction = entry_data['direction']
            position_side = direction.upper() # LONG або SHORT
            order_side = 'buy' if position_side == 'LONG' else 'sell'
            
            logger.info(f"[Main C1 Entry] Отримано сигнал ВХОДУ для {pair} {position_side}. Ініціюємо вхід по ринку...")
            
            # === Розміщення ринкового ордера ===
            order_result = bingx_api_instance.place_market_order_basic(
                symbol=pair,
                side=order_side,
                position_side=position_side,
                margin_usdt=margin_usdt,
                leverage=leverage
            )

            if order_result and order_result.get('status') == 'closed':
                logger.info(f"[Main C1 Entry] Ринковий ордер для {pair} {position_side} успішно розміщено та виконано.")
                # Зберігаємо інформацію про виконаний ордер для встановлення TP/SL пізніше
                filled_amount = order_result.get('filled')
                market_symbol = order_result.get('symbol')
                order_id = order_result.get('id') # ID ордера на біржі
                
                if filled_amount and market_symbol and order_id:
                    # Зберігаємо дані про угоду, що очікує деталей
                    pending_channel1_details[pair] = {
                        'symbol': market_symbol,
                        'position_side': position_side,
                        'filled_amount': filled_amount,
                        'order_id': order_id
                    }
                    logger.info(f"[Main C1 Entry] Позиція {pair} додана до очікування деталей TP/SL (ID: {order_id}, Обсяг: {filled_amount}).")
                else:
                     logger.error(f"[Main C1 Entry] Не вдалося отримати filled_amount/symbol/id з результату ордера: {order_result}")
                     # Що робити в цьому випадку? Поки що просто логуємо.
            else:
                logger.error(f"[Main C1 Entry] Не вдалося розмістити ринковий ордер для {pair} {position_side} або він не виконався.")
                if order_result:
                     logger.error(f"  Результат від біржі: {order_result}")
            return # Завершуємо обробку цього повідомлення

        # Якщо це не вхідний сигнал, пробуємо парсити як деталі
        details_data = signal_interpreter.parse_channel_1_details(signal_text, config)
        if details_data:
            pair = details_data['pair']
            stop_loss = details_data['stop_loss']
            take_profits = details_data.get('take_profits', []) # TP може не бути
            logger.info(f"[Main C1 Details] Отримано деталі TP/SL для {pair}: SL={stop_loss}, TP={take_profits}")

            # Перевіряємо, чи очікували ми деталі для цієї пари
            if pair in pending_channel1_details:
                position_info = pending_channel1_details[pair]
                logger.info(f"[Main C1 Details] Позиція {pair} (ID: {position_info['order_id']}) очікувала на деталі. Встановлюємо TP/SL...")
                
                # --- Встановлення Stop Loss --- 
                sl_order = None
                if stop_loss:
                    sl_order = bingx_api_instance.set_stop_loss(
                        symbol=position_info['symbol'],
                        position_side=position_info['position_side'],
                        initial_amount=position_info['filled_amount'],
                        stop_loss_price=stop_loss
                    )
                    if not sl_order:
                         logger.error(f"[Main C1 Details] Не вдалося встановити Stop Loss для {pair}.")
                         # Продовжуємо спробувати встановити TP
                else:
                    logger.warning(f"[Main C1 Details] Ціна Stop Loss не знайдена в сигналі для {pair}.")
                
                # --- Встановлення Take Profit --- 
                tp_orders = []
                if take_profits and tp_distribution:
                    tp_orders = bingx_api_instance.set_take_profits(
                        symbol=position_info['symbol'],
                        position_side=position_info['position_side'],
                        initial_amount=position_info['filled_amount'],
                        take_profit_prices=take_profits,
                        tp_distribution=tp_distribution
                    )
                    if not tp_orders:
                         logger.warning(f"[Main C1 Details] Не вдалося встановити один або більше ордерів Take Profit для {pair}.")
                elif not take_profits:
                     logger.info(f"[Main C1 Details] Ціни Take Profit не знайдено в сигналі для {pair}.")
                elif not tp_distribution:
                     logger.warning(f"[Main C1 Details] Не знайдено tp_distribution в конфігу для каналу {channel_key} для встановлення TP.")
                
                # Видаляємо зі списку очікування незалежно від успіху встановлення SL/TP
                # Можливо, варто видаляти тільки якщо SL/TP встановлено?
                try:
                    del pending_channel1_details[pair]
                    logger.info(f"[Main C1 Details] Пара {pair} видалена зі списку очікування деталей.")
                except KeyError:
                     logger.warning(f"[Main C1 Details] Спроба видалити {pair}, але її вже немає в списку очікування.")
                     
            else:
                logger.warning(f"[Main C1 Details] Отримано деталі для {pair}, але вхід для цієї пари не очікувався. Можливо, пропущено сигнал 'Заполняю'? Ігнорується.")
            return # Завершуємо обробку
        
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
                 # Парсер повернув None (не розпізнав сигнал)
                 logger.warning(f"[Main Other] Парсер для каналу {source_name} не зміг розпізнати дані в тексті: {signal_text[:100]}...")
                 return
                 
            logger.info(f"[Main Other] Розпізнано сигнал від '{source_name}': { {k: v for k, v in signal_data.items() if k not in ['raw_text', 'source', 'source_name', 'type']} }")
            
            # Отримуємо необхідні дані з сигналу
            pair = signal_data.get('pair')
            direction = signal_data.get('direction')
            stop_loss = signal_data.get('stop_loss')
            take_profits = signal_data.get('take_profits', [])
            # --- Витягуємо ціну лімітного ордера ---
            limit_order_price = signal_data.get('limit_order_price')

            if not all([pair, direction, stop_loss]): # Перевіряємо наявність основних даних
                 logger.error(f"[Main Other] Сигнал від {source_name} не містить обов'язкових полів (pair, direction, stop_loss). Сигнал: {signal_data}")
                 return
                 
            position_side = direction.upper()
            order_side = 'buy' if position_side == 'LONG' else 'sell'

            # --- Спеціальна логіка для Каналу 3 (Джимми) з ліміткою ---
            if channel_key == 'channel_3' and limit_order_price is not None:
                logger.info(f"[Main C3 Special] Застосування логіки Market + Limit для {pair} {position_side} з лімітом {limit_order_price}")

                # 1. Розподіл маржі (приклад 50/50)
                market_split_percentage = 50.0
                limit_split_percentage = 50.0
                market_margin = margin_usdt * (market_split_percentage / 100.0)
                limit_margin = margin_usdt * (limit_split_percentage / 100.0)
                logger.info(f"  Розподіл маржі: Market={market_margin:.2f} USDT, Limit={limit_margin:.2f} USDT")

                # 2. Розміщення ринкового ордера (перша частина)
                logger.info(f"  Розміщення MARKET ордера (частина 1)...")
                order_result_market = bingx_api_instance.place_market_order_basic(
                    symbol=pair,
                    side=order_side,
                    position_side=position_side,
                    margin_usdt=market_margin, # Використовуємо частину маржі
                    leverage=leverage
                )

                filled_amount_market = None
                market_symbol = None
                avg_fill_price_market = None # Ціна виконання ринкового ордера

                if order_result_market and order_result_market.get('status') == 'closed':
                    filled_amount_market = order_result_market.get('filled')
                    market_symbol = order_result_market.get('symbol') # Беремо з результату
                    avg_fill_price_market = order_result_market.get('average') # Середня ціна виконання
                    logger.info(f"  MARKET ордер виконано: Обсяг={filled_amount_market}, Символ={market_symbol}, Ціна виконання ~{avg_fill_price_market}")
                else:
                    logger.error(f"  Не вдалося розмістити або виконати MARKET ордер (частина 1). Подальше розміщення LIMIT/SL/TP скасовано.")
                    # Можливо, варто вийти, якщо ринковий ордер не пройшов?
                    return

                # 3. Розрахунок обсягу для лімітного ордера (друга частина)
                logger.info(f"  Розрахунок обсягу для LIMIT ордера (частина 2)...")
                limit_amount = None
                if limit_order_price > 0:
                    limit_position_size = limit_margin * leverage
                    limit_amount_unrounded = limit_position_size / limit_order_price
                    logger.debug(f"    Розрахований обсяг ліміту (до округлення): {limit_amount_unrounded}")
                    # Округлення обсягу для ліміту (використовуємо метод клієнта)
                    # Важливо: округлювати треба для правильного символу біржі
                    limit_amount = bingx_api_instance._round_amount(limit_amount_unrounded, market_symbol)
                    if limit_amount is None or limit_amount <= 0:
                        logger.error(f"    Не вдалося округлити обсяг для LIMIT ордера або результат нульовий: {limit_amount}. Розміщення ліміту скасовано.")
                        limit_amount = None # Скидаємо, щоб не намагатись розмістити
                    else:
                         logger.info(f"    Обсяг для LIMIT ордера (після округлення): {limit_amount}")
                else:
                    logger.error(f"    Некоректна ціна ліміту ({limit_order_price}). Розміщення ліміту скасовано.")

                # 4. Розміщення лімітного ордера (друга частина)
                order_result_limit = None
                if limit_amount is not None and limit_amount > 0:
                    logger.info(f"  Розміщення LIMIT ордера (частина 2)...")
                    order_result_limit = bingx_api_instance.place_limit_order(
                        symbol=market_symbol, # Використовуємо символ з ринкового ордера
                        direction=direction, # Передаємо оригінальний напрямок ('LONG' або 'SHORT')
                        amount=limit_amount,
                        limit_price=limit_order_price,
                        leverage=leverage # Плече вже мало бути встановлено ринковим ордером, але можна передати для логування/перевірки
                    )
                    if order_result_limit:
                         logger.info(f"  LIMIT ордер успішно розміщено (ID: {order_result_limit.get('id')}).")
                    else:
                         logger.warning(f"  Не вдалося розмістити LIMIT ордер (частина 2).")
                         # Продовжуємо зі SL/TP тільки для ринкової частини? Ні, ставимо на повну.

                # 5. Розрахунок ЗАГАЛЬНОГО цільового обсягу для SL/TP
                # Базуємось на сумі фактично виконаного ринкового обсягу та потенційного лімітного
                logger.info(f"  Визначення ЗАГАЛЬНОГО обсягу для SL/TP...")
                total_intended_amount = filled_amount_market # Починаємо з ринкового
                
                # Додаємо обсяг лімітного ордера, якщо він був успішно розміщений і мав обсяг
                # Важливо: ми не знаємо, чи спрацює лімітка, але SL/TP ставимо на повний потенційний обсяг
                if order_result_limit and limit_amount and limit_amount > 0:
                    # Перевіряємо, чи обсяг лімітного ордера відповідає розрахованому
                    # order_limit_amount = order_result_limit.get('amount') # CCXT може повернути рядок
                    # if order_limit_amount and float(order_limit_amount) == limit_amount:
                    total_intended_amount += limit_amount
                    logger.info(f"    Додано обсяг запланованого LIMIT ордера ({limit_amount}).")
                    # else:
                    #     logger.warning(f"    Обсяг створеного LIMIT ордера ({order_limit_amount}) не співпадає з розрахованим ({limit_amount}). Використовуємо лише ринковий обсяг для SL/TP.")
                elif limit_amount and limit_amount > 0:
                     logger.warning(f"    LIMIT ордер не було створено, хоча обсяг ({limit_amount}) було розраховано. SL/TP будуть лише на ринковий обсяг.")
                     
                # Логуємо фінальний обсяг для SL/TP
                if total_intended_amount and total_intended_amount > 0:
                     logger.info(f"    Фінальний ЗАГАЛЬНИЙ обсяг для SL/TP: {total_intended_amount}")
                else:
                     logger.error(f"    Не вдалося визначити позитивний ЗАГАЛЬНИЙ обсяг для SL/TP ({total_intended_amount}).")
                     total_intended_amount = None # Переконуємось, що це None

                # 6. Встановлення Stop Loss і Take Profit на ЗАГАЛЬНИЙ обсяг
                if total_intended_amount and total_intended_amount > 0:
                    # --- Встановлення Stop Loss ---
                    logger.info(f"  Встановлення Stop Loss на ЗАГАЛЬНИЙ обсяг {total_intended_amount}...")
                    sl_order = bingx_api_instance.set_stop_loss(
                        symbol=market_symbol,
                        position_side=position_side,
                        initial_amount=total_intended_amount, # <<< Використовуємо загальний
                        stop_loss_price=stop_loss
                    )
                    if not sl_order:
                        logger.error(f"  Не вдалося встановити Stop Loss для {pair} на загальний обсяг.")

                    # --- Встановлення Take Profit ---
                    logger.info(f"  Встановлення Take Profit на ЗАГАЛЬНИЙ обсяг {total_intended_amount}...")
                    if take_profits and tp_distribution:
                        tp_orders = bingx_api_instance.set_take_profits(
                            symbol=market_symbol,
                            position_side=position_side,
                            initial_amount=total_intended_amount, # <<< Використовуємо загальний
                            take_profit_prices=take_profits,
                            tp_distribution=tp_distribution
                        )
                        if not tp_orders:
                            logger.warning(f"  Не вдалося встановити один або більше ордерів Take Profit для {pair} на загальний обсяг.")
                    elif not take_profits:
                        logger.info(f"  Ціни Take Profit не знайдено в сигналі для {pair}.")
                    elif not tp_distribution:
                        logger.warning(f"  Не знайдено tp_distribution в конфігу для каналу {channel_key} для встановлення TP.")
                else:
                     logger.warning("  SL/TP не встановлюються через відсутність розрахованого ЗАГАЛЬНОГО обсягу.")

            # --- Стандартна логіка для інших каналів або каналу 3 без лімітки ---
            else:
                logger.info(f"[Main Other Standard] Застосування стандартної логіки Market входу для {pair} {position_side}")
                # === Розміщення ринкового ордера (повний обсяг) ===
                logger.info(f"  Ініціюємо вхід по ринку (повна маржа {margin_usdt} USDT)...")
                order_result = bingx_api_instance.place_market_order_basic(
                    symbol=pair,
                    side=order_side,
                    position_side=position_side,
                    margin_usdt=margin_usdt, # Використовуємо повну маржу
                    leverage=leverage
                )

                if order_result and order_result.get('status') == 'closed':
                    logger.info(f"  Ринковий ордер успішно розміщено та виконано.")
                    filled_amount = order_result.get('filled')
                    market_symbol = order_result.get('symbol')

                    if filled_amount and market_symbol:
                        # --- Встановлення Stop Loss ---
                        logger.info(f"  Встановлення Stop Loss на виконаний обсяг {filled_amount}...")
                        sl_order = bingx_api_instance.set_stop_loss(
                            symbol=market_symbol,
                            position_side=position_side,
                            initial_amount=filled_amount, # <<< На основі фактично виконаного
                            stop_loss_price=stop_loss
                        )
                        if not sl_order:
                            logger.error(f"  Не вдалося встановити Stop Loss для {pair}.")

                        # --- Встановлення Take Profit ---
                        logger.info(f"  Встановлення Take Profit на виконаний обсяг {filled_amount}...")
                        if take_profits and tp_distribution:
                            tp_orders = bingx_api_instance.set_take_profits(
                                symbol=market_symbol,
                                position_side=position_side,
                                initial_amount=filled_amount, # <<< На основі фактично виконаного
                                take_profit_prices=take_profits,
                                tp_distribution=tp_distribution
                            )
                            if not tp_orders:
                                logger.warning(f"  Не вдалося встановити один або більше ордерів Take Profit для {pair}.")
                        elif not take_profits:
                            logger.info(f"  Ціни Take Profit не знайдено в сигналі для {pair}.")
                        elif not tp_distribution:
                            logger.warning(f"  Не знайдено tp_distribution в конфігу для каналу {channel_key} для встановлення TP.")
                    else:
                        logger.error(f"  Не вдалося отримати filled_amount або market_symbol з результату ордера: {order_result}. SL/TP не встановлено.")
                else:
                    logger.error(f"  Не вдалося розмістити ринковий ордер або він не виконався.")
                    if order_result:
                        logger.error(f"  Результат від біржі: {order_result}")

        except Exception as e:
            logger.error(f"[Main Other] Неочікувана помилка під час обробки сигналу від {source_name}: {e}", exc_info=True)

    # --- Кінець функції handle_new_message ---

if __name__ == "__main__":
    load_dotenv() # Завантажує змінні середовища з .env файлу
    logger = setup_logging()
    config = load_config()

    if not config:
        logger.critical("Не вдалося завантажити конфігурацію. Завершення роботи.")
        exit()

    # Отримання токенів/ключів
    raw_telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_token = None
    if raw_telegram_token:
        # --- Очистка токена від коментарів та пробілів ---
        token_part = raw_telegram_token.split('#', 1)[0]
        telegram_token = token_part.strip()
        # --- --- --- --- --- --- --- --- ---
    else:
        # Спробувати взяти з config.json, якщо немає в .env
        telegram_token = config.get('notifications', {}).get('telegram_bot_token')
        
    target_chat_id_str = os.getenv("TARGET_TELEGRAM_CHAT_ID")
    bingx_api_key = os.getenv("BINGX_API_KEY") or config.get('global_settings', {}).get('bingx_api_key')
    bingx_secret_key = os.getenv("BINGX_API_SECRET") or config.get('global_settings', {}).get('bingx_api_secret')

    if not telegram_token or "YOUR_BOT_TOKEN" in telegram_token:
        # Логуємо оригінальний прочитаний токен (якщо він був), щоб було зрозуміло
        logger.critical(f"Telegram Bot Token не знайдено, не замінено або має невірний формат. Перевірте .env або config.json. Прочитане значення (raw): '{raw_telegram_token}'")
        exit()
    else:
        # Логуємо очищений токен для перевірки (без секретної частини)
        logger.info(f"Telegram Bot Token успішно отримано та очищено (початок: {telegram_token[:15]}...).")

    # Перевірка TARGET_TELEGRAM_CHAT_ID
    target_chat_id = None
    if target_chat_id_str:
        try:
            # --- Захист від коментарів та пробілів ---
            # 1. Відрізаємо все, що йде після першого символу #
            chat_id_part = target_chat_id_str.split('#', 1)[0]
            # 2. Видаляємо зайві пробіли з початку та кінця
            chat_id_clean = chat_id_part.strip()
            # 3. Конвертуємо очищений рядок в число
            target_chat_id = int(chat_id_clean)
            logger.info(f"Цільовий ID чату успішно прочитано: {target_chat_id}")
            # --- --- --- --- --- --- --- --- ---
        except ValueError:
            # Логуємо оригінальний рядок, щоб бачити, що саме не так
            logger.critical(f"Невірний формат TARGET_TELEGRAM_CHAT_ID: '{target_chat_id_str}'. Очікувалось число.")
            exit()
    else:
        logger.critical("Змінна середовища TARGET_TELEGRAM_CHAT_ID не встановлена.")
        exit()

    # Перевірку BingX ключів робимо перед ініціалізацією
    if not bingx_api_key or "YOUR_API_KEY" in bingx_api_key or not bingx_secret_key or "YOUR_API_SECRET" in bingx_secret_key:
        logger.warning("BingX API ключі не знайдено або не замінено в .env/config.json. Функціонал біржі буде недоступний.")
    else:
        try:
            logger.info("Ініціалізація BingX клієнта...")
            bingx_api_instance = bingx_client.BingXClient(
                api_key=bingx_api_key,
                api_secret=bingx_secret_key,
                logger=logger # Передаємо наш головний логер
            )
            logger.info("BingX клієнт успішно ініціалізовано.")
        except Exception as client_error:
            logger.critical(f"Не вдалося ініціалізувати BingX клієнт: {client_error}", exc_info=True)
            # Можна вирішити, чи продовжувати роботу без біржі, чи завершити
            # Поки що завершуємо, якщо клієнт критично важливий
            exit()
    # --- Кінець ініціалізації BingX --- 

    logger.info("Запуск Telegram монітора...")

    # Передаємо конфіг та, можливо, bingx_api_instance в обробник?
    # Поки що handle_new_message приймає тільки title, text, config
    # Можливо, варто зробити bingx_api_instance доступним глобально або передавати його
    # Найпростіше - передати його в message_handler_wrapper
    try:
        def message_handler_wrapper(channel_title, text):
            # Перевіряємо, чи є у нас робочий клієнт біржі
            if bingx_api_instance: 
                handle_new_message(channel_title, text, config, bingx_api_instance) # <<< Передаємо клієнт
            else:
                logger.warning("BingX клієнт не ініціалізовано, обробка сигналу без взаємодії з біржею.")
                # Можна або нічого не робити, або викликати handle_new_message без клієнта,
                # якщо там є логіка, не залежна від біржі (зараз немає)
                pass 

        telegram_monitor.start_monitoring(telegram_token, config, target_chat_id, message_handler_wrapper)
    except Exception as e:
        logger.critical(f"Фатальна помилка під час запуску Telegram монітора: {e}", exc_info=True)

    logger.info("Роботу бота завершено.")

    # В кінці (для чистого виходу, якщо потрібно)
    # logger.info("Зупинка бота...") 