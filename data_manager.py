import time
import logging
import threading
import sqlite3 
import json 
import datetime 
from typing import List, Dict, Optional, Any 

from bingx_client import BingXClient
# Імпортуємо функції з data_manager
import data_manager 

class PositionManager:
    def __init__(self, bingx_api: BingXClient, config: dict, db_conn: sqlite3.Connection):
        self.logger = logging.getLogger(__name__)
        self.bingx_api = bingx_api
        self.config = config
        self.db = db_conn # Зберігаємо реальне з'єднання
        self.stop_event = threading.Event()
        self.thread = None
        # Отримуємо інтервал з конфігу, або значення за замовчуванням
        self.check_interval_seconds = config.get('position_manager', {}).get('check_interval_seconds', 60)
        self.api_request_delay = config.get('position_manager', {}).get('api_request_delay', 0.2) # Пауза між запитами
        self.logger.info(f"[PositionManager] Інтервал перевірки стану: {self.check_interval_seconds} сек.")
        self.logger.info(f"[PositionManager] Пауза між API запитами: {self.api_request_delay} сек.")

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
                        time.sleep(self.api_request_delay) # Використовуємо паузу з конфігу
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
            self.logger.debug(f"[_fetch_order_status] Пропущено запит для {symbol} - немає ID ордера.")
            return None # Немає ID - немає чого перевіряти
        try:
            self.logger.debug(f"[_fetch_order_status] Запит статусу ордера {order_id} для {symbol}...")
            # Використовуємо метод з bingx_client
            order_info = self.bingx_api.fetch_order(symbol, order_id)
            if order_info:
                 self.logger.debug(f"[_fetch_order_status] Отримано статус для {order_id}: {order_info.get('status')}")
            else:
                 # fetch_order повертає None при OrderNotFound або помилці API
                 self.logger.warning(f"[_fetch_order_status] Не вдалося отримати інформацію для ордера {order_id} ({symbol}). Метод fetch_order повернув None.")
            return order_info 
        except Exception as e:
            # Логуємо помилку, але не перериваємо цикл через один ордер
            self.logger.error(f"[PositionManager] Неочікувана помилка при _fetch_order_status ({order_id}, {symbol}): {e}", exc_info=False)
            return None # Повертаємо None у разі будь-якої помилки запиту

    def _check_and_update_position_status(self, position_data: Dict[str, Any]):
        """Перевіряє стан конкретної позиції та її ордерів на біржі."""
        position_id = position_data['id']
        symbol = position_data['symbol']
        # Перетворюємо значення з БД на bool
        is_breakeven = bool(position_data['is_breakeven'])
        # Переконуємось, що tp_order_ids - це список, навіть якщо в БД null/порожньо
        tp_order_ids = position_data.get('tp_order_ids') 
        if not isinstance(tp_order_ids, list):
             tp_order_ids = [] 
             
        sl_order_id = position_data['sl_order_id']
        entry_price = position_data['entry_price']
        source_channel_key = position_data['signal_channel_key']
        limit_order_id_c3 = position_data.get('related_limit_order_id') 
        current_amount = position_data['current_amount']
        position_side = position_data['position_side'] # Додано для логування
        
        self.logger.info(f"--- Перевірка позиції ID={position_id} ({symbol} {position_side}) ---")
        self.logger.debug(f"Дані з БД: Канал={source_channel_key}, ББ={is_breakeven}, SL_ID={sl_order_id}, TP_IDs={tp_order_ids}, LimitC3_ID={limit_order_id_c3}, Пот.Обсяг={current_amount}")

        try: # Обгортаємо основну логіку в try/except
            # --- Отримуємо статус ордерів --- 
            sl_order_info = self._fetch_order_status(symbol, sl_order_id)
            sl_status = sl_order_info.get('status') if sl_order_info else 'unknown'
            self.logger.debug(f"[PM ID={position_id}] SL статус: {sl_status}")

            tp_orders_info = {}
            if tp_order_ids:
                self.logger.debug(f"[PM ID={position_id}] Запит статусів для {len(tp_order_ids)} TP ордерів...")
                for tp_id in tp_order_ids:
                    if self.stop_event.is_set(): return 
                    time.sleep(self.api_request_delay / 2) # Ще менша пауза для TP
                    info = self._fetch_order_status(symbol, tp_id)
                    tp_orders_info[tp_id] = info
                    # Не логуємо тут статус TP детально, щоб не засмічувати логи
                self.logger.debug(f"[PM ID={position_id}] Статуси TP ордерів отримано.")
            
            # --- Перевірка спрацювання SL --- 
            if sl_status == 'closed':
                filled_amount = sl_order_info.get('filled') # Може бути None
                average_price = sl_order_info.get('average')
                # Перевіряємо, чи filled не None і більше 0
                if filled_amount is not None and filled_amount > 0: 
                    self.logger.info(f"[PositionManager] Позиція ID={position_id} ({symbol}) ЗАКРИТА по Stop Loss (ID: {sl_order_id}). Виконано: {filled_amount} @ {average_price}.")
                    self._handle_position_closed(position_id, 'stop_loss_hit', position_data, sl_order_info)
                    return 
                else:
                    # Якщо статус closed, але filled = 0 або None.
                    self.logger.warning(f"[PositionManager] SL ордер {sl_order_id} ({symbol}) має статус 'closed', але filled={filled_amount}. Перевірка активності позиції...")
                    position_still_open = self._check_if_position_open(symbol, position_side)
                    if not position_still_open:
                         self.logger.info(f"[PositionManager] Позиція {position_id} ({symbol}) не знайдена на біржі після SL closed/no fill. Вважаємо закритою.")
                         self._handle_position_closed(position_id, 'sl_closed_no_fill_pos_gone', position_data, sl_order_info)
                         return
                    else:
                         # SL закритий без виконання, але позиція є - дивна ситуація
                         self.logger.error(f"[PositionManager] КРИТИЧНО! SL {sl_order_id} ({symbol}) closed/no fill, але позиція {position_id} ще існує! Потрібне ручне втручання!")
                         # Не закриваємо позицію автоматично в цьому випадку
                         
            elif sl_status == 'canceled':
                self.logger.warning(f"[PositionManager] SL ордер {sl_order_id} для позиції {position_id} ({symbol}) має статус 'canceled'. Перевірка активності позиції...")
                position_still_open = self._check_if_position_open(symbol, position_side)
                if not position_still_open:
                    self.logger.warning(f"[PositionManager] Позиція {position_id} ({symbol}) не знайдена на біржі після скасування SL. Ймовірно, ліквідована або закрита іншим чином.")
                    self._handle_position_closed(position_id, 'position_not_found_after_sl_cancel', position_data, sl_order_info)
                    return
                else:
                    self.logger.error(f"[PositionManager] КРИТИЧНО! SL {sl_order_id} ({symbol}) скасовано, але позиція {position_id} все ще існує на біржі! Потрібне ручне втручання або логіка відновлення SL!")
            
            elif sl_status == 'unknown':
                 self.logger.warning(f"[PositionManager] Не вдалося отримати статус SL ордера {sl_order_id} для позиції {position_id}. Пропускаємо перевірку SL цього разу.")

            # --- Перевірка спрацювання TP та переведення в ББ --- 
            # Тільки якщо позиція ще не закрита по SL
            if sl_status not in ['closed', 'canceled']: # Або якщо SL unknown
                
                processed_tp_volume_this_cycle = 0 # Обсяг, оброблений в цій ітерації
                tp1_processed_now = False # Чи обробили ми TP1 саме зараз?

                if not tp_order_ids:
                    self.logger.debug(f"[PM ID={position_id}] Немає TP ордерів для перевірки.")
                else:
                    # Перевіряємо спочатку TP1 (якщо він є і позиція не в ББ)
                    tp1_id = tp_order_ids[0]
                    if not is_breakeven and tp1_id in tp_orders_info:
                        tp1_info = tp_orders_info[tp1_id]
                        tp1_status = tp1_info.get('status') if tp1_info else 'unknown'
                        tp1_filled = tp1_info.get('filled') if tp1_info else 0.0 # Важливо мати float
                        
                        # Перевіряємо, що статус closed і є виконаний обсяг
                        if tp1_status == 'closed' and tp1_filled is not None and tp1_filled > 0:
                            self.logger.info(f"[PositionManager] TP1 (ID: {tp1_id}) для позиції {position_id} ({symbol}) виконано! Обсяг: {tp1_filled}. Переводимо SL в ББ...")
                            processed_tp_volume_this_cycle += tp1_filled
                            tp1_processed_now = True
                            
                            # --- Спроба модифікації SL --- 
                            self.logger.info(f"[PositionManager] Спроба змінити SL {sl_order_id} на ціну ББ {entry_price}...")
                            edited_sl_order = self.bingx_api.edit_order(symbol=symbol, order_id=sl_order_id, new_price=entry_price)
                            
                            if edited_sl_order:
                                new_sl_id = edited_sl_order.get('id')
                                log_msg_sl = f"SL для позиції {position_id} успішно переведено в ББ."
                                if new_sl_id and new_sl_id != sl_order_id:
                                     log_msg_sl += f" Новий ID SL: {new_sl_id}"
                                     # Оновлюємо ID в БД ТІЛЬКИ якщо він змінився
                                     data_manager._update_position_field(self.db, position_id, 'sl_order_id', new_sl_id)
                                     sl_order_id = new_sl_id # Оновлюємо локальну змінну
                                self.logger.info(f"[PositionManager] {log_msg_sl}")
                                
                                # Оновлюємо статус ББ в БД
                                data_manager.update_position_breakeven(self.db, position_id, True)
                                is_breakeven = True # Оновлюємо локальний статус
                                 
                                # Звільняємо слот (якщо канал 1, 2 або 4)
                                if source_channel_key in ['channel_1', 'channel_2', 'channel_4']:
                                    self._trigger_slot_release(source_channel_key, position_id, True) # Сигналізуємо про звільнення
                                    
                                # --- Логіка для Каналу 3: Скасування лімітного ордера --- 
                                if source_channel_key == 'channel_3' and limit_order_id_c3:
                                    self._cancel_related_limit_order(symbol, limit_order_id_c3, position_id)
                                    limit_order_id_c3 = None # Оновлюємо локальну змінну
                                        
                            else:
                                self.logger.error(f"[PositionManager] Не вдалося перевести SL в ББ для позиції {position_id} (спроба редагування SL {sl_order_id} не вдалася). ПОТРІБНА АЛЬТЕРНАТИВНА ЛОГІКА (Cancel+Create?)!")
                                # TODO: Реалізувати Cancel+Create SL
                                # НЕ оновлюємо is_breakeven в БД, спробуємо наступного разу
                                
                        elif tp1_status not in ['open', 'unknown', 'new']: # Якщо TP1 вже не активний (closed без fill, canceled)
                             self.logger.warning(f"[PositionManager] TP1 ордер {tp1_id} для позиції {position_id} вже не активний (статус: {tp1_status}), але позиція ще не в ББ.")
                             # Чи потрібно закривати позицію в такому випадку? Поки ні.
                             
                    # Перевіряємо решту TP (або всі, якщо позиція вже була в ББ)
                    # ТУТ ПОТРІБНА КРАЩА ЛОГІКА, щоб не віднімати обсяг двічі!
                    # Ідея: Отримувати 'filled' з БД або зберігати стан виконаних TP
                    # --- СПРОЩЕННЯ v2: Перевіряємо ВСІ TP і сумуємо 'filled', порівнюємо з 'current_amount' ---
                    
                    total_filled_tp_volume = 0.0
                    all_tp_inactive = True # Флаг, чи всі TP ордери вже неактивні
                    
                    for tp_id in tp_order_ids:
                        tp_info = tp_orders_info.get(tp_id)
                        tp_status = tp_info.get('status') if tp_info else 'unknown'
                        tp_filled = tp_info.get('filled') if tp_info else 0.0
                        
                        if tp_filled is not None and tp_filled > 0:
                             total_filled_tp_volume += tp_filled
                             
                        if tp_status in ['open', 'new']:
                             all_tp_inactive = False # Є ще активний TP
                             
                    self.logger.debug(f"[PM ID={position_id}] Загальний виконаний обсяг TP (з API): {total_filled_tp_volume:.8f}. Поточний обсяг (з БД): {current_amount:.8f}")

                    # --- Оновлення поточного обсягу та перевірка закриття по TP ---
                    # Використовуємо точніше порівняння з current_amount
                    # Якщо загальний виконаний обсяг TP >= поточного обсягу в БД (з невеликим допуском)
                    tolerance = 1e-9 
                    if total_filled_tp_volume >= current_amount - tolerance:
                        # Позиція повністю закрита по TP
                        self.logger.info(f"[PositionManager] Позиція ID={position_id} ({symbol}) повністю закрита по Take Profit (Виконано TP: {total_filled_tp_volume:.8f} >= Поточний: {current_amount:.8f}).")
                        # Оновлюємо current_amount на 0 в БД
                        data_manager.update_position_amount(self.db, position_id, 0.0) 
                        # Збираємо інформацію про всі TP для логування
                        closed_tp_info_list = [tp_orders_info.get(tp_id) for tp_id in tp_order_ids if tp_orders_info.get(tp_id)]
                        self._handle_position_closed(position_id, 'all_tp_hit_volume_check', position_data, {"tp_orders": closed_tp_info_list})
                        return # Позиція закрита, виходимо
                    else:
                        # Позиція закрита частково по TP
                        # Оновлюємо current_amount в БД, якщо total_filled_tp_volume > 0 і відрізняється від того, що було
                        # TODO: Потрібно зберігати в БД загальний виконаний обсяг, щоб уникнути повторного оновлення!
                        # Поки що не оновлюємо current_amount тут, щоб уникнути проблем. 
                        # Оновлення суми краще робити при отриманні сигналу про виконання ордеру (Webhook/WSS).
                        self.logger.debug(f"[PM ID={position_id}] Позиція частково закрита по TP (Виконано: {total_filled_tp_volume:.8f} < Поточний: {current_amount:.8f}).")
                        
                    # Перевірка, чи всі TP неактивні, але позиція ще не закрита
                    if all_tp_inactive and current_amount > tolerance:
                        self.logger.warning(f"[PM ID={position_id}] Всі TP ордери неактивні, але розрахунковий обсяг > 0 ({current_amount:.8f}). Позиція залишається активною (моніторимо SL).")

            # Якщо ми дійшли сюди, позиція, ймовірно, все ще активна
            self.logger.debug(f"[PM ID={position_id}] Перевірку завершено, позиція активна.")

        except Exception as check_err:
            # Логуємо помилку перевірки конкретної позиції, але не зупиняємо весь менеджер
            self.logger.error(f"[PositionManager] Помилка під час перевірки позиції ID={position_id} ({symbol}): {check_err}", exc_info=True)


    def _handle_position_closed(self, position_id: int, reason: str, position_data: Dict[str, Any], closing_info: Optional[Dict[str, Any]] = None):
        """Обробляє закриття позиції (SL або всі TP). Оновлює БД.
           НЕ звільняє слот напряму, а викликає _trigger_slot_release.
        """
        symbol = position_data['symbol']
        self.logger.info(f"--- Обробка закриття позиції ID={position_id} ({symbol}), Причина: {reason} ---")
        
        # 1. Оновити статус позиції в БД (позначити як неактивну)
        status_info_text = f"{reason} at {datetime.datetime.now().isoformat(timespec='seconds')}" 
        if closing_info:
            # Намагаємось зробити інформацію читабельною
            try: 
                # Використовуємо str() для безпечного перетворення, обмежуємо довжину
                closing_info_str = str(closing_info)
                if len(closing_info_str) > 500: 
                     closing_info_str = closing_info_str[:500] + "..."
            except Exception:
                closing_info_str = "Error serializing closing info"
            status_info_text += f" | Info: {closing_info_str}"
            
        update_success = data_manager.update_position_status(self.db, position_id, False, status_info_text)
        if not update_success:
             self.logger.critical(f"[PositionManager] НЕ ВДАЛОСЯ оновити статус is_active=False для позиції ID={position_id}! Дані можуть бути некоректні.")
             # TODO: Потрібна система сповіщень про такі помилки
             
        # 2. Перевірити, чи потрібно сигналізувати про звільнення слоту
        # Звільняємо, якщо позиція закрита НЕ через переведення в ББ (бо тоді слот звільнився раніше)
        is_breakeven_before_close = bool(position_data.get('is_breakeven', False)) # Статус ББ на момент початку перевірки
        source_channel_key = position_data['signal_channel_key']
        
        if not is_breakeven_before_close and source_channel_key in ['channel_1', 'channel_2', 'channel_4']:
             self.logger.info(f"[PositionManager] Позиція {position_id} закрита (причина: {reason}) до переведення в ББ. Сигналізуємо про звільнення слоту.")
             self._trigger_slot_release(source_channel_key, position_id, False)
        else:
             self.logger.info(f"[PositionManager] Слот для позиції {position_id} або вже був звільнений (ББ={is_breakeven_before_close}), або це канал {source_channel_key}. Звільнення зараз не потрібне.")
             
        # 3. TODO: Додати опціональну логіку скасування залишкових ордерів (SL/TP) після закриття.
        
        self.logger.info(f"--- Завершено обробку закриття позиції ID={position_id} ({symbol}) ---")

    def _trigger_slot_release(self, source_channel_key: str, position_id: int, became_breakeven: bool):
        """Сигналізує основному потоку (або Slot Manager) про необхідність звільнити слот."""
        # Реальна логіка звільнення слоту має бути поза цим класом.
        status_note = "(стала ББ)" if became_breakeven else "(закрита до ББ)"
        self.logger.info(f"[PositionManager] >>> СИГНАЛ: Звільнити слот для каналу {source_channel_key} (Позиція ID: {position_id}) {status_note}")
        # TODO: Реалізувати механізм передачі сигналу (Callback, Queue, etc.)
        pass
        
    def _cancel_related_limit_order(self, symbol: str, limit_order_id: str, position_id: int):
        """Скасовує лімітний ордер, пов'язаний з позицією каналу 3."""
        if not limit_order_id: return # Немає чого скасовувати
        
        self.logger.info(f"[PositionManager C3] Скасування пов'язаного лімітного ордера ID: {limit_order_id} для позиції {position_id} ({symbol})...")
        cancel_success = self.bingx_api.cancel_order(symbol, limit_order_id)
        if cancel_success:
            self.logger.info(f"[PositionManager C3] Лімітний ордер {limit_order_id} успішно скасовано (або вже був неактивний). Оновлення БД...")
            # Оновлюємо дані позиції в БД
            db_update_ok = data_manager.update_position_limit_order(self.db, position_id, None)
            if not db_update_ok:
                 self.logger.error(f"[PositionManager C3] Не вдалося оновити related_limit_order_id в БД для позиції {position_id} після скасування ордера {limit_order_id}.")
        else:
            self.logger.warning(f"[PositionManager C3] Не вдалося скасувати лімітний ордер {limit_order_id}. Можливо, він вже виконався або сталася помилка API.")

    def _check_if_position_open(self, symbol: str, position_side: str) -> bool:
        """Перевіряє, чи існує відкрита позиція для символу та сторони на біржі."""
        try:
            current_positions = self.bingx_api.fetch_positions(symbol=symbol)
            if current_positions is None: # Помилка API
                 self.logger.warning(f"[_check_if_position_open] Не вдалося отримати позиції для {symbol} (API повернув None).")
                 return False # Не можемо перевірити
                 
            # Перевірка за символом і стороною (long/short)
            position_exists = any(
                p and p['symbol'] == symbol and p.get('side') == position_side.lower() 
                for p in current_positions
            )
            self.logger.debug(f"[_check_if_position_open] Перевірка існування позиції {symbol} {position_side}: {position_exists}. Дані API: {current_positions}")
            return position_exists
        except Exception as e:
            self.logger.error(f"[_check_if_position_open] Помилка при перевірці позиції {symbol} {position_side} на біржі: {e}", exc_info=False)
            return False # Не можемо перевірити


