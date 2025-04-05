# Module for interpreting signals from different channels 

import logging
import re

logger = logging.getLogger(__name__)

# # Словник з назвами каналів більше не потрібен тут, беремо з конфігу
# CHANNEL_NAME_MAP = {
#     "VIP марафон | Даниэль": "channel_1",
#     "Crypto Alliance | Мартин": "channel_2",
#     "Внутри графика с Джимми": "channel_3",
#     "KostyaKogan": "channel_4",
# }

# --- Helper function to safely convert string to float ---
def safe_float(value_str):
    if value_str is None:
        return None
    try:
        # Replace comma with dot if needed, remove spaces
        return float(value_str.replace(",", ".").strip())
    except (ValueError, TypeError):
        return None

# --- Helper function to normalize trading pair ---
def normalize_pair(pair_str):
    if pair_str is None:
        return None
    pair = pair_str.upper().replace("/", "").strip()
    # Assume USDT if only base currency is provided
    if not any(quote in pair for quote in ["USDT", "BTC", "ETH"]): # Add other potential quote currencies if needed
        pair += "USDT"
    return pair

# --- Function to identify the channel --- (No parsing logic here anymore for C1)
def identify_signal_source(forwarded_channel_title: str, config: dict):
    """Визначає ключ каналу за його назвою з Telegram API."""
    logger.debug(f"Визначаю джерело за назвою: '{forwarded_channel_title}'")
    for key, channel_data in config.get('channels', {}).items():
        name_from_config = channel_data.get('name')
        if name_from_config and forwarded_channel_title == name_from_config:
            logger.debug(f"Джерело визначено: key={key}, name={name_from_config}")
            return key, name_from_config # Повертаємо ключ і назву
    logger.info(f"Назва каналу '{forwarded_channel_title}' не знайдена в config.json.")
    return None, None # Якщо канал не знайдено

# --- Channel-specific parsers --- 

def parse_channel_1_entry(text: str):
    """Парсер для ПЕРШОГО повідомлення каналу 1 ('Заполняю...')."""
    logger.debug("  [C1 Entry] Спроба парсингу як повідомлення 'Заполняю...'")
    # Патерн: "Заполняю" + пробіл + (Слово) + пробіл + (long або short)
    match = re.search(r"Заполняю\s+(\w+)\s+(long|short)", text, re.IGNORECASE)
    if match:
        pair = normalize_pair(match.group(1))
        direction = match.group(2).upper()
        logger.info(f"  [C1 Entry] Розпізнано вхідний сигнал: Pair={pair}, Direction={direction}")
        return {"type": "entry", "pair": pair, "direction": direction}
    logger.debug("  [C1 Entry] Не знайдено патерн 'Заполняю...'")
    return None

