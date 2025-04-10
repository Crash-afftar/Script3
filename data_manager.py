import sqlite3
import json
import logging
import datetime
from typing import List, Dict, Optional, Any

DATABASE_FILE = 'positions.sqlite'

logger = logging.getLogger(__name__)

# --- Ось тут має бути функція ---
def get_db_connection() -> Optional[sqlite3.Connection]:
    """Встановлює та повертає з'єднання з базою даних SQLite."""
    try:
        conn = sqlite3.connect(DATABASE_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        conn.row_factory = sqlite3.Row # Повертати результати як словники
        logger.info(f"[DataManager] Успішно підключено до бази даних: {DATABASE_FILE}")
        return conn
    except sqlite3.Error as e:
        logger.critical(f"[DataManager] Помилка підключення до бази даних {DATABASE_FILE}: {e}", exc_info=True)
        return None
# ------------------------------------

def initialize_database(conn: Optional[sqlite3.Connection] = None):
    """Створює таблицю active_positions, якщо вона не існує."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        if conn is None:
            return False
        close_conn = True
        
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_channel_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                position_side TEXT NOT NULL, -- 'LONG' or 'SHORT'
                entry_price REAL NOT NULL,
                initial_amount REAL NOT NULL,
                current_amount REAL NOT NULL,
                initial_margin REAL,
                leverage INTEGER,
                sl_order_id TEXT,
                tp_order_ids TEXT, -- JSON list of strings
                related_limit_order_id TEXT, -- For Channel 3
                is_breakeven INTEGER NOT NULL DEFAULT 0, -- 0 = False, 1 = True
                is_active INTEGER NOT NULL DEFAULT 1, -- 0 = False, 1 = True
                status_info TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Додаємо тригер для автоматичного оновлення updated_at
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS update_active_positions_updated_at
            AFTER UPDATE ON active_positions
            FOR EACH ROW
            BEGIN
                UPDATE active_positions SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END;
        ''')
        conn.commit()
        logger.info("[DataManager] Таблиця 'active_positions' успішно ініціалізована (або вже існувала). Тригер оновлення створено.")
        return True
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при ініціалізації таблиці active_positions: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if close_conn and conn:
            conn.close()

def add_new_position(conn: sqlite3.Connection, data: Dict[str, Any]) -> Optional[int]:
    """Додає новий запис про відкриту позицію.

    Args:
        conn: З'єднання з БД.
        data: Словник з даними позиції (ключі відповідають стовпцям таблиці).
              'tp_order_ids' повинен бути списком рядків.

    Returns:
        ID створеного запису або None у разі помилки.
    """
    required_keys = ['signal_channel_key', 'symbol', 'position_side', 'entry_price', 'initial_amount', 'current_amount'] # Додано current_amount
    if not all(key in data for key in required_keys):
        logger.error(f"[DataManager] Не вистачає обов'язкових полів для додавання позиції: {required_keys}. Надано: {list(data.keys())}")
        return None

    # Переконуємось, що tp_order_ids - це JSON рядок
    tp_ids_json = json.dumps(data.get('tp_order_ids', []))
    
    sql = '''INSERT INTO active_positions (
                signal_channel_key, symbol, position_side, entry_price, initial_amount, current_amount,
                initial_margin, leverage, sl_order_id, tp_order_ids, related_limit_order_id
             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
    params = (
        data['signal_channel_key'], data['symbol'], data['position_side'], data['entry_price'], 
        data['initial_amount'], data['current_amount'], # Використовуємо переданий current_amount
        data.get('initial_margin'), data.get('leverage'), data.get('sl_order_id'), 
        tp_ids_json, data.get('related_limit_order_id')
    )
    
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        position_id = cursor.lastrowid
        logger.info(f"[DataManager] Успішно додано нову позицію ID: {position_id} для {data['symbol']} ({data['position_side']}).")
        return position_id
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при додаванні нової позиції: {e}", exc_info=True)
        conn.rollback()
        return None

def get_active_positions(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Повертає список всіх активних позицій (is_active = 1)."""
    sql = "SELECT * FROM active_positions WHERE is_active = 1 ORDER BY created_at ASC"
    positions = []
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            position_dict = dict(row)
            # Декодуємо JSON рядок для tp_order_ids
            try:
                tp_ids = position_dict.get('tp_order_ids')
                position_dict['tp_order_ids'] = json.loads(tp_ids) if tp_ids else [] # Обробка None
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[DataManager] Не вдалося декодувати tp_order_ids для позиції ID {position_dict.get('id')}. Значення: {position_dict.get('tp_order_ids')}")
                position_dict['tp_order_ids'] = [] # Повертаємо пустий список у разі помилки
            positions.append(position_dict)
        logger.debug(f"[DataManager] Отримано {len(positions)} активних позицій з БД.")
        return positions
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при отриманні активних позицій: {e}", exc_info=True)
        return []

def get_position_by_id(conn: sqlite3.Connection, position_id: int) -> Optional[Dict[str, Any]]:
    """Повертає дані конкретної позиції за її ID."""
    sql = "SELECT * FROM active_positions WHERE id = ?"
    try:
        cursor = conn.cursor()
        cursor.execute(sql, (position_id,))
        row = cursor.fetchone()
        if row:
            position_dict = dict(row)
            try:
                tp_ids = position_dict.get('tp_order_ids')
                position_dict['tp_order_ids'] = json.loads(tp_ids) if tp_ids else []
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"[DataManager] Не вдалося декодувати tp_order_ids для позиції ID {position_id}. Значення: {position_dict.get('tp_order_ids')}")
                position_dict['tp_order_ids'] = []
            logger.debug(f"[DataManager] Отримано дані для позиції ID {position_id}.")
            return position_dict
        else:
            logger.warning(f"[DataManager] Позицію з ID {position_id} не знайдено.")
            return None
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при отриманні позиції ID {position_id}: {e}", exc_info=True)
        return None