# --- Приклад використання --- 
# Залишаємо блок if __name__ == '__main__' без змін для можливості тестування модуля окремо
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
                conf = json.load(f)
                # Додамо дефолтну секцію для position_manager, якщо її немає
                if 'position_manager' not in conf:
                    conf['position_manager'] = {
                        'check_interval_seconds': 60,
                        'api_request_delay': 0.2
                    }
                return conf
        except Exception as e:
            main_logger.error(f"Не вдалося завантажити конфіг {config_path}: {e}")
            # Повертаємо дефолтний конфіг у разі помилки
            return {'position_manager': {'check_interval_seconds': 60, 'api_request_delay': 0.2}}

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
        # Передаємо реальне з'єднання db_conn та конфіг
        position_manager = PositionManager(bingx_client_instance, config, db_conn)
        
        # --- Додамо тестову позицію в БД, щоб було що моніторити --- 
        # УВАГА: Замініть ID ордерів на РЕАЛЬНІ існуючі ордери на вашому ТЕСТОВОМУ акаунті BingX!
        # Інакше тести моніторингу не спрацюють коректно.
        main_logger.info("Додавання/перевірка тестової позиції в БД...")
        test_symbol = 'LTC/USDT:USDT' # Або інша пара, для якої є тестові ордери
        test_sl_id = 'YOUR_REAL_SL_ORDER_ID' # <--- ЗАМІНІТЬ
        test_tp_ids = ['YOUR_REAL_TP1_ORDER_ID', 'YOUR_REAL_TP2_ORDER_ID'] # <--- ЗАМІНІТЬ
        test_entry_price = 70.0 # Приблизна ціна входу
        
        test_pos_data = {
             'signal_channel_key': 'channel_1',
             'symbol': test_symbol, 
             'position_side': 'LONG',
             'entry_price': test_entry_price, 
             'initial_amount': 0.1, 
             'leverage': 10,
             'sl_order_id': test_sl_id, 
             'tp_order_ids': test_tp_ids,
        }
        
        # Перевіримо, чи є вже АКТИВНА позиція для цього символу та сторони
        existing_active = [
            p for p in data_manager.get_active_positions(db_conn) 
            if p['symbol'] == test_symbol and p['position_side'] == test_pos_data['position_side']
        ]
        
        if not existing_active:
             main_logger.info(f"Активної позиції для {test_symbol} LONG не знайдено. Додаємо нову тестову...")
             # Встановлюємо поточний обсяг при додаванні
             test_pos_data['current_amount'] = test_pos_data['initial_amount'] 
             new_id = data_manager.add_new_position(db_conn, test_pos_data)
             if new_id:
                  main_logger.info(f"Додано тестову позицію з ID {new_id}. Переконайтесь, що ордери SL={test_sl_id}, TP={test_tp_ids} існують на BingX!")
             else:
                  main_logger.error("Не вдалося додати тестову позицію")
        else:
             pos_id = existing_active[0]['id']
             main_logger.info(f"Знайдено активну позицію для {test_symbol} LONG (ID: {pos_id}). Тестова позиція не додається.")
             # Оновимо ID ордерів у тестових даних на випадок, якщо вони змінилися
             test_sl_id = existing_active[0].get('sl_order_id', test_sl_id)
             test_tp_ids = existing_active[0].get('tp_order_ids', test_tp_ids)
             main_logger.info(f"Будуть використовуватися ордери з БД: SL={test_sl_id}, TP={test_tp_ids}")
        # ----------------------------------------------------------------

        position_manager.start_monitoring()
        main_logger.info("Менеджер позицій запущено. Натисніть Ctrl+C для зупинки.")
        
        # Тримаємо основний потік живим
        while True:
            if not position_manager.thread.is_alive():
                 main_logger.warning("Потік моніторингу PositionManager несподівано завершився!")
                 break
            time.sleep(5) 
            
    except KeyboardInterrupt:
        main_logger.info("Отримано сигнал зупинки (Ctrl+C).")
        if 'position_manager' in locals() and position_manager:
             position_manager.stop_monitoring()
    except Exception as main_err:
        main_logger.critical(f"Критична помилка в основному блоці тестування: {main_err}", exc_info=True)
        if 'position_manager' in locals() and position_manager:
             position_manager.stop_monitoring()
    finally:
         # Переконуємось, що потік зупинено перед закриттям БД
         if 'position_manager' in locals() and position_manager and position_manager.thread and position_manager.thread.is_alive():
             main_logger.info("Очікування остаточної зупинки PositionManager перед закриттям БД...")
             position_manager.stop_monitoring()

         if db_conn:
             db_conn.close()
             main_logger.info("З'єднання з БД закрито.")
