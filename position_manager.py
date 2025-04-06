# Position and Order Management Module
import time
import logging
import threading
import sqlite3
import json
import datetime
from typing import List, Dict, Optional, Any
from bingx_client import BingXClient  # Припускаємо, що ваш клієнт BingX тут
# TODO: Додати імпорт бази даних або сховища стану

# Імпортуємо функції з data_manager
import data_manager 

class PositionManager:
    def __init__(self, bingx_api: BingXClient, config: dict, db_conn: sqlite3.Connection): # Змінено db_connection на db_conn
        self.logger = logging.getLogger(__name__)
        self.bingx_api = bingx_api
        self.config = config
        self.db = db_conn # Зберігаємо реальне з'єднання
        self.stop_event = threading.Event()
        self.thread = None
        # Отримуємо інтервал з конфігу, або значення за замовчуванням
        self.check_interval_seconds = config.get('position_manager', {}).get('check_interval_seconds', 60)
        self.logger.info(f"[PositionManager] Інтервал перевірки стану: {self.check_interval_seconds} секунд.")
        # TODO: Завантажити активні позиції з БД при старті? (Можливо, не потрібно, цикл сам їх підхопить)

    def start_monitoring(self):
        """Запускає потік моніторингу позицій."""
        if self.thread is not None and self.thread.is_alive():
            self.logger.warning("[PositionManager] Спроба запустити моніторинг, коли він вже працює.")
            return
            
        self.logger.info("[PositionManager] Запуск потоку моніторингу...")
        self.stop_event.clear()
        # Передаємо db_conn в цикл моніторингу, якщо він потрібен напряму (хоча він є в self.db)
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        self.logger.info("[PositionManager] Потік моніторингу запущено.")

    def stop_monitoring(self):
        """Зупиняє потік моніторингу."""
        if self.thread is None or not self.thread.is_alive():
            self.logger.warning("[PositionManager] Спроба зупинити моніторинг, коли він не працює.")
            return
            
        self.logger.info("[PositionManager] Зупинка потоку моніторингу...")
        self.stop_event.set()
        # Збільшимо таймаут на випадок довгих запитів до API
        join_timeout = self.check_interval_seconds * 1.5 
        self.thread.join(timeout=join_timeout) 
        if self.thread.is_alive():
             self.logger.warning(f"[PositionManager] Потік моніторингу не завершився за {join_timeout} сек.")
        else:
             self.logger.info("[PositionManager] Потік моніторингу успішно зупинено.")
        self.thread = None

    def _monitor_loop(self):
        """Головний цикл моніторингу стану позицій та ордерів."""
        self.logger.info("[PositionManager] Початок циклу моніторингу.")
        while not self.stop_event.is_set():
            start_time = time.time()
            try:
                self.logger.debug("[PositionManager] Початок ітерації перевірки стану позицій...") # Змінено на DEBUG
                # 1. Отримати список активних позицій з БД
                active_positions = data_manager.get_active_positions(self.db)
                if not active_positions:
                     self.logger.debug("[PositionManager] Немає активних позицій для моніторингу.") # Змінено на DEBUG
                else:
                    self.logger.info(f"[PositionManager] Знайдено {len(active_positions)} активних позицій для перевірки.")
                    # 2. Для кожної позиції перевірити її стан
                    for position in active_positions:
                        # Додамо невелику паузу між обробкою позицій, щоб не перевантажувати API
                        if self.stop_event.is_set(): 
                            self.logger.info("[PositionManager] Отримано сигнал зупинки під час обробки позицій.")
                            break 
                        # Використовуємо меншу паузу, можливо 0.1-0.2 сек достатньо?
                        time.sleep(self.config.get('position_manager', {}).get('api_request_delay', 0.2))
                        self._check_and_update_position_status(position)
                
                # Якщо цикл обробки позицій був перерваний сигналом зупинки, виходимо з _monitor_loop
                if self.stop_event.is_set():
                     break
                     
                self.logger.debug("[PositionManager] Ітерацію перевірки стану позицій завершено.") # Змінено на DEBUG
                
            except sqlite3.Error as db_err:
                 # Окремо обробляємо помилки БД
                 self.logger.critical(f"[PositionManager] Помилка бази даних в циклі моніторингу: {db_err}", exc_info=True)
                 # Зупиняємо моніторинг, оскільки без БД він не може працювати коректно
                 self.stop_event.set() # Сигналізуємо про зупинку
                 # TODO: Додати сповіщення про критичну помилку БД
                 break # Виходимо з циклу while
            except Exception as e:
                self.logger.error(f"[PositionManager] Неочікувана помилка в циклі моніторингу: {e}", exc_info=True)
                # Не зупиняємо цикл через інші помилки, спробуємо наступну ітерацію
            
            # Розраховуємо час очікування до наступної перевірки
            elapsed_time = time.time() - start_time
            wait_time = max(0, self.check_interval_seconds - elapsed_time)
            self.logger.debug(f"[PositionManager] Цикл завершено за {elapsed_time:.2f} сек. Очікування {wait_time:.2f} сек...") # Змінено на DEBUG
            # Використовуємо wait() для можливості переривання під час очікування
            interrupted = self.stop_event.wait(wait_time)
            if interrupted:
                 self.logger.info("[PositionManager] Очікування перервано сигналом зупинки.")
                 break # Виходимо з циклу while
            
        self.logger.info("[PositionManager] Цикл моніторингу завершено." )

    def _fetch_order_status(self, symbol: str, order_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Допоміжна функція для отримання статусу ордера з обробкою помилок."""
        if not order_id:
            return None # Немає ID - немає чого перевіряти
        try:
            # Використовуємо метод з bingx_client
            order_info = self.bingx_api.fetch_order(symbol, order_id)
            return order_info 
        except Exception as e:
            # Логуємо помилку, але не перериваємо цикл через один ордер
            self.logger.error(f"[PositionManager] Помилка при отриманні статусу ордера ID {order_id} для {symbol}: {e}", exc_info=False)
            return None # Повертаємо None у разі будь-якої помилки запиту

    def _check_and_update_position_status(self, position_data: Dict[str, Any]):
        """Перевіряє стан конкретної позиції та її ордерів на біржі."""
        position_id = position_data['id']
        symbol = position_data['symbol']
        is_breakeven = bool(position_data['is_breakeven'])
        entry_price = position_data['entry_price']
        sl_order_id = position_data['sl_order_id']
        tp_order_ids = position_data.get('tp_order_ids', []) # Переконуємось, що це список
        source_channel_key = position_data['signal_channel_key']
        limit_order_id_c3 = position_data.get('related_limit_order_id') 
        current_amount = position_data['current_amount']
        
        self.logger.info(f"[PM Check] ID={position_id}, Символ={symbol}, Канал={source_channel_key}, ББ={is_breakeven}, SL_ID={sl_order_id}, TP_IDs={tp_order_ids}")

        # --- Отримуємо статус ордерів --- 
        # Отримуємо статус SL
        sl_order_info = self._fetch_order_status(symbol, sl_order_id)
        sl_status = sl_order_info.get('status') if sl_order_info else 'unknown' # unknown, якщо не вдалось отримати
        self.logger.debug(f"[PM Check ID={position_id}] SL статус: {sl_status} (Info: {sl_order_info})")

        # Отримуємо статус TP ордерів (тільки якщо є ID)
        tp_orders_info = {}
        if tp_order_ids:
            for tp_id in tp_order_ids:
                 if self.stop_event.is_set(): return # Перевірка зупинки
                 time.sleep(0.1) # Маленька пауза між запитами TP
                 info = self._fetch_order_status(symbol, tp_id)
                 tp_orders_info[tp_id] = info
                 self.logger.debug(f"[PM Check ID={position_id}] TP статус ({tp_id}): {info.get('status') if info else 'unknown'}")
        
        # --- Перевірка спрацювання SL --- 
        # Спрацював, якщо статус 'closed' і є виконаний обсяг (filled)
        # Або якщо статус 'canceled' (можливо, скасовано біржею при ліквідації?)
        if sl_status == 'closed':
             filled_amount = sl_order_info.get('filled', 0)
             average_price = sl_order_info.get('average') # Ціна виконання
             if filled_amount > 0:
                 self.logger.info(f"[PositionManager] Позиція ID={position_id} ({symbol}) ЗАКРИТА по Stop Loss (ID: {sl_order_id}). Виконано: {filled_amount} @ {average_price}.")
                 self._handle_position_closed(position_id, 'stop_loss_hit', position_data, sl_order_info)
                 return # Позиція закрита, виходимо
             else:
                 self.logger.warning(f"[PositionManager] SL ордер {sl_order_id} має статус 'closed', але filled=0. Можливо, скасовано? Ігноруємо поки що.")
        elif sl_status == 'canceled':
             # Розглядати скасований SL як закриття позиції? Залежить від логіки біржі.
             # Поки що логуємо як попередження.
             self.logger.warning(f"[PositionManager] SL ордер {sl_order_id} для позиції {position_id} має статус 'canceled'. Позиція може бути ще активна або ліквідована! Потрібна додаткова перевірка позиції.")
             # TODO: Додати перевірку самої позиції через fetch_positions?

        # --- Перевірка спрацювання TP --- 
        remaining_amount = current_amount # Поточний залишок позиції
        closed_tp_ids = [] # Список ID TP, які спрацювали в цьому циклі
        all_tp_closed_or_irrelevant = True # Флаг, що всі TP або закриті, або їх немає

        if not tp_order_ids:
             all_tp_closed_or_irrelevant = True
        else:
            for tp_id in tp_order_ids:
                tp_info = tp_orders_info.get(tp_id)
                tp_status = tp_info.get('status') if tp_info else 'unknown'
                tp_filled = tp_info.get('filled', 0) if tp_info else 0
                
                if tp_status == 'closed' and tp_filled > 0:
                    # Цей TP спрацював
                    self.logger.info(f"[PositionManager] Take Profit (ID: {tp_id}) для позиції {position_id} ({symbol}) виконано. Обсяг: {tp_filled}")
                    closed_tp_ids.append(tp_id)
                    remaining_amount -= tp_filled # Зменшуємо залишок позиції
                    # TODO: Потрібно перевірити, чи є цей TP першим, і якщо так, перевести в ББ
                    # Припускаємо, що перший ID у списку tp_order_ids - це TP1
                    is_tp1 = (tp_id == tp_order_ids[0])
                    
                    if not is_breakeven and is_tp1:
                        self.logger.info(f"[PositionManager] TP1 (ID: {tp_id}) виконано для позиції {position_id}. Переводимо SL в ББ (ціна: {entry_price})...")
                        
                        # --- Спроба модифікації SL --- 
                        # Використовуємо edit_order з bingx_client
                        edited_sl_order = self.bingx_api.edit_order(symbol=symbol, order_id=sl_order_id, new_price=entry_price)
                        
                        if edited_sl_order:
                            self.logger.info(f"[PositionManager] SL для позиції {position_id} успішно переведено в ББ. Новий ID (якщо змінився): {edited_sl_order.get('id')}")
                            # Оновлюємо статус в БД
                            data_manager.update_position_breakeven(self.db, position_id, True)
                            # Оновлюємо SL ID в БД, якщо він змінився (деякі біржі повертають новий ID)
                            new_sl_id = edited_sl_order.get('id')
                            if new_sl_id and new_sl_id != sl_order_id:
                                 self.logger.info(f"[PositionManager] SL ID для позиції {position_id} оновлено на {new_sl_id}")
                                 data_manager._update_position_field(self.db, position_id, 'sl_order_id', new_sl_id)
                                 sl_order_id = new_sl_id # Оновлюємо локальну змінну для подальших перевірок
                                 
                            # Звільняємо слот (якщо канал 1, 2 або 4)
                            if source_channel_key in ['channel_1', 'channel_2', 'channel_4']:
                                self._release_trading_slot(source_channel_key, position_id)
                                
                            # --- Логіка для Каналу 3: Скасування лімітного ордера --- 
                            if source_channel_key == 'channel_3' and limit_order_id_c3:
                                self.logger.info(f"[PositionManager C3] TP1 виконано, скасування лімітного ордера ID: {limit_order_id_c3}...")
                                cancel_success = self.bingx_api.cancel_order(symbol, limit_order_id_c3)
                                if cancel_success:
                                    self.logger.info(f"[PositionManager C3] Лімітний ордер {limit_order_id_c3} успішно скасовано.")
                                    # Оновлюємо дані позиції в БД
                                    data_manager.update_position_limit_order(self.db, position_id, None)
                                    limit_order_id_c3 = None # Оновлюємо локальну змінну
                                else:
                                    self.logger.warning(f"[PositionManager C3] Не вдалося скасувати лімітний ордер {limit_order_id_c3}.")
                                    
                        else:
                            self.logger.error(f"[PositionManager] Не вдалося перевести SL в ББ для позиції {position_id}. Спроба редагування SL {sl_order_id} не вдалася.")
                            # TODO: Додати логіку Cancel+Create як альтернативу?
                            
                        # Оновлюємо флаг is_breakeven локально, щоб не намагатись знову в цьому циклі
                        is_breakeven = True # Навіть якщо редагування не вдалося, вважаємо, що спроба була
                        
                elif tp_status not in ['closed', 'canceled']:
                     # Якщо хоча б один TP ще не закритий/скасований, то позиція ще не повністю закрита по TP
                     all_tp_closed_or_irrelevant = False

        # --- Оновлення поточного обсягу в БД, якщо були виконані TP --- 
        if closed_tp_ids: # Якщо хоча б один TP спрацював
             # Перевіряємо, чи залишився обсяг > 0 (з урахуванням можливих похибок float)
             if remaining_amount > 1e-9: # Використовуємо мале число замість 0
                 self.logger.info(f"[PositionManager] Оновлення поточного обсягу для позиції {position_id} на {remaining_amount:.8f}")
                 data_manager.update_position_amount(self.db, position_id, remaining_amount)
             else:
                 # Якщо обсяг став нульовим або від'ємним, вважаємо позицію закритою по TP
                 self.logger.info(f"[PositionManager] Розрахунковий залишок обсягу для позиції {position_id} <= 0 ({remaining_amount:.8f}). Вважаємо закритою по TP.")
                 all_tp_closed_or_irrelevant = True # Примусово ставимо флаг
                 # Закриваємо позицію в БД
                 # Збираємо інформацію про всі TP для логування
                 closed_tp_info = {tp_id: tp_orders_info.get(tp_id) for tp_id in tp_order_ids if tp_orders_info.get(tp_id)}
                 self._handle_position_closed(position_id, 'all_tp_hit_calculated', position_data, closed_tp_info)
                 return

        # --- Перевірка повного закриття по TP (якщо SL не спрацював раніше) --- 
        if all_tp_closed_or_irrelevant and not closed_tp_ids: # Додаткова перевірка, якщо remaining_amount не став 0
             # Це може статися, якщо всі TP були скасовані, або їх не було взагалі
             # В цьому випадку позиція НЕ закрита, якщо тільки SL не спрацював
             pass # Просто продовжуємо моніторинг SL
        elif all_tp_closed_or_irrelevant and closed_tp_ids:
             # Якщо всі TP були позначені як closed/canceled І хоча б один спрацював (remaining_amount оброблено вище) 
             # АБО якщо remaining_amount став <= 0
             # То позиція закрита по TP.
             # Функція _handle_position_closed вже викликана вище, якщо remaining_amount <=0
             # Якщо ж remaining_amount > 0, але всі ордери closed/canceled, потрібна додаткова логіка
             # (наприклад, скасувати SL і закрити позицію вручну, якщо біржа не закрила її автоматично)
             # Поки що вважаємо, що якщо remaining_amount > 0, то вона ще активна.
             if remaining_amount > 1e-9:
                  self.logger.warning(f"[PositionManager] Всі TP ордери для позиції {position_id} закриті/скасовані, але розрахунковий залишок > 0 ({remaining_amount:.8f}). Позиція може бути ще активна. Потрібен контроль.")
                  # TODO: Додати логіку перевірки позиції на біржі та можливого ручного закриття залишку.
             else:
                  # Цей випадок вже оброблено вище (коли remaining_amount став <=0)
                  pass 
        
        # Якщо ми дійшли сюди, позиція все ще активна (або сталася помилка)
        self.logger.debug(f"[PositionManager] Перевірку позиції {position_id} ({symbol}) завершено, позиція активна.")

    def _handle_position_closed(self, position_id: int, reason: str, position_data: Dict[str, Any], closing_order_info: Optional[Dict[str, Any]] = None):
        """Обробляє закриття позиції (SL або всі TP)."""
        self.logger.info(f"[PositionManager] Обробка закриття позиції ID={position_id}, Символ={position_data['symbol']}, Причина: {reason}")
        
        # 1. Оновити статус позиції в БД (позначити як неактивну)
        status_info_text = f"{reason} at {datetime.datetime.now().isoformat()}" 
        if closing_order_info:
            status_info_text += f" | Order: {json.dumps(closing_order_info)}"
            
        update_success = data_manager.update_position_status(self.db, position_id, False, status_info_text)
        if not update_success:
             # Критична помилка - не змогли оновити статус в БД!
             self.logger.critical(f"[PositionManager] НЕ ВДАЛОСЯ оновити статус на is_active=False для позиції ID={position_id}!")
             # TODO: Потрібна система сповіщень про такі помилки
             
        # 2. Звільнити слот, ЯКЩО він ще не був звільнений при переведенні в ББ
        # Перевіряємо актуальний статус ББ з БД, оскільки він міг змінитися
        current_position_data = data_manager.get_position_by_id(self.db, position_id)
        if current_position_data:
            is_breakeven_final = bool(current_position_data['is_breakeven'])
            source_channel_key = current_position_data['signal_channel_key']
            if not is_breakeven_final and source_channel_key in ['channel_1', 'channel_2', 'channel_4']:
                self._release_trading_slot(source_channel_key, position_id, was_breakeven=False)
            else:
                # Слот або вже був звільнений, або це канал 3
                self.logger.info(f"[PositionManager] Слот для позиції {position_id} не потребує звільнення зараз (канал={source_channel_key}, ББ={is_breakeven_final})")
        else:
             # Якщо не можемо отримати дані, логуємо помилку, але не падаємо
             self.logger.error(f"[PositionManager] Не вдалося отримати дані для позиції {position_id} при фінальній обробці закриття.")
        
        # 3. Додаткові дії (напр., логування результату, розрахунок PnL - майбутнє)
        self.logger.info(f"[PositionManager] Завершено обробку закриття позиції ID={position_id}.")
        # ...

    def _release_trading_slot(self, source_channel_key: str, position_id: int, was_breakeven: Optional[bool] = None):
        """ЗАГЛУШКА: Звільняє торговий слот.
           РЕАЛІЗАЦІЯ МАЄ БУТИ В MAIN.PY або окремому SlotManager!
        """
        # Ця функція тут лише для логування та позначення місця, де має відбуватися логіка
        # Вона НЕ повинна змінювати стан слотів безпосередньо тут
        status_note = f"(Стала ББ: {was_breakeven})" if was_breakeven is not None else ""
        self.logger.info(f"[PositionManager] СИГНАЛ НА ЗВІЛЬНЕННЯ СЛОТУ для каналу {source_channel_key} (позиція {position_id}) {status_note}")
        # TODO: Реалізувати механізм сповіщення або колбеку до main.py / SlotManager
        pass

# Приклад використання (залишаємо для тестування, але реальна ініціалізація буде в main.py)
if __name__ == '__main__':
    # Налаштування логування
    log_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    logging.basicConfig(level=logging.DEBUG, format=log_format)
    main_logger = logging.getLogger("PositionManagerTest")

    # --- Потрібно завантажити конфіг і .env --- 
    from dotenv import load_dotenv
    import json
    import os

    def load_config(config_path='config.json'):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            main_logger.error(f"Не вдалося завантажити конфіг {config_path}: {e}")
            return {}

    load_dotenv()
    api_key = os.getenv("BINGX_API_KEY")
    api_secret = os.getenv("BINGX_SECRET_KEY")
    config = load_config()
    
    if not api_key or not api_secret:
        main_logger.critical("Не знайдено API ключі в .env")
        exit(1)
        
    db_conn = data_manager.get_db_connection()
    if not db_conn:
         main_logger.critical("Не вдалося підключитися до БД.")
         exit(1)
         
    if not data_manager.initialize_database(db_conn):
         main_logger.critical("Не вдалося ініціалізувати БД.")
         db_conn.close()
         exit(1)

    try:
        main_logger.info("Ініціалізація BingXClient...")
        bingx_client_instance = BingXClient(api_key, api_secret, main_logger)
        
        main_logger.info("Ініціалізація PositionManager...")
        position_manager = PositionManager(bingx_client_instance, config, db_conn)
        
        # --- Додамо тестову позицію в БД, щоб було що моніторити --- 
        main_logger.info("Додавання тестової позиції в БД (якщо ще немає)...")
        test_pos_data = {
             'signal_channel_key': 'channel_1',
             'symbol': 'LTC/USDT:USDT', # Використовуйте реальну пару для тестів
             'position_side': 'LONG',
             'entry_price': 75.0, 
             'initial_amount': 0.1, 
             'current_amount': 0.1, # Має бути таким же спочатку
             'leverage': 10,
             'sl_order_id': 'YOUR_REAL_SL_ORDER_ID_FOR_TESTING', # <-- ВАЖЛИВО: Замініть на реальний ID ордера
             'tp_order_ids': ['YOUR_REAL_TP1_ORDER_ID', 'YOUR_REAL_TP2_ORDER_ID'], # <-- ВАЖЛИВО: Замініть
             'is_breakeven': 0,
             'is_active': 1
        }
        # Перевіримо, чи вже є така позиція (дуже примітивно)
        existing = data_manager.get_active_positions(db_conn)
        if not any(p['symbol'] == test_pos_data['symbol'] for p in existing):
            new_id = data_manager.add_new_position(db_conn, test_pos_data)
            if new_id:
                 main_logger.info(f"Додано тестову позицію з ID {new_id}")
                 # Оновлюємо ID для подальших тестів (не ідеально, але для прикладу)
                 test_pos_data['sl_order_id'] = test_pos_data['sl_order_id'].replace("YOUR_REAL_", str(new_id)+"_")
                 test_pos_data['tp_order_ids'][0] = test_pos_data['tp_order_ids'][0].replace("YOUR_REAL_", str(new_id)+"_")
                 # Потрібно оновити і в БД, якщо ми хочемо симулювати реальні ID
                 data_manager._update_position_field(db_conn, new_id, 'sl_order_id', test_pos_data['sl_order_id'])
                 data_manager._update_position_field(db_conn, new_id, 'tp_order_ids', json.dumps(test_pos_data['tp_order_ids']))
                 main_logger.info(f"(Примітка: ID ордерів у БД/прикладі можуть не відповідати реальним)")
            else:
                 main_logger.error("Не вдалося додати тестову позицію")
        else:
             main_logger.info("Тестова позиція вже існує або інша для цього символу.")
        # ----------------------------------------------------------------

        position_manager.start_monitoring()
        main_logger.info("Менеджер позицій запущено. Натисніть Ctrl+C для зупинки.")
        
        # Тримаємо основний потік живим
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        main_logger.info("Отримано сигнал зупинки (Ctrl+C).")
        if 'position_manager' in locals() and position_manager:
             position_manager.stop_monitoring()
    except Exception as main_err:
        main_logger.critical(f"Критична помилка: {main_err}", exc_info=True)
        if 'position_manager' in locals() and position_manager:
             position_manager.stop_monitoring()
    finally:
         if db_conn:
             db_conn.close()
             main_logger.info("З'єднання з БД закрито.") 