def parse_channel_1_details(text: str, config: dict):
    """Парсер для ДРУГОГО повідомлення каналу 1 (з деталями TP/SL)."""
    logger.debug("  [C1 Details] Спроба парсингу як повідомлення з деталями (Монета:...)")
    signal_data = {
        "type": "details", # Додаємо тип для розрізнення
        "source": "channel_1",
        "source_name": config['channels']['channel_1']['name'],
        "pair": None,
        "direction": None, # Напрямок теж є в цьому повідомленні
        "entry_price": None,
        "take_profits": [],
        "stop_loss": None,
        "raw_text": text,
    }

    try:
        # 1. Пара та напрямок (з рядка "Монета: ...")
        pair_match = re.search(r"Монета:\s+(\w+)\s+(LONG|SHORT)", text, re.IGNORECASE)
        if pair_match:
            signal_data["pair"] = normalize_pair(pair_match.group(1))
            signal_data["direction"] = pair_match.group(2).upper()
            logger.debug(f"  [C1 Details] Знайдено пару: {signal_data['pair']}, напрямок: {signal_data['direction']}")
        else:
            logger.warning("  [C1 Details] Не вдалося знайти пару та напрямок ('Монета:...').")
            return None # Вважаємо обов'язковим для ідентифікації

        # 2. Ціна входу
        entry_match = re.search(r"Цена входа:\s*([\d.,]+)", text)
        if entry_match:
            signal_data["entry_price"] = safe_float(entry_match.group(1))
            logger.debug(f"  [C1 Details] Знайдено ціну входу: {signal_data['entry_price']}")
        # Не робимо return None, можливо ціна входу не критична для встановлення SL/TP?
        # Але краще залишити обов'язковим для повноти даних
        # else:
        #     logger.warning("  [C1 Details] Не вдалося знайти ціну входу.")
        #     return None 

        # 3. Тейк-профіти
        tp_match = re.search(r"Тэйки:\s*([\d.,\s]+)", text)
        if tp_match:
            tp_str = tp_match.group(1).strip()
            signal_data["take_profits"] = [p for p in (safe_float(val) for val in tp_str.split()) if p is not None]
            logger.debug(f"  [C1 Details] Знайдено тейк-профіти: {signal_data['take_profits']}")
        else:
            logger.warning("  [C1 Details] Не вдалося знайти тейк-профіти.")

        # 4. Стоп-лосс
        sl_match = re.search(r"Стоп:\s*([\d.,]+)", text)
        if sl_match:
            signal_data["stop_loss"] = safe_float(sl_match.group(1))
            logger.debug(f"  [C1 Details] Знайдено стоп-лосс: {signal_data['stop_loss']}")
        else:
            logger.warning("  [C1 Details] Не вдалося знайти стоп-лосс.")
            return None # Обов'язкове поле для встановлення ордерів

        # Перевірка обов'язкових полів для деталей
        if not all([signal_data["pair"], signal_data["direction"], signal_data["stop_loss"]]):
             logger.warning("  [C1 Details] Не всі обов'язкові поля (pair, direction, stop_loss) було розпізнано.")
             return None
             
        logger.info(f"  [C1 Details] Розпізнано деталі сигналу: { {k: v for k, v in signal_data.items() if k != 'raw_text'} }")
        return signal_data

    except Exception as e:
        logger.error(f"  [C1 Details] Неочікувана помилка під час парсингу деталей каналу 1: {e}", exc_info=True)
        return None

# --- Заглушки для інших каналів (додаємо type) ---
def parse_channel_2(text: str, config: dict):
    """Парсер для каналу 2 (Crypto Alliance | Мартин)."""
    logger.info(f"Викликано парсер для каналу 2 ({config['channels']['channel_2']['name']}).")
    signal_data = {
        "type": "full",
        "source": "channel_2",
        "source_name": config['channels']['channel_2']['name'],
        "pair": None,
        "direction": None,
        "entry_price": None,
        "take_profits": [],
        "stop_loss": None,
        "raw_text": text,
    }

    try:
        # 1. Пара та напрямок
        pair_match = re.search(r"Заходим\s+([\w\/]+)\s+(long|short)", text, re.IGNORECASE)
        if pair_match:
            signal_data["pair"] = normalize_pair(pair_match.group(1))
            signal_data["direction"] = pair_match.group(2).upper()
            logger.debug(f"  [C2] Знайдено пару: {signal_data['pair']}, напрямок: {signal_data['direction']}")
        else:
            logger.warning("  [C2] Не вдалося знайти пару та напрямок ('Заходим...').")
            return None # Обов'язкові

        # 2. Ціна входу (Шукаємо текст після двокрапки)
        entry_match = re.search(r"Точка входа:\s*([\d.,]+)", text, re.IGNORECASE)
        if entry_match:
            signal_data["entry_price"] = safe_float(entry_match.group(1))
            logger.debug(f"  [C2] Знайдено ціну входу: {signal_data['entry_price']}")
        else:
            logger.warning("  [C2] Не вдалося знайти ціну входу ('Точка входа:...').")
            return None # Обов'язкове

        # 3. Тейк-профіти (Шукаємо текст після двокрапки, розділяємо по " - ")
        tp_match = re.search(r"Тейки:\s*(.+)", text, re.IGNORECASE)
        if tp_match:
            tp_str = tp_match.group(1).strip()
            # Розділяємо по " - ", очищуємо від пробілів навколо чисел
            signal_data["take_profits"] = [p for p in (safe_float(val.strip()) for val in tp_str.split(' - ')) if p is not None]
            logger.debug(f"  [C2] Знайдено тейк-профіти: {signal_data['take_profits']}")
        else:
            logger.warning("  [C2] Не вдалося знайти тейк-профіти ('Тейки:...').")
            # Тейки можуть бути необов'язковими

        # 4. Стоп-лосс (Шукаємо текст після двокрапки)
        sl_match = re.search(r"Стоп:\s*([\d.,]+)", text, re.IGNORECASE)
        if sl_match:
            signal_data["stop_loss"] = safe_float(sl_match.group(1))
            logger.debug(f"  [C2] Знайдено стоп-лосс: {signal_data['stop_loss']}")
        else:
            logger.warning("  [C2] Не вдалося знайти стоп-лосс ('Стоп:...').")
            return None # Обов'язкове

        # Перевірка обов'язкових полів
        if not all([signal_data["pair"], signal_data["direction"], signal_data["entry_price"], signal_data["stop_loss"]]):
             logger.warning("  [C2] Не всі обов'язкові поля (pair, direction, entry_price, stop_loss) було розпізнано.")
             return None

        logger.info(f"  [C2] Розпізнано сигнал: { {k: v for k, v in signal_data.items() if k != 'raw_text'} }")
        return signal_data

    except Exception as e:
        logger.error(f"  [C2] Неочікувана помилка під час парсингу каналу 2: {e}", exc_info=True)
        return None