def _update_position_field(conn: sqlite3.Connection, position_id: int, field_name: str, value: Any) -> bool:
    """Внутрішня функція для оновлення одного поля позиції."""
    # Захист від SQL ін'єкції при формуванні імені поля
    allowed_fields = ['is_breakeven', 'related_limit_order_id', 'is_active', 'status_info', 'current_amount', 'sl_order_id', 'tp_order_ids']
    if field_name not in allowed_fields:
        logger.error(f"[DataManager] Спроба оновити недозволене поле: {field_name}")
        return False
        
    # Оновлюємо також updated_at (хоча тригер теж мав би спрацювати)
    sql = f"UPDATE active_positions SET {field_name} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
    try:
        cursor = conn.cursor()
        # Спеціальна обробка для tp_order_ids (перетворення в JSON)
        value_to_update = json.dumps(value) if field_name == 'tp_order_ids' else value
        cursor.execute(sql, (value_to_update, position_id))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"[DataManager] Успішно оновлено поле '{field_name}' для позиції ID {position_id}.")
            return True
        else:
            logger.warning(f"[DataManager] Позицію ID {position_id} не знайдено під час оновлення поля '{field_name}'.")
            return False
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при оновленні поля '{field_name}' для позиції ID {position_id}: {e}", exc_info=True)
        conn.rollback()
        return False

def update_position_breakeven(conn: sqlite3.Connection, position_id: int, is_breakeven: bool) -> bool:
    """Оновлює флаг is_breakeven (0 або 1)."""
    return _update_position_field(conn, position_id, 'is_breakeven', 1 if is_breakeven else 0)

def update_position_limit_order(conn: sqlite3.Connection, position_id: int, limit_order_id: Optional[str]) -> bool:
    """Оновлює related_limit_order_id."""
    return _update_position_field(conn, position_id, 'related_limit_order_id', limit_order_id)

def update_position_status(conn: sqlite3.Connection, position_id: int, is_active: bool, status_info: str = '') -> bool:
    """Оновлює флаг is_active (0 або 1) та status_info."""
    sql = "UPDATE active_positions SET is_active = ?, status_info = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
    try:
        cursor = conn.cursor()
        cursor.execute(sql, (1 if is_active else 0, status_info, position_id))
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"[DataManager] Успішно оновлено статус is_active={is_active}, status='{status_info}' для позиції ID {position_id}.")
            return True
        else:
            logger.warning(f"[DataManager] Позицію ID {position_id} не знайдено під час оновлення статусу.")
            return False
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при оновленні статусу позиції ID {position_id}: {e}", exc_info=True)
        conn.rollback()
        return False

