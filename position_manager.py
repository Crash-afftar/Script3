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
    def __init__(self, bingx_api: BingXClient, config: dict):
        self.logger = logging.getLogger(__name__)
        self.bingx_api = bingx_api
        self.config = config
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
        self.logger.info("[PositionManager] Початок циклу моніторингу (в окремому потоці).")
        
        # Створюємо з'єднання з БД СПЕЦІАЛЬНО для цього потоку
        db_conn_thread: Optional[sqlite3.Connection] = None 
        try:
            db_conn_thread = data_manager.get_db_connection()
            if not db_conn_thread:
                 self.logger.critical("[PositionManager] Не вдалося створити з'єднання з БД для потоку моніторингу. Зупинка потоку.")
                 return # Зупиняємо потік
                 
            self.logger.info("[PositionManager] З'єднання з БД для потоку моніторингу створено.")

            while not self.stop_event.is_set():
                start_time = time.time()
                try:
                    self.logger.debug("[PositionManager] Початок ітерації перевірки стану позицій...") 
                    # 1. Отримати список активних позицій з БД (використовуємо з'єднання потоку)
                    active_positions = data_manager.get_active_positions(db_conn_thread)
                    if not active_positions:
                        self.logger.debug("[PositionManager] Немає активних позицій для моніторингу.") 
                    else:
                        self.logger.info(f"[PositionManager] Знайдено {len(active_positions)} активних позицій для перевірки.")
                        # 2. Для кожної позиції перевірити її стан
                        for position in active_positions:
                            if self.stop_event.is_set(): 
                                self.logger.info("[PositionManager] Отримано сигнал зупинки під час обробки позицій.")
                                break 
                            time.sleep(self.config.get('position_manager', {}).get('api_request_delay', 0.2)) 
                            # Передаємо з'єднання потоку в функцію перевірки
                            self._check_and_update_position_status(position, db_conn_thread)
                    
                    if self.stop_event.is_set():
                        break
                         
                    self.logger.debug("[PositionManager] Ітерацію перевірки стану позицій завершено.") 
                    
                except sqlite3.Error as db_err:
                     self.logger.critical(f"[PositionManager] Помилка бази даних в циклі моніторингу: {db_err}", exc_info=True)
                     self.stop_event.set() 
                     break 
                except Exception as e:
                    self.logger.error(f"[PositionManager] Неочікувана помилка в циклі моніторингу: {e}", exc_info=True)
                
                # Розрахунок часу очікування
                elapsed_time = time.time() - start_time
                wait_time = max(0, self.check_interval_seconds - elapsed_time)
                self.logger.debug(f"[PositionManager] Цикл завершено за {elapsed_time:.2f} сек. Очікування {wait_time:.2f} сек...")
                interrupted = self.stop_event.wait(wait_time)
                if interrupted:
                     self.logger.info("[PositionManager] Очікування перервано сигналом зупинки.")
                     break 
                     
        finally:
            # Гарантовано закриваємо з'єднання потоку при виході з циклу/потоку
            if db_conn_thread:
                 self.logger.info("[PositionManager] Закриття з'єднання з БД для потоку моніторингу.")
                 db_conn_thread.close()
            self.logger.info("[PositionManager] Цикл моніторингу завершено.")

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

    def _check_and_update_position_status(self, position_data: Dict[str, Any], db_conn: sqlite3.Connection):
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
                 self._handle_position_closed(position_id, 'stop_loss_hit', position_data, db_conn, sl_order_info)
                 return # Позиція закрита, виходимо
             else:
                 self.logger.warning(f"[PositionManager] SL ордер {sl_order_id} має статус 'closed', але filled=0. Можливо, скасовано? Ігноруємо поки що.")
                 self._handle_position_closed(position_id, 'sl_closed_no_fill_pos_gone', position_data, db_conn, sl_order_info)
                 return
        elif sl_status == 'canceled':
             # Розглядати скасований SL як закриття позиції? Залежить від логіки біржі.
             # Поки що логуємо як попередження.
             self.logger.warning(f"[PositionManager] SL ордер {sl_order_id} для позиції {position_id} має статус 'canceled'. Позиція може бути ще активна або ліквідована! Потрібна додаткова перевірка позиції.")
             # TODO: Додати перевірку самої позиції через fetch_positions?
             self._handle_position_closed(position_id, 'position_not_found_after_sl_cancel', position_data, db_conn, sl_order_info)
             return

        # --- Перевірка спрацювання TP --- 
        remaining_amount = current_amount # Поточний залишок позиції
        closed_tp_ids = [] # Список ID TP, які спрацювали в цьому циклі
        all_tp_closed_or_irrelevant = True # Флаг, що всі TP або закриті, або їх немає
        any_tp_filled_or_closed = False # Флаг, що хоча б один TP спрацював/закрився

        if not tp_order_ids:
            all_tp_closed_or_irrelevant = True
        else:
            for tp_id in tp_order_ids:
                tp_info = tp_orders_info.get(tp_id)
                tp_status = tp_info.get('status') if tp_info else 'unknown'
                # ВАЖЛИВО: Використовуємо amount ордера для розрахунку зменшення,
                # оскільки filled може бути 0 для закритих TP_MARKET ордерів
                tp_order_amount = tp_info.get('amount', 0) if tp_info else 0

                # Вважаємо TP спрацьованим, якщо він 'closed' (або 'FILLED' в API)
                if tp_status == 'closed':
                    self.logger.info(f"[PositionManager] Take Profit (ID: {tp_id}) для позиції {position_id} ({symbol}) має статус 'closed'. Обсяг ордера: {tp_order_amount}")
                    closed_tp_ids.append(tp_id)
                    # Зменшуємо залишок на обсяг *цього* TP ордера
                    if tp_order_amount > 0:
                         remaining_amount -= tp_order_amount
                    any_tp_filled_or_closed = True # Зафіксували спрацювання/закриття TP
                elif tp_status == 'open' or tp_status == 'new':
                    # Якщо хоча б один TP ще відкритий, то не всі закриті
                    all_tp_closed_or_irrelevant = False
                # Інші статуси ('canceled', 'expired', 'rejected', 'unknown') ігноруємо для розрахунку remaining_amount,
                # але вони не заважають all_tp_closed_or_irrelevant стати True, якщо немає активних.

        # Захист від від'ємного залишку через можливі неточності
        remaining_amount = max(0, remaining_amount)

        # Оновлюємо поточний обсяг в БД, якщо були закриття TP (closed_tp_ids не порожній)
        if closed_tp_ids:
            # Оновлюємо тільки якщо розрахунковий remaining_amount відрізняється від поточного в БД
            # Порівняння float потребує обережності
            if abs(current_amount - remaining_amount) > 1e-9: # Якщо є зміна
                # Переконуємось, що функція update_position_amount існує
                update_amount_ok = data_manager.update_position_amount(db_conn, position_id, remaining_amount)
                if update_amount_ok:
                     self.logger.info(f"[PositionManager] Оновлено current_amount для позиції {position_id} на {remaining_amount:.8f}")
                else:
                     self.logger.error(f"[PositionManager] Не вдалося оновити current_amount для позиції {position_id}!")
            else:
                 self.logger.debug(f"[PositionManager] Розрахунковий remaining_amount ({remaining_amount:.8f}) не змінився суттєво порівняно з БД ({current_amount:.8f}). Оновлення БД не потрібне.")

            # --- Логіка переміщення SL в ББ ---
            # Умови: SL ще не в ББ, хоча б один TP закрився, SL ордер ще активний
            if not is_breakeven and any_tp_filled_or_closed and (sl_status == 'open' or sl_status == 'new'):
                self.logger.info(f"[PositionManager] Спрацював TP для позиції {position_id}. Переміщення SL в ББ (ціна: {entry_price})...")

                old_sl_order_id = sl_order_id # Зберігаємо старий ID для скасування
                new_sl_order = None
                new_sl_order_id_str = None
                new_sl_success = False
                cancel_verified = False # Буде True тільки після успішної верифікації

                # 1. Спроба скасувати старий SL
                if old_sl_order_id:
                    self.logger.info(f"[PM ББ] Крок 1: Спроба скасування старого SL ордера ID: {old_sl_order_id}...")
                    cancel_attempt_finished = False
                    try:
                        # --- Додано блок перевірки статусу перед скасуванням ---
                        self.logger.debug(f"[PM ББ] Перевірка статусу SL {old_sl_order_id} ПЕРЕД скасуванням...")
                        pre_cancel_info = self._fetch_order_status(symbol, old_sl_order_id)
                        pre_cancel_status = pre_cancel_info.get('status') if pre_cancel_info else 'error'
                        self.logger.debug(f"[PM ББ] Статус SL {old_sl_order_id} ПЕРЕД скасуванням: {pre_cancel_status}")
                        # --- Кінець блоку перевірки ---

                        # Скасовуємо тільки якщо він ще 'open' або 'new'
                        if pre_cancel_status in ['open', 'new']:
                            cancel_result = self.bingx_api.cancel_order(symbol, old_sl_order_id)
                            if cancel_result: # Якщо функція повернула True
                                self.logger.info(f"[PM ББ] Спроба скасування {old_sl_order_id} повернула успіх (буде верифіковано).)")
                            else:
                                self.logger.warning(f"[PM ББ] Спроба скасування {old_sl_order_id} повернула False без помилки (буде верифіковано).)")
                        elif pre_cancel_status in ['canceled', 'closed']:
                             self.logger.info(f"[PM ББ] SL {old_sl_order_id} вже був неактивний ({pre_cancel_status}) перед спробою скасування. Продовжуємо до верифікації.")
                             # Вважаємо спробу "завершеною", хоча фактичного виклику cancel не було
                        elif pre_cancel_status == 'error':
                             self.logger.warning(f"[PM ББ] Не вдалося отримати статус SL {old_sl_order_id} перед скасуванням (ймовірно, не існує). Продовжуємо до верифікації.")
                        else: # Невідомий статус
                             self.logger.warning(f"[PM ББ] Невідомий статус SL {old_sl_order_id} ({pre_cancel_status}) перед скасуванням. Продовжуємо до верифікації.")

                        cancel_attempt_finished = True # Позначаємо, що етап спроби завершено

                    except Exception as cancel_err:
                        cancel_attempt_finished = True
                        error_code = getattr(cancel_err, 'code', None)
                        error_message = str(cancel_err).lower()
                        # Важливо: помилка 109414 при СПРОБІ скасування тепер не є критичною,
                        # бо ми все одно перевіримо статус на кроці верифікації.
                        if error_code == 109414 or 'order not exist' in error_message:
                            self.logger.warning(f"[PM ББ] Спроба скасування {old_sl_order_id} отримала помилку 'Order not exist' (109414). Продовжуємо до верифікації.")
                        else:
                            self.logger.error(f"[PM ББ] Неочікувана помилка при спробі скасування SL {old_sl_order_id}: {cancel_err}", exc_info=True)
                            # У випадку неочікуваної помилки - не продовжуємо ББ в цій ітерації
                            cancel_verified = False # Залишаємо як False
                            new_sl_success = False # І новий не створюємо
                            # Важливо: не виходимо, а даємо дійти до кінця логіки перевірки DB

                    # --- Крок 1.5: Верифікація скасування ---
                    # Виконується завжди, якщо була спроба або перевірка статусу перед спробою
                    if cancel_attempt_finished:
                        self.logger.info(f"[PM ББ] Крок 1.5: Верифікація статусу старого SL ордера {old_sl_order_id} ПІСЛЯ спроби скасування...")
                        time.sleep(1) # Невелика пауза перед перевіркою
                        verification_order_info = self._fetch_order_status(symbol, old_sl_order_id)
                        verification_status = verification_order_info.get('status') if verification_order_info else 'error' # error, якщо fetch не вдався

                        if verification_status in ['canceled', 'closed']:
                            self.logger.info(f"[PM ББ] Верифікація: Старий SL {old_sl_order_id} підтверджено як неактивний (статус: {verification_status}). Скасування вважається успішним.")
                            cancel_verified = True
                        elif verification_status == 'error':
                            self.logger.warning(f"[PM ББ] Верифікація: Не вдалося отримати статус старого SL {old_sl_order_id} ПІСЛЯ спроби скасування (ймовірно, дійсно не існує). Вважаємо скасування успішним.")
                            cancel_verified = True
                        elif verification_status == 'open':
                            self.logger.warning(f"[PM ББ] Верифікація: Старий SL {old_sl_order_id} все ще має статус 'open' ПІСЛЯ спроби скасування. Скасування НЕ вдалося. Створення нового SL пропускається.")
                            cancel_verified = False
                        else: # unknown etc.
                            self.logger.warning(f"[PM ББ] Верифікація: Старий SL {old_sl_order_id} має невідомий/неочікуваний статус '{verification_status}' ПІСЛЯ спроби скасування. Скасування НЕ вдалося.")
                            cancel_verified = False
                    # else: Якщо спроба скасування не була завершена (малоймовірно)
                        # cancel_verified залишається False

                else: # Якщо old_sl_order_id відсутній у БД
                     self.logger.warning(f"[PM ББ] Немає ID старого SL для скасування позиції {position_id}. Вважаємо цей крок успішним.")
                     cancel_verified = True # Старого SL немає, значить скасовувати не потрібно

                # 2. Створити новий SL в ББ (тільки якщо скасування ВЕРИФІКОВАНО)
                if cancel_verified:
                    # --- Додаємо невелику паузу перед створенням нового SL ---
                    self.logger.debug("[PM ББ] Пауза 2 секунди перед створенням нового SL...")
                    time.sleep(2)
                    # --- Кінець паузи ---

                    # Використовуємо залишковий обсяг `remaining_amount`
                    if remaining_amount > 1e-9: # Перевіряємо, чи є що захищати
                        self.logger.info(f"[PM ББ] Крок 2: Створення нового SL ордера в ББ ({entry_price}) для залишку {remaining_amount:.8f}...")
                        new_sl_order = self.bingx_api.set_stop_loss(
                            symbol=symbol,
                            position_side=position_data['position_side'],
                            sl_price=entry_price,
                            amount=remaining_amount
                        )
                        if new_sl_order and new_sl_order.get('id'):
                            new_sl_order_id_str = str(new_sl_order.get('id')) # Перетворюємо на рядок
                            self.logger.info(f"[PM ББ] Новий SL ордер успішно створено. ID: {new_sl_order_id_str}")
                            new_sl_success = True
                        else:
                             # Використовуємо покращене логування
                             error_details = getattr(new_sl_order, 'last_json_response', str(new_sl_order))
                             self.logger.error(f"[PM ББ] НЕ вдалося створити новий SL ордер в ББ для позиції {position_id}. Залишок: {remaining_amount:.8f}, Ціна ББ: {entry_price}. Відповідь API/деталі: {error_details}")
                             new_sl_success = False # Явно ставимо False
                    else:
                        self.logger.warning(f"[PM ББ] Залишок позиції {position_id} ({remaining_amount:.8f}) занадто малий для створення нового SL в ББ.")
                        # Вважаємо операцію ББ умовно успішною, бо позиція майже закрита, старий SL скасовано.
                        new_sl_success = True
                        new_sl_order_id_str = 'None' # Немає нового ID

                # 3. Оновити БД, якщо все вдалося (скасування ВЕРИФІКОВАНО + створення нового SL або підтвердження малого залишку)
                if cancel_verified and new_sl_success:
                    self.logger.info(f"[PM ББ] Крок 3: Оновлення даних позиції {position_id} в БД (новий SL ID: {new_sl_order_id_str}, is_breakeven: 1)...")
                    update_ok = data_manager.update_position_sl_and_breakeven(db_conn, position_id, new_sl_order_id_str, 1)

                    if update_ok:
                        self.logger.info(f"[PM ББ] Дані позиції {position_id} успішно оновлено в БД.")
                        # Оновлюємо локальні змінні для подальшої логіки в цьому циклі
                        sl_order_id = new_sl_order_id_str
                        is_breakeven = True
                    else:
                        self.logger.error(f"[PM ББ] Не вдалося оновити дані позиції {position_id} в БД!")
                        # Поки що просто логуємо.
                else:
                     self.logger.error(f"[PM ББ] Пропуск оновлення БД для позиції {position_id}, оскільки не всі кроки ББ були успішними (cancel_verified={cancel_verified}, new_sl_success={new_sl_success}).")

            # --- Кінець блоку переміщення в ББ ---