def parse_channel_3(text: str, config: dict):
    logger.info(f"Викликано парсер для каналу 3 ({config['channels']['channel_3']['name']}).")
    # --- Log the exact text being parsed ---
    logger.debug(f"  [C3] Текст для парсингу (repr): {repr(text)}")
    logger.debug(f"  [C3] Текст для парсингу (raw):\n---\n{text}\n---")

    signal_data = {
        "type": "full",
        "source": "channel_3",
        "source_name": config['channels']['channel_3']['name'],
        "pair": None,
        "direction": None,
        "entry_price": "MARKET",
        "limit_order_price": None,
        "take_profits": [],
        "stop_loss": None,
        "raw_text": text,
    }

    try:
        # 1. Напрямок та діапазон цін (з нього беремо ціну для лімітки)
        # Зробимо regex більш гнучким до пробілів, замінюючи пробіли на \s+
        entry_range_match = re.search(r"Начинаю\s+открывать\s+(лонг|шорт)\s+в\s+диапазоне\s+цены\s+([\d.,]+)\s*-\s*([\d.,]+)", text, re.IGNORECASE)
        if entry_range_match:
            signal_data["direction"] = "LONG" if entry_range_match.group(1).lower() == "лонг" else "SHORT"
            # Витягуємо обидві межі, але для лімітки беремо другу (min)
            price_high_str = entry_range_match.group(2)
            price_low_str = entry_range_match.group(3)
            signal_data["limit_order_price"] = safe_float(price_low_str)
            logger.debug(f"  [C3] Знайдено напрямок: {signal_data['direction']}, діапазон: {price_high_str}-{price_low_str}, ціна ліміту: {signal_data['limit_order_price']}")
            if signal_data["limit_order_price"] is None:
                 logger.warning("  [C3] Не вдалося конвертувати нижню межу діапазону в ціну лімітного ордера, але продовжуємо (для можливості market + limit).")
        else:
            # Якщо діапазону немає, це просто MARKET сигнал (але для Джиммі це не очікувано)
            # Або може бути інший формат, який ми ще не обробляємо
            logger.warning("  [C3] Не вдалося знайти рядок 'Начинаю открывать...' з діапазоном цін. Можливо, інший формат сигналу?")
            # Повертаємо None, якщо діапазон обов'язковий для цього каналу за новою логікою
            return None # Поки що вважаємо діапазон обов'язковим для логіки market+limit

        # 2. Пара (Шукаємо ТІКЕР перед рядком "Начинаю открывать")
        # Перевіряємо, чи entry_range_match існує перед доступом до start()
        if not entry_range_match:
             logger.error("  [C3] Логічна помилка: entry_range_match не знайдено, але код продовжив виконання.")
             return None
             
        entry_range_start_index = entry_range_match.start()
        text_before_entry = text[:entry_range_start_index]
        pair_ticker_match = None
        for match in re.finditer(r"\b([A-Z]{3,})\b", text_before_entry):
            pair_ticker_match = match # Запам'ятовуємо останній
        
        if pair_ticker_match:
            signal_data["pair"] = normalize_pair(pair_ticker_match.group(1))
            logger.debug(f"  [C3] Знайдено пару (тікер): {signal_data['pair']}")
        else:
            # Якщо тікер не знайдено, спробуємо знайти щось типу xxx/usdt
            pair_slash_match = re.search(r"\b(\w+/usdt)\b", text_before_entry, re.IGNORECASE)
            if pair_slash_match:
                signal_data["pair"] = normalize_pair(pair_slash_match.group(1))
                logger.debug(f"  [C3] Знайдено пару (xxx/usdt): {signal_data['pair']}")
            else:
                logger.warning("  [C3] Не вдалося знайти тікер пари у тексті перед описом входу.")
                return None # Обов'язкове поле

        # 3. Стоп-лосс
        # Зробимо regex більш гнучким до пробілів
        sl_match = re.search(r"Сл\s+ставлю\s+на\s+([\d.,]+)", text, re.IGNORECASE)
        if sl_match:
            signal_data["stop_loss"] = safe_float(sl_match.group(1))
            logger.debug(f"  [C3] Знайдено стоп-лосс: {signal_data['stop_loss']}")
        else:
            logger.warning("  [C3] Не вдалося знайти стоп-лосс ('Сл ставлю на...').")
            return None # Обов'язкове

        # 4. Тейк-профіти (Роздільник " и ")
        tp_match = re.search(r"Мои цели на сделку\s+(.+)", text, re.IGNORECASE)
        if tp_match:
            tp_str = tp_match.group(1).strip()
            # Розділяємо по " и ", очищуємо КОЖНУ частину від нечислових символів (крім . ,) і конвертуємо
            take_profits = []
            for part in tp_str.split(" и "):
                # Видаляємо все, що не є цифрою, крапкою або комою
                cleaned_part = re.sub(r"[^\d.,]", "", part.strip())
                profit_value = safe_float(cleaned_part)
                if profit_value is not None:
                    take_profits.append(profit_value)

            signal_data["take_profits"] = take_profits
            logger.debug(f"  [C3] Знайдено тейк-профіти: {signal_data['take_profits']}")

            if not signal_data["take_profits"]:
                logger.warning("  [C3] Знайдено рядок 'Мои цели...', але не вдалося витягти жодного числового значення тейк-профіту.")
        else:
            logger.warning("  [C3] Не вдалося знайти рядок 'Мои цели на сделку...'.")

        # Перевірка обов'язкових полів (включаючи перевірку типу ордера)
        required_fields_ok = all([
            signal_data["pair"],
            signal_data["direction"],
            signal_data["stop_loss"]
        ])
        # --- Видаляємо некоректний блок перевірки для MARKET ---
        # # Додаткова перевірка для лімітного ордера
        # if signal_data["entry_price"] == "LIMIT" and signal_data["limit_order_price"] is None:
        #      logger.warning("  [C3] Тип ордера LIMIT, але ціна ліміту відсутня або некоректна.")
        #      return None
        # elif signal_data["entry_price"] == "MARKET":
        #     # Поки що для каналу 3 немає підтримки MARKET ордерів, тому якщо сюди дійшло, це помилка парсингу
        #     logger.warning("  [C3] Парсер не знайшов діапазон цін, але дійшов до кінця. Це не очікувано для каналу 3.")
        #     return None

        if not required_fields_ok:
            logger.warning("  [C3] Не всі обов'язкові поля (pair, direction, stop_loss) було розпізнано.")
            return None

        # --- Додаткове логування перед успішним поверненням ---
        logger.debug("  [C3] Усі перевірки пройдені. Повертаю розпізнані дані.")
        # 6. Log success and return data
        logger.info(f"  [C3] Розпізнано сигнал: { {k: v for k, v in signal_data.items() if k != 'raw_text'} }")
        return signal_data

    except Exception as e:
        logger.error(f"  [C3] Неочікувана помилка під час парсингу каналу 3: {e}", exc_info=True)
        return None