def update_position_amount(conn: sqlite3.Connection, position_id: int, new_amount: float) -> bool:
    """Оновлює поточний обсяг (current_amount) для позиції."""
    return _update_position_field(conn, position_id, 'current_amount', new_amount)

def get_active_position_count(conn: sqlite3.Connection, channel_group: str) -> int:
    """Підраховує кількість активних (НЕ в ББ) позицій для групи каналів.

    Args:
        conn: З'єднання з БД.
        channel_group: 'group_1_2_4' або 'channel_3'.

    Returns:
        Кількість таких позицій.
    """
    if channel_group == 'group_1_2_4':
        # Рахуємо позиції з каналів 1, 2, 4, які активні І ще НЕ в беззбитковості
        sql = "SELECT COUNT(id) FROM active_positions WHERE is_active = 1 AND is_breakeven = 0 AND signal_channel_key IN ('channel_1', 'channel_2', 'channel_4')"
        params = ()
    elif channel_group == 'channel_3':
        # Рахуємо ВСІ активні позиції з каналу 3 (ліміт для нього не залежить від ББ)
        sql = "SELECT COUNT(id) FROM active_positions WHERE is_active = 1 AND signal_channel_key = 'channel_3'"
        params = ()
    else:
        logger.error(f"[DataManager] Невідома група каналів для підрахунку: {channel_group}")
        return -1 # Повертаємо -1 як ознаку помилки

    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        count = cursor.fetchone()[0]
        logger.debug(f"[DataManager] Знайдено {count} активних позицій для групи '{channel_group}'.")
        return count if count is not None else 0
    except sqlite3.Error as e:
        logger.error(f"[DataManager] Помилка при підрахунку активних позицій для групи '{channel_group}': {e}", exc_info=True)
        return -1

def get_total_active_position_count(db_conn: sqlite3.Connection) -> int:
    """Повертає загальну кількість активних позицій."""
    try:
        cursor = db_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM active_positions WHERE is_active = 1")
        count = cursor.fetchone()[0]
        logger.debug(f"Знайдено {count} активних позицій.") # Додамо лог
        return count
    except sqlite3.Error as e:
        logger.error(f"Помилка отримання загальної кількості активних позицій: {e}", exc_info=True)
        return -1 # Повертаємо -1 у разі помилки

def update_position_sl_and_breakeven(db_conn: sqlite3.Connection, position_id: int, new_sl_order_id: str, is_breakeven: int) -> bool:
    """Оновлює SL ордер ID та статус беззбитковості для позиції."""
    try:
        cursor = db_conn.cursor()
        cursor.execute("""
            UPDATE active_positions
            SET sl_order_id = ?, is_breakeven = ?
            WHERE id = ? AND is_active = 1
        """, (new_sl_order_id, is_breakeven, position_id))
        db_conn.commit()
        updated_rows = cursor.rowcount
        if updated_rows > 0:
            logger.info(f"Оновлено SL ID на {new_sl_order_id} та is_breakeven на {is_breakeven} для позиції {position_id}.")
            return True
        else:
            logger.warning(f"Спроба оновити SL/ББ для неіснуючої або неактивної позиції {position_id}.")
            return False
    except sqlite3.Error as e:
        logger.error(f"Помилка оновлення SL ID та статусу ББ для позиції {position_id}: {e}", exc_info=True)
        return False

# --- Приклад використання --- 
if __name__ == '__main__':
    # Налаштування логування
    log_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    logging.basicConfig(level=logging.DEBUG, format=log_format)
    main_logger = logging.getLogger("DataManagerTest")

    # 1. Ініціалізація БД
    main_logger.info("Ініціалізація бази даних...")
    conn = get_db_connection()
    if not conn:
        exit(1)
        
    if not initialize_database(conn):
        main_logger.error("Не вдалося ініціалізувати БД.")
        conn.close()
        exit(1)

    # 2. Додавання тестової позиції
    main_logger.info("\nДодавання тестової позиції...")
    test_position_data = {
        'signal_channel_key': 'channel_1',
        'symbol': 'BTC/USDT:USDT',
        'position_side': 'LONG',
        'entry_price': 65000.50,
        'initial_amount': 0.0015,
        'current_amount': 0.0015, # Встановлюємо при створенні
        'initial_margin': 97.5,
        'leverage': 10,
        'sl_order_id': 'sl_test_123',
        'tp_order_ids': ['tp1_test_abc', 'tp2_test_def'], # Передаємо як список
        'related_limit_order_id': None
    }
    pos_id = add_new_position(conn, test_position_data)
    if pos_id:
        main_logger.info(f"Створено позицію з ID: {pos_id}")
    else:
        main_logger.error("Не вдалося створити позицію.")

    # 3. Додавання ще однієї позиції (інший канал)
    main_logger.info("\nДодавання позиції для каналу 3...")
    test_position_data_c3 = {
        'signal_channel_key': 'channel_3',
        'symbol': 'ETH/USDT:USDT',
        'position_side': 'SHORT',
        'entry_price': 3500.0,
        'initial_amount': 0.05,
        'current_amount': 0.05,
        'leverage': 20,
        'sl_order_id': 'sl_eth_456',
        'tp_order_ids': ['tp_eth_1', 'tp_eth_2'],
        'related_limit_order_id': 'limit_eth_789'
    }
    pos_id_c3 = add_new_position(conn, test_position_data_c3)
    
    # 4. Отримання всіх активних позицій
    main_logger.info("\nОтримання всіх активних позицій...")
    active_positions = get_active_positions(conn)
    if active_positions:
        main_logger.info(f"Знайдено {len(active_positions)} активних позицій:")
        for p in active_positions:
            print(f"  ID: {p['id']}, Symbol: {p['symbol']}, Side: {p['position_side']}, Amount: {p['current_amount']}, Breakeven: {p['is_breakeven']}, TP IDs: {p['tp_order_ids']}")
    else:
        main_logger.info("Активних позицій не знайдено.")

    # 5. Отримання позиції за ID
    if pos_id:
        main_logger.info(f"\nОтримання позиції ID={pos_id}...")
        position = get_position_by_id(conn, pos_id)
        if position:
             print(f"  Дані позиції {pos_id}: {position}")
        else:
             main_logger.warning(f"Позицію {pos_id} не знайдено.")

    # 6. Оновлення статусу ББ для першої позиції
    if pos_id:
        main_logger.info(f"\nОновлення статусу ББ для позиції ID={pos_id}...")
        if update_position_breakeven(conn, pos_id, True):
            main_logger.info("Статус ББ оновлено.")
            # Перевірка
            position = get_position_by_id(conn, pos_id)
            if position:
                 print(f"  Новий статус ББ: {position['is_breakeven']}")
        else:
            main_logger.error("Не вдалося оновити статус ББ.")

    # 7. Оновлення лімітного ордера для другої позиції (скасування)
    if pos_id_c3:
        main_logger.info(f"\nОновлення лімітного ордера (скасування) для позиції ID={pos_id_c3}...")
        update_position_limit_order(conn, pos_id_c3, None)

    # 8. Оновлення суми для першої позиції (частковий TP)
    if pos_id:
         main_logger.info(f"\nОновлення суми для позиції ID={pos_id}...")
         update_position_amount(conn, pos_id, 0.0005)

    # 9. Підрахунок активних слотів
    main_logger.info("\nПідрахунок активних слотів...")
    count_124 = get_active_position_count(conn, 'group_1_2_4')
    count_3 = get_active_position_count(conn, 'channel_3')
    main_logger.info(f"Активні слоти (не в ББ) для групи 1,2,4: {count_124}")
    main_logger.info(f"Активні слоти для каналу 3: {count_3}")

    # 10. Закриття позиції
    if pos_id:
        main_logger.info(f"\nЗакриття позиції ID={pos_id}...")
        if update_position_status(conn, pos_id, False, 'closed_all_tp'):
            main_logger.info("Позицію позначено як неактивну.")
        else:
            main_logger.error("Не вдалося закрити позицію.")
            
    # Перевірка активних позицій після закриття
    main_logger.info("\nПеревірка активних позицій після закриття...")
    active_positions = get_active_positions(conn)
    main_logger.info(f"Залишилось {len(active_positions)} активних позицій.")
    if active_positions:
         for p in active_positions:
            print(f"  ID: {p['id']}, Symbol: {p['symbol']}")

    # Закриття з'єднання
    if conn:
        conn.close()
        main_logger.info("\nЗ'єднання з БД закрито.")