def parse_channel_4(text: str, config: dict):
    """Парсер для каналу 4 (KostyaKogan)."""
    logger.info(f"Викликано парсер для каналу 4 ({config['channels']['channel_4']['name']}).")
    signal_data = {
        "type": "full",
        "source": "channel_4",
        "source_name": config['channels']['channel_4']['name'],
        "pair": None,
        "direction": None,
        "entry_price": "MARKET", # Явно вказуємо ринковий вхід
        "take_profits": [],
        "stop_loss": None,
        "leverage": None, # Додаткове поле для плеча
        "raw_text": text,
    }

    try:
        # 1. Пара та напрямок (Шукаємо в першому рядку)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        pair_match = re.search(r"([A-Z]{3,})\s+(long|short)", first_line, re.IGNORECASE)
        if pair_match:
            signal_data["pair"] = normalize_pair(pair_match.group(1))
            signal_data["direction"] = pair_match.group(2).upper()
            logger.debug(f"  [C4] Знайдено пару: {signal_data['pair']}, напрямок: {signal_data['direction']}")
        else:
            logger.warning("  [C4] Не вдалося знайти пару та напрямок у першому рядку.")
            return None # Обов'язкові

        # 2. Плече (опціонально)
        leverage_match = re.search(r"плечо:\s*(\d+)x?", text, re.IGNORECASE)
        if leverage_match:
            try:
                signal_data["leverage"] = int(leverage_match.group(1))
                logger.debug(f"  [C4] Знайдено плече: {signal_data['leverage']}")
            except ValueError:
                 logger.warning("  [C4] Не вдалося конвертувати плече в число.")
        else:
             logger.debug("  [C4] Плече не знайдено.")

        # 3. Стоп-лосс (Ключове слово з маленької літери!)
        sl_match = re.search(r"стоп:\s*([\d.,]+)", text, re.IGNORECASE)
        if sl_match:
            signal_data["stop_loss"] = safe_float(sl_match.group(1))
            logger.debug(f"  [C4] Знайдено стоп-лосс: {signal_data['stop_loss']}")
        else:
            logger.warning("  [C4] Не вдалося знайти стоп-лосс ('стоп:...').")
            return None # Обов'язкове

        # 4. Тейк-профіти (Ключове слово з маленької! Роздільник ", ")
        tp_match = re.search(r"тейк:\s*(.+)", text, re.IGNORECASE)
        if tp_match:
            tp_str = tp_match.group(1).strip()
            # Розділяємо по ", ", очищуємо від пробілів, конвертуємо
            signal_data["take_profits"] = [p for p in (safe_float(val.strip()) for val in tp_str.split(',')) if p is not None]
            logger.debug(f"  [C4] Знайдено тейк-профіти: {signal_data['take_profits']}")
        else:
            logger.warning("  [C4] Не вдалося знайти тейк-профіти ('тейк:...').")
            # Тейки можуть бути необов'язковими

        # Перевірка обов'язкових полів
        if not all([signal_data["pair"], signal_data["direction"], signal_data["stop_loss"]]):
             logger.warning("  [C4] Не всі обов'язкові поля (pair, direction, stop_loss) було розпізнано.")
             return None

        logger.info(f"  [C4] Розпізнано сигнал: { {k: v for k, v in signal_data.items() if k != 'raw_text'} }")
        return signal_data

    except Exception as e:
        logger.error(f"  [C4] Неочікувана помилка під час парсингу каналу 4: {e}", exc_info=True)
        return None 