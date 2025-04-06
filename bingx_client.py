import ccxt
import os
import sys
import logging
from dotenv import load_dotenv
import math
import decimal
import re
import time

class BingXClient:
    def __init__(self, api_key: str, api_secret: str, logger: logging.Logger):
        """Ініціалізація клієнта BingX."""
        self.api_key = api_key
        self.api_secret = api_secret
        self.logger = logger
        self.exchange = None
        
        if not self.api_key or not self.api_secret:
            self.logger.critical("[BingXClient] API ключ або секрет не надано при ініціалізації.")
            raise ValueError("API ключ та секрет є обов'язковими.")

        self.logger.info("[BingXClient] Ініціалізація ccxt.bingx...")
        try:
            self.exchange = ccxt.bingx({
                'apiKey': self.api_key,
                'secret': self.api_secret,
                'enableRateLimit': True,
            })
            self.logger.info("[BingXClient] Встановлення defaultType='swap'...")
            self.exchange.options['defaultType'] = 'swap'
            self.logger.info("[BingXClient] Завантаження ринків...")
            self.exchange.load_markets()
            self.logger.info("[BingXClient] Клієнт ccxt для BingX (swap) успішно ініціалізовано та ринки завантажено.")
        except ccxt.AuthenticationError as e:
            self.logger.critical(f"[BingXClient] Помилка автентифікації ccxt: {e}")
            raise
        except ccxt.NetworkError as e:
            self.logger.error(f"[BingXClient] Мережева помилка ccxt під час ініціалізації: {e}")
            raise
        except ccxt.ExchangeError as e:
            self.logger.critical(f"[BingXClient] Помилка біржі ccxt під час ініціалізації: {e}")
            raise
        except Exception as e:
            self.logger.critical(f"[BingXClient] Невідома помилка під час ініціалізації: {e}", exc_info=True)
            raise

    def _format_symbol_for_swap(self, symbol: str) -> str:
        """Конвертує символ типу 'BTCUSDT' у формат 'BTC/USDT:USDT' для ccxt swap."""
        quote_currencies_pattern = '(?:USDT|BUSD|USDC|BTC|ETH)'
        if re.match(f"^[A-Z0-9]+/{quote_currencies_pattern}:{quote_currencies_pattern}$", symbol):
            self.logger.debug(f"[_format_symbol_for_swap] Символ '{symbol}' вже у правильному форматі swap. Без змін.")
            return symbol

        symbol_cleaned = re.sub(r'^\d+', '', symbol)
        if symbol_cleaned != symbol:
            self.logger.debug(f"[_format_symbol_for_swap] Видалено префікс з '{symbol}' -> '{symbol_cleaned}'")
            symbol = symbol_cleaned

        quote_currencies = ['USDT', 'BUSD', 'USDC', 'BTC', 'ETH']
        for quote in quote_currencies:
            if symbol.endswith(quote):
                base = symbol[:-len(quote)]
                if base:
                    formatted_symbol = f"{base}/{quote}:{quote}"
                    self.logger.debug(f"[_format_symbol_for_swap] Символ '{symbol}' конвертовано в '{formatted_symbol}'")
                    return formatted_symbol
                else:
                    break

        self.logger.warning(f"[_format_symbol_for_swap] Не вдалося автоматично форматувати символ '{symbol}'. Використовується як є.")
        return symbol

    def _get_precision_digits(self, precision_value):
        """Допоміжна функція для отримання кількості знаків після коми з значення точності ccxt."""
        if precision_value is None:
            return None
        try:
            precision_decimal = decimal.Decimal(str(precision_value))
            if precision_decimal <= 0:
                return None
            num_decimal_places = int(-precision_decimal.log10())
            return max(0, num_decimal_places)
        except (ValueError, TypeError, decimal.InvalidOperation) as e:
            self.logger.warning(f"Не вдалося обчислити десяткові знаки з точності {precision_value}: {e}")
            return None

    def _round_amount(self, amount, symbol):
        """Округлює кількість до точності, визначеної для ринку."""
        try:
            market = self.exchange.market(symbol)
            precision_value = market.get('precision', {}).get('amount')
            num_decimal_places = self._get_precision_digits(precision_value)

            if num_decimal_places is None:
                self.logger.warning(f"[_round_amount] Не знайдено точність для {symbol}. Округлення не виконується.")
                return amount

            rounded_amount = round(amount, num_decimal_places)
            self.logger.debug(f"[_round_amount] {amount} округлено до {num_decimal_places} знаків: {rounded_amount} для {symbol}")
            return rounded_amount
        except Exception as e:
            self.logger.error(f"[_round_amount] Помилка округлення для {symbol}: {e}", exc_info=True)
            return amount

    def place_market_order_basic(self, symbol: str, side: str, position_side: str, margin_usdt: float, leverage: int):
        """Розміщує ринковий ордер, розраховуючи обсяг від маржі та плеча."""
        if not self.exchange:
            self.logger.error("[BingXClient] Спроба викликати метод на неініціалізованому клієнті.")
            return None

        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        market_data = self.exchange.market(ccxt_market_symbol)
        self.logger.info(f"-- Початок розміщення ринкового ордера ({ccxt_market_symbol}) --")
        self.logger.info(f"Параметри: side={side}, posSide={position_side}, МАРЖА={margin_usdt} USDT, плече={leverage}x")

        self.logger.info(f"Встановлення плеча {leverage}x для {ccxt_market_symbol} ({position_side})...")
        try:
            self.exchange.set_leverage(leverage, ccxt_market_symbol, params={'side': position_side})
            self.logger.info(f"Плече встановлено.")
        except Exception as e:
            self.logger.warning(f"Помилка при встановленні плеча: {e}. Продовжуємо...", exc_info=True)

        self.logger.info(f"Отримання поточної ціни для {ccxt_market_symbol}...")
        try:
            ticker = self.exchange.fetch_ticker(ccxt_market_symbol)
            current_price = ticker.get('last')
            if not current_price:
                self.logger.error(f"Не вдалося отримати ціну 'last' з ticker для {ccxt_market_symbol}. Ticker: {ticker}")
                return None
            self.logger.info(f"Поточна ціна: {current_price}")
        except Exception as e:
            self.logger.error(f"Помилка при отриманні ціни: {e}", exc_info=True)
            return None

        self.logger.info(f"Розрахунок та округлення кількості для маржі {margin_usdt} USDT...")
        try:
            min_amount = market_data.get('limits', {}).get('amount', {}).get('min')
            min_cost = market_data.get('limits', {}).get('cost', {}).get('min')
            base_currency = market_data.get('base', ccxt_market_symbol.split('/')[0])

            position_size_usdt = margin_usdt * leverage
            self.logger.debug(f"Розрахований розмір позиції: {position_size_usdt:.2f} USDT")
            amount_unrounded = position_size_usdt / current_price
            self.logger.debug(f"Розрахована кількість (до перевірки/округлення): {amount_unrounded} {base_currency}")

            if min_cost is not None and position_size_usdt < min_cost:
                self.logger.error(f"Розрахований розмір позиції {position_size_usdt:.2f} USDT менший за min_cost {min_cost} USDT.")
                return None

            if min_amount is not None and amount_unrounded < min_amount:
                self.logger.warning(f"Розрахована к-сть {amount_unrounded} менша за min_amount {min_amount}. Спробуємо використати min_amount.")
                amount_unrounded = min_amount
                position_size_usdt = amount_unrounded * current_price
                margin_usdt_adjusted = position_size_usdt / leverage
                self.logger.info(f"Розмір позиції скориговано до ~{position_size_usdt:.2f} USDT (для min_amount), маржа ~{margin_usdt_adjusted:.2f} USDT")
                if min_cost is not None and position_size_usdt < min_cost:
                    self.logger.error(f"Навіть min_amount ({amount_unrounded}) має вартість позиції ~{position_size_usdt:.2f} USDT, що менше за min_cost {min_cost}.")
                    return None
            else:
                margin_usdt_adjusted = margin_usdt

            amount_rounded = self._round_amount(amount_unrounded, ccxt_market_symbol)
            if amount_rounded is None or amount_rounded <= 0:
                self.logger.error(f"Не вдалося округлити кількість або результат нульовий/від'ємний: {amount_rounded}")
                return None

            if min_amount is not None and amount_rounded < min_amount:
                self.logger.error(f"Округлена кількість {amount_rounded} все ще менша за min_amount {min_amount}. Ордер неможливий.")
                return None

            final_amount = amount_rounded
            final_position_size_usdt = final_amount * current_price

        except Exception as e:
            self.logger.error(f"Помилка під час розрахунку/округлення кількості: {e}", exc_info=True)
            return None

        self.logger.info(f"Спроба створити ринковий ордер: {side.upper()} {final_amount} {base_currency} ({ccxt_market_symbol})...")
        try:
            order_params = {
                'leverage': leverage,
                'positionSide': position_side.upper()
            }
            if side.lower() == 'buy':
                order = self.exchange.create_market_buy_order(ccxt_market_symbol, final_amount, params=order_params)
            elif side.lower() == 'sell':
                order = self.exchange.create_market_sell_order(ccxt_market_symbol, final_amount, params=order_params)
            else:
                self.logger.error(f"Неправильний параметр 'side': {side}.")
                return None

            self.logger.info(f"[УСПІХ] Ордер успішно створено! (К-сть: {final_amount} {base_currency}, Розмір позиції ~{final_position_size_usdt:.2f} USDT, Маржа ~{margin_usdt_adjusted:.2f} USDT)")
            self.logger.debug(f"Деталі ордеру: {order}")
            return order

        except ccxt.InsufficientFunds as e:
            self.logger.error(f"Недостатньо коштів для ордера {ccxt_market_symbol}: {e}", exc_info=True)
            return None
        except ccxt.InvalidOrder as e:
            self.logger.error(f"Неприпустимий ордер для {ccxt_market_symbol}: {e}", exc_info=True)
            return None
        except ccxt.ExchangeError as e:
            self.logger.error(f"Помилка біржі при створенні ордера для {ccxt_market_symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            self.logger.error(f"Невідома помилка при створенні ордера для {ccxt_market_symbol}: {e}", exc_info=True)
            return None

    def place_limit_order(self, symbol: str, direction: str, amount: float, limit_price: float, leverage: int = None):
        """Розміщує лімітний ордер (BUY або SELL) за вказаною ціною."""
        self.logger.info(f"Спроба розмістити LIMIT ордер: {direction} {amount} {symbol} @ {limit_price} (плече: {leverage or 'default'})")

        formatted_symbol = self._format_symbol_for_swap(symbol)
        if not formatted_symbol:
            self.logger.error(f"[Limit Order] Не вдалося відформатувати символ {symbol}. Ордер не розміщено.")
            return None

        try:
            market_data = self.exchange.market(formatted_symbol)
        except Exception as e:
            self.logger.error(f"[Limit Order] Помилка отримання ринкових даних для {formatted_symbol}: {e}")
            return None

        side = 'buy' if direction.upper() == 'LONG' else 'sell'
        position_side_param = direction.upper()

        try:
            if leverage is not None:
                try:
                    leverage_params = {'side': position_side_param}
                    self.logger.info(f"[Limit Order] Встановлення плеча {leverage}x для {formatted_symbol} ({position_side_param})...")
                    self.exchange.set_leverage(leverage, formatted_symbol, params=leverage_params)
                    self.logger.info(f"[Limit Order] Плече встановлено.")
                except Exception as lev_err:
                    self.logger.warning(f"[Limit Order] Помилка при встановленні плеча {leverage}x для {formatted_symbol}: {lev_err}. Продовжуємо без зміни плеча.")

            rounded_amount = self._round_amount(amount, formatted_symbol)
            rounded_price_str = self.exchange.price_to_precision(formatted_symbol, limit_price)
            rounded_price = float(rounded_price_str)

            if rounded_amount is None or rounded_amount <= 0:
                self.logger.error(f"[Limit Order] Розрахований обсяг {rounded_amount} (з {amount}) занадто малий або нульовий для {formatted_symbol}. Ордер не розміщено.")
                return None

            if rounded_price is None or rounded_price <= 0:
                self.logger.error(f"[Limit Order] Ціна ліміту {rounded_price} (з {limit_price}) некоректна для {formatted_symbol}. Ордер не розміщено.")
                return None

            self.logger.debug(f"[Limit Order] Округлені значення: Amount={rounded_amount}, Price={rounded_price}")

            params = {'positionSide': position_side_param}
            self.logger.info(f"Розміщую {side.upper()} LIMIT ордер: {rounded_amount} {formatted_symbol} за ціною {rounded_price_str}")
            if side == 'buy':
                order = self.exchange.create_limit_buy_order(formatted_symbol, rounded_amount, rounded_price_str, params=params)
            else:
                order = self.exchange.create_limit_sell_order(formatted_symbol, rounded_amount, rounded_price_str, params=params)

            self.logger.info(f"LIMIT ордер успішно розміщено: ID {order.get('id')}, Symbol: {order.get('symbol')}, Side: {order.get('side')}, Amount: {order.get('amount')}, Price: {order.get('price')}, Status: {order.get('status')}")
            return order

        except ccxt.InsufficientFunds as e:
            self.logger.error(f"[Limit Order] Недостатньо коштів для розміщення {side} ордера {amount} {formatted_symbol} @ {limit_price}: {e}")
        except ccxt.ExchangeError as e:
            self.logger.error(f"[Limit Order] Помилка біржі під час розміщення {side} ордера для {formatted_symbol}: {e}")
        except ccxt.NetworkError as e:
            self.logger.error(f"[Limit Order] Мережева помилка під час розміщення {side} ордера для {formatted_symbol}: {e}")
        except Exception as e:
            self.logger.error(f"[Limit Order] Неочікувана помилка під час розміщення {side} ордера для {formatted_symbol}: {e}", exc_info=True)
        return None

    def set_stop_loss(self, symbol: str, position_side: str, initial_amount: float, stop_loss_price: float):
        """Встановлює ордер Stop Loss для існуючої позиції."""
        if not self.exchange:
            self.logger.error("[SL] Спроба викликати метод на неініціалізованому клієнті.")
            return None

        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        sl_side = 'sell' if position_side.upper() == 'LONG' else 'buy'
        self.logger.info(f"-- Встановлення Stop Loss для {ccxt_market_symbol} --")
        order_type = 'STOP_MARKET'
        self.logger.info(f"Параметри: Side={sl_side}, Amount={initial_amount}, SL Price={stop_loss_price}, Type={order_type}")

        try:
            amount_to_set = self._round_amount(initial_amount, ccxt_market_symbol)
            if amount_to_set is None or amount_to_set <= 0:
                self.logger.error(f"[SL] Не вдалося округлити обсяг {initial_amount} або результат нульовий/від'ємний: {amount_to_set}")
                return None

            self.logger.info(f"[SL] Округлений обсяг для встановлення: {amount_to_set}")
            params = {
                'stopPrice': stop_loss_price,
                'positionSide': position_side.upper(),
                'workingType': 'MARK_PRICE'
                # Видалено 'reduceOnly': True
            }
            self.logger.debug(f"Параметри для create_order (SL): symbol={ccxt_market_symbol}, type={order_type}, side={sl_side}, amount={amount_to_set}, params={params}")

            sl_order = self.exchange.create_order(
                symbol=ccxt_market_symbol,
                type=order_type,
                side=sl_side,
                amount=amount_to_set,
                price=None,
                params=params
            )

            self.logger.info(f"[УСПІХ] Ордер Stop Loss успішно створено!")
            self.logger.debug(f"Деталі SL ордеру: {sl_order}")
            return sl_order

        except ccxt.InvalidOrder as e:
            self.logger.error(f"[SL] Неприпустимий ордер SL для {ccxt_market_symbol}: {e}", exc_info=True)
            return None
        except ccxt.ExchangeError as e:
            self.logger.error(f"[SL] Помилка біржі при створенні SL ордера для {ccxt_market_symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            self.logger.error(f"[SL] Невідома помилка при створенні SL ордера для {ccxt_market_symbol}: {e}", exc_info=True)
            return None

    def set_take_profits(self, symbol: str, position_side: str, initial_amount: float, 
                         take_profit_prices: list[float], tp_distribution: list[int]):
        """Встановлює ордери Take Profit для часткового закриття позиції."""
        if not self.exchange:
            self.logger.error("[TP] Спроба викликати метод на неініціалізованому клієнті.")
            return []

        if len(take_profit_prices) != len(tp_distribution):
            self.logger.error(f"[TP] Кількість цін TP ({len(take_profit_prices)}) не співпадає з кількістю розподілів ({len(tp_distribution)}).")
            return []

        if sum(tp_distribution) > 100:
            self.logger.warning(f"[TP] Сума розподілу ({sum(tp_distribution)}%) більша за 100%. Можливе некоректне закриття.")

        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        tp_side = 'sell' if position_side.upper() == 'LONG' else 'buy'
        created_tp_orders = []
        remaining_amount = initial_amount
        self.logger.info(f"-- Встановлення Take Profit ордерів для {ccxt_market_symbol}. Початковий обсяг: {initial_amount} --")

        try:
            market_data = self.exchange.market(ccxt_market_symbol)
            base_currency = market_data.get('base', ccxt_market_symbol.split('/')[0])
            min_amount = market_data.get('limits', {}).get('amount', {}).get('min')
            self.logger.debug(f"[TP] Дані для валідації: min_amount={min_amount}")
        except Exception as e:
            self.logger.error(f"[TP] Не вдалося отримати дані ринку для валідації TP обсягів: {e}", exc_info=True)
            return []

        for i, tp_price in enumerate(take_profit_prices):
            if remaining_amount <= 0:
                self.logger.info(f"[TP {i+1}] Залишок позиції нульовий. Подальше встановлення TP неможливе.")
                break

            percentage = tp_distribution[i]
            if percentage <= 0:
                self.logger.info(f"[TP {i+1}] Пропуск рівня TP з розподілом {percentage}%.")
                continue

            target_partial_amount = initial_amount * (percentage / 100.0)
            self.logger.info(f"[TP {i+1}] Ціна={tp_price}, Відсоток={percentage}%, Цільовий обсяг={target_partial_amount:.8f} {base_currency}")

            amount_to_place_rounded = self._round_amount(target_partial_amount, ccxt_market_symbol)
            if amount_to_place_rounded is None or amount_to_place_rounded <= 0:
                self.logger.error(f"[TP {i+1}] Не вдалося округлити цільовий обсяг або результат нульовий/від'ємний: {amount_to_place_rounded}")
                continue

            remaining_amount_rounded = self._round_amount(remaining_amount, ccxt_market_symbol)
            if remaining_amount_rounded is None:
                self.logger.error(f"[TP {i+1}] Не вдалося округлити залишок {remaining_amount}. Пропуск TP.")
                continue

            if amount_to_place_rounded > remaining_amount_rounded:
                self.logger.warning(f"[TP {i+1}] Округлений цільовий обсяг ({amount_to_place_rounded}) більший за округлений залишок ({remaining_amount_rounded}). Використовуємо залишок.")
                amount_to_place_final = remaining_amount_rounded
            else:
                amount_to_place_final = amount_to_place_rounded

            if amount_to_place_final <= 0:
                self.logger.warning(f"[TP {i+1}] Пропускаємо TP, оскільки розрахований обсяг ({amount_to_place_final}) нульовий або від'ємний (залишок: {remaining_amount}).")
                continue

            if min_amount is not None and amount_to_place_final < min_amount:
                self.logger.error(f"[TP {i+1}] Фінальний обсяг {amount_to_place_final} менший за min_amount {min_amount}. Неможливо встановити цей TP.")
                continue

            order_type = 'TAKE_PROFIT_MARKET'
            self.logger.info(f"[TP {i+1}] Спроба створити ордер: {tp_side.upper()} {amount_to_place_final} {base_currency} @ TP={tp_price}...")
            try:
                params = {
                    'stopPrice': tp_price,
                    'positionSide': position_side.upper(),
                    'workingType': 'MARK_PRICE',
                    # 'reduceOnly': True 
                }
                self.logger.debug(f"Параметри для create_order (TP {i+1}): symbol={ccxt_market_symbol}, type={order_type}, side={tp_side}, amount={amount_to_place_final}, params={params}")

                tp_order = self.exchange.create_order(
                    symbol=ccxt_market_symbol,
                    type=order_type,
                    side=tp_side,
                    amount=amount_to_place_final,
                    price=None,
                    params=params
                )

                if tp_order:
                    self.logger.info(f"[УСПІХ] Ордер TP {i+1} успішно створено! Обсяг: {amount_to_place_final}")
                    created_tp_orders.append(tp_order)
                    remaining_amount -= amount_to_place_final
                else:
                    self.logger.error(f"[TP {i+1}] Ордер TP для {ccxt_market_symbol} не було створено (повернуто None).")
            
            except ccxt.InsufficientFunds as e:
                self.logger.error(f"[TP {i+1}] Недостатньо коштів для TP ордера {ccxt_market_symbol}: {e}")
            except ccxt.ExchangeError as e:
                self.logger.error(f"[TP {i+1}] Помилка біржі при створенні TP ордера {ccxt_market_symbol}: {e}")
            except Exception as e:
                self.logger.error(f"[TP {i+1}] Неочікувана помилка при створенні TP ордера {ccxt_market_symbol}: {e}", exc_info=True)

        self.logger.info(f"-- Завершено встановлення TP ордерів. Створено: {len(created_tp_orders)}. Фінальний залишок: {remaining_amount:.8f} --")
        return created_tp_orders

    def cancel_open_orders(self, symbol: str, order_ids: list):
        """Скасовує відкриті ордери за їхніми ID."""
        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        for order_id in order_ids:
            try:
                self.exchange.cancel_order(order_id, ccxt_market_symbol)
                self.logger.info(f"Ордер {order_id} успішно скасовано.")
            except Exception as e:
                self.logger.error(f"Помилка при скасуванні ордера {order_id}: {e}", exc_info=True)

    def fetch_order(self, symbol: str, order_id: str):
        """Отримує інформацію про конкретний ордер за ID."""
        if not self.exchange:
            self.logger.error("[BingXClient] Спроба викликати fetch_order на неініціалізованому клієнті.")
            return None
            
        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        self.logger.info(f"[BingXClient] Запит даних ордера ID: {order_id} для {ccxt_market_symbol}...")
        
        try:
            order_info = self.exchange.fetch_order(order_id, ccxt_market_symbol)
            self.logger.info(f"[BingXClient] Дані для ордера {order_id} успішно отримано.")
            self.logger.debug(f"Деталі ордера {order_id}: {order_info}")
            return order_info
        except ccxt.OrderNotFound as e:
            self.logger.warning(f"[BingXClient] Ордер ID {order_id} для {ccxt_market_symbol} не знайдено: {e}")
            return None # Повертаємо None, якщо ордер не знайдено
        except ccxt.ExchangeError as e:
            self.logger.error(f"[BingXClient] Помилка біржі при запиті ордера {order_id} для {ccxt_market_symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            self.logger.error(f"[BingXClient] Невідома помилка при запиті ордера {order_id} для {ccxt_market_symbol}: {e}", exc_info=True)
            return None
            
    def edit_order(self, symbol: str, order_id: str, new_price: float, new_amount: float = None):
        """Спроба модифікувати існуючий ордер (наприклад, ціну SL).
        УВАГА: Підтримка та параметри залежать від біржі та ccxt. 
               Може знадобитися інший підхід (cancel + create)."""
        if not self.exchange:
            self.logger.error("[BingXClient] Спроба викликати edit_order на неініціалізованому клієнті.")
            return None

        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        self.logger.info(f"[BingXClient] Спроба змінити ордер ID: {order_id} ({ccxt_market_symbol}). Нова ціна: {new_price}, Нова кількість: {new_amount}")

        try:
            # Спочатку отримуємо інформацію про ордер, щоб знати його тип, сторону і т.д.
            existing_order = self.fetch_order(ccxt_market_symbol, order_id)
            if not existing_order:
                self.logger.error(f"[BingXClient] Не вдалося отримати дані для ордера {order_id} перед редагуванням.")
                return None
                
            # --- Логіка визначення параметрів для edit_order ---
            # Це дуже залежить від ccxt і біржі. Нам потрібно передати *всі* необхідні параметри, 
            # навіть якщо ми міняємо лише ціну. 
            # Особливо важливо для SL/TP, де може знадобитись `stopPrice`.
            
            order_type = existing_order.get('type')
            order_side = existing_order.get('side')
            original_amount = existing_order.get('amount')
            
            # Якщо нова кількість не вказана, використовуємо оригінальну
            amount_to_use = new_amount if new_amount is not None else original_amount
            if amount_to_use is None:
                 self.logger.error(f"[BingXClient] Не вдалося визначити кількість для редагування ордера {order_id}.")
                 return None

            # Округлення нової кількості (якщо вона змінилась)
            if new_amount is not None:
                 amount_to_use = self._round_amount(amount_to_use, ccxt_market_symbol)
                 if amount_to_use is None or amount_to_use <= 0:
                      self.logger.error(f"[BingXClient] Некоректна нова кількість {new_amount} для ордера {order_id}.")
                      return None

            # Параметри, які, ймовірно, потрібні для edit_order (можуть відрізнятись!)
            params = {}
            if order_type in ['STOP_MARKET', 'TAKE_PROFIT_MARKET']:
                # Для SL/TP головне - змінити stopPrice
                 params['stopPrice'] = new_price 
                 # Можливо, BingX вимагає й інші параметри тут, напр. positionSide
                 if 'info' in existing_order and 'positionSide' in existing_order['info']:
                      params['positionSide'] = existing_order['info']['positionSide']
                 else: 
                      # Спробуємо вгадати positionSide з side (не надійно!)
                      params['positionSide'] = 'LONG' if order_side == 'sell' else 'SHORT' # sell для закриття long, buy для закриття short
                      self.logger.warning(f"Не вдалося визначити positionSide для ордеру {order_id}. Використовуємо припущення: {params['positionSide']}")

            elif order_type == 'limit':
                 # Для лімітних ордерів міняємо price
                 params['price'] = new_price
                 # Також може знадобитись positionSide
                 if 'info' in existing_order and 'positionSide' in existing_order['info']:
                     params['positionSide'] = existing_order['info']['positionSide']
            else:
                 self.logger.error(f"[BingXClient] Редагування ордерів типу '{order_type}' поки не підтримується цим методом.")
                 return None
                 
            # Округлення нової ціни (якщо це price або stopPrice)
            price_param_key = 'stopPrice' if order_type in ['STOP_MARKET', 'TAKE_PROFIT_MARKET'] else 'price'
            rounded_new_price_str = self.exchange.price_to_precision(ccxt_market_symbol, params[price_param_key])
            rounded_new_price = float(rounded_new_price_str)
            params[price_param_key] = rounded_new_price
            
            self.logger.info(f"Виклик edit_order для {order_id}: symbol={ccxt_market_symbol}, type={order_type}, side={order_side}, amount={amount_to_use}, params={params}")

            edited_order = self.exchange.edit_order(
                id=order_id, 
                symbol=ccxt_market_symbol, 
                type=order_type, 
                side=order_side, 
                amount=amount_to_use, 
                price=params.get('price'), # Передаємо price=None, якщо це SL/TP
                params=params
            )
            
            self.logger.info(f"[BingXClient] Ордер {order_id} успішно змінено (або створено новий з тим же ID).")
            self.logger.debug(f"Деталі зміненого ордеру: {edited_order}")
            return edited_order

        except ccxt.NotSupported as e:
            self.logger.error(f"[BingXClient] Біржа BingX (через ccxt) не підтримує edit_order: {e}. Потрібно реалізувати Cancel+Create.")
            # TODO: Додати логіку Cancel+Create тут або вище
            return None
        except ccxt.OrderNotFound as e:
            self.logger.warning(f"[BingXClient] Ордер {order_id} не знайдено під час спроби редагування: {e}")
            return None
        except ccxt.InvalidOrder as e:
             self.logger.error(f"[BingXClient] Неприпустимий запит на редагування ордера {order_id}: {e}", exc_info=True)
             return None
        except ccxt.ExchangeError as e:
            self.logger.error(f"[BingXClient] Помилка біржі при редагуванні ордера {order_id}: {e}", exc_info=True)
            return None
        except Exception as e:
            self.logger.error(f"[BingXClient] Невідома помилка при редагуванні ордера {order_id}: {e}", exc_info=True)
            return None

    def fetch_positions(self, symbol: str = None):
        """Отримує список відкритих позицій (для конкретного символу або всіх)."""
        if not self.exchange:
            self.logger.error("[BingXClient] Спроба викликати fetch_positions на неініціалізованому клієнті.")
            return None
            
        target_symbol = None
        if symbol:
            target_symbol = self._format_symbol_for_swap(symbol)
            self.logger.info(f"[BingXClient] Запит відкритих позицій для {target_symbol}...")
        else:
             self.logger.info(f"[BingXClient] Запит всіх відкритих позицій...")

        try:
            # fetch_positions може приймати список символів
            positions = self.exchange.fetch_positions(symbols=[target_symbol] if target_symbol else None)
            # Фільтруємо позиції з нульовим розміром (ccxt іноді повертає закриті)
            active_positions = [p for p in positions if p.get('contracts') is not None and float(p.get('contracts', 0)) != 0]
            
            self.logger.info(f"[BingXClient] Отримано {len(active_positions)} активних позицій" + (f" для {target_symbol}." if target_symbol else "."))
            self.logger.debug(f"Активні позиції: {active_positions}")
            return active_positions
            
        except ccxt.ExchangeError as e:
            self.logger.error(f"[BingXClient] Помилка біржі при запиті позицій" + (f" для {target_symbol}" if target_symbol else "") + f": {e}", exc_info=True)
            return None
        except Exception as e:
            self.logger.error(f"[BingXClient] Невідома помилка при запиті позицій" + (f" для {target_symbol}" if target_symbol else "") + f": {e}", exc_info=True)
            return None

    def cancel_order(self, symbol: str, order_id: str):
        """Скасовує конкретний ордер за його ID."""
        if not self.exchange:
            self.logger.error("[BingXClient] Спроба викликати cancel_order на неініціалізованому клієнті.")
            return None
            
        ccxt_market_symbol = self._format_symbol_for_swap(symbol)
        self.logger.info(f"[BingXClient] Спроба скасувати ордер ID: {order_id} для {ccxt_market_symbol}...")
        
        try:
            # ccxt.cancel_order повертає інформацію про скасований ордер або None/undefined
            # BingX може вимагати символ, навіть якщо ID унікальний
            result = self.exchange.cancel_order(order_id, ccxt_market_symbol) 
            self.logger.info(f"[BingXClient] Ордер {order_id} успішно скасовано (або вже був неактивний).")
            self.logger.debug(f"Результат скасування для {order_id}: {result}")
            # Повертаємо True для індикації успіху (навіть якщо ордер вже був закритий/скасований)
            # Якщо була помилка (напр., OrderNotFound), ccxt кине виняток
            return True 
        except ccxt.OrderNotFound as e:
            # Ордер вже не існує (був виконаний або скасований раніше) - це НЕ помилка для нас
            self.logger.warning(f"[BingXClient] Ордер {order_id} ({ccxt_market_symbol}) не знайдено під час скасування (можливо, вже закритий): {e}")
            return True # Вважаємо успіхом, бо ордера більше немає
        except ccxt.ExchangeError as e:
            self.logger.error(f"[BingXClient] Помилка біржі при скасуванні ордера {order_id} для {ccxt_market_symbol}: {e}", exc_info=True)
            return False
        except Exception as e:
            self.logger.error(f"[BingXClient] Невідома помилка при скасуванні ордера {order_id} для {ccxt_market_symbol}: {e}", exc_info=True)
            return False

if __name__ == "__main__":
    # Налаштування логера
    log_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    logging.basicConfig(level=logging.INFO, format=log_format)
    main_logger = logging.getLogger("BingXClientTest")

    # Завантаження ключів з .env
    load_dotenv()
    test_api_key = os.getenv("BINGX_API_KEY")
    test_api_secret = os.getenv("BINGX_API_SECRET")

    if not test_api_key or not test_api_secret:
        main_logger.critical("Не знайдено BINGX_API_KEY або BINGX_API_SECRET у файлі .env для тестування.")
        sys.exit(1)

    try:
        main_logger.info("Створення екземпляру BingXClient...")
        client = BingXClient(api_key=test_api_key, api_secret=test_api_secret, logger=main_logger)

        # Параметри сигналу зі скріншота
        symbol = 'EOSUSDT'
        direction = 'LONG'
        market_margin_usdt = 2.0  # Маржа для ринкового ордера
        limit_margin_usdt = 2.0   # Маржа для лімітного ордера
        leverage = 5              # Плече
        limit_price = 0.765       # Ціна лімітного ордера
        stop_loss_price = 0.750   # Стоп-лосс
        take_profit_prices = [0.860, 0.940]  # Ціни TP
        tp_distribution = [70, 30]  # Розподіл TP (70% і 30%)

        # 1. Розміщення ринкового ордера
        main_logger.info(f"\nРозміщення MARKET ордера для {symbol}...")
        market_order = client.place_market_order_basic(
            symbol=symbol,
            side='buy',
            position_side=direction,
            margin_usdt=market_margin_usdt,
            leverage=leverage
        )

        if not market_order or market_order.get('status') != 'closed':
            main_logger.error("Не вдалося розмістити ринковий ордер або він не виконався.")
            if market_order:
                print("\nРезультат ордеру:")
                print(market_order)
            sys.exit(1)

        main_logger.info("Ринковий ордер успішно розміщено.")
        print("\nРезультат ринкового ордеру:")
        print(market_order)

        # Отримуємо обсяг виконаного ринкового ордера
        market_amount = market_order.get('amount', 0)
        if market_amount <= 0:
            main_logger.error("Не вдалося отримати обсяг ринкового ордера.")
            sys.exit(1)

        # 2. Розміщення лімітного ордера
        main_logger.info(f"\nРозміщення LIMIT ордера для {symbol}...")
        limit_position_size_usdt = limit_margin_usdt * leverage
        ticker = client.exchange.fetch_ticker(client._format_symbol_for_swap(symbol))
        current_price = ticker.get('last')
        limit_amount = limit_position_size_usdt / limit_price  # Обсяг для лімітного ордера
        limit_amount = client._round_amount(limit_amount, client._format_symbol_for_swap(symbol))

        limit_order = client.place_limit_order(
            symbol=symbol,
            direction=direction,
            amount=limit_amount,
            limit_price=limit_price,
            leverage=leverage
        )

        if not limit_order:
            main_logger.error("Не вдалося розмістити лімітний ордер.")
            sys.exit(1)

        main_logger.info("Лімітний ордер успішно розміщено.")
        print("\nРезультат лімітного ордеру:")
        print(limit_order)

        # 3. Встановлення SL/TP для поточної позиції (лише ринковий ордер)
        main_logger.info(f"\nВстановлення Stop Loss для поточної позиції ({market_amount} EOS)...")
        sl_order = client.set_stop_loss(
            symbol=symbol,
            position_side=direction,
            initial_amount=market_amount,
            stop_loss_price=stop_loss_price
        )

        if not sl_order:
            main_logger.error("Не вдалося встановити Stop Loss.")
            sys.exit(1)

        main_logger.info("Stop Loss успішно встановлено.")
        print("\nРезультат SL ордеру:")
        print(sl_order)

        main_logger.info(f"\nВстановлення Take Profit для поточної позиції ({market_amount} EOS)...")
        tp_orders = client.set_take_profits(
            symbol=symbol,
            position_side=direction,
            initial_amount=market_amount,
            take_profit_prices=take_profit_prices,
            tp_distribution=tp_distribution
        )

        if not tp_orders:
            main_logger.error("Не вдалося встановити Take Profit.")
            sys.exit(1)

        main_logger.info(f"Take Profit успішно встановлено. Створено {len(tp_orders)} ордерів.")
        print("\nРезультати TP ордерів:")
        for i, tp_order in enumerate(tp_orders):
            print(f"--- TP {i+1} ---")
            print(tp_order)

        # 4. Очікування виконання лімітного ордера та оновлення SL/TP
        limit_order_id = limit_order['id']
        total_amount = market_amount + limit_amount
        main_logger.info(f"\nОчікування виконання лімітного ордера (ID: {limit_order_id})...")
        while True:
            try:
                order_status = client.exchange.fetch_order(limit_order_id, symbol)
                if order_status['status'] == 'closed':
                    main_logger.info("Лімітний ордер виконано! Оновлюємо SL/TP для загальної позиції...")
                    break
                elif order_status['status'] == 'canceled':
                    main_logger.info("Лімітний ордер скасовано. SL/TP залишаються без змін.")
                    break
                else:
                    main_logger.info("Лімітний ордер ще не виконано. Чекаємо...")
                    time.sleep(60)
            except Exception as e:
                main_logger.error(f"Помилка при перевірці статусу лімітного ордера: {e}", exc_info=True)
                break

        if order_status['status'] == 'closed':
            # Скасування попередніх SL/TP
            main_logger.info("Скасування попередніх SL/TP...")
            client.cancel_open_orders(symbol, [sl_order['id']] + [tp['id'] for tp in tp_orders])

            # Новий SL для загальної позиції
            main_logger.info(f"Встановлення нового Stop Loss для загальної позиції ({total_amount} EOS)...")
            new_sl_order = client.set_stop_loss(
                symbol=symbol,
                position_side=direction,
                initial_amount=total_amount,
                stop_loss_price=stop_loss_price
            )

            if not new_sl_order:
                main_logger.error("Не вдалося встановити новий Stop Loss.")
                sys.exit(1)

            main_logger.info("Новий Stop Loss успішно встановлено.")
            print("\nРезультат нового SL ордеру:")
            print(new_sl_order)

            # Нові TP для загальної позиції
            main_logger.info(f"Встановлення нових Take Profit для загальної позиції ({total_amount} EOS)...")
            new_tp_orders = client.set_take_profits(
                symbol=symbol,
                position_side=direction,
                initial_amount=total_amount,
                take_profit_prices=take_profit_prices,
                tp_distribution=tp_distribution
            )

            if not new_tp_orders:
                main_logger.error("Не вдалося встановити нові Take Profit.")
                sys.exit(1)

            main_logger.info(f"Нові Take Profit успішно встановлено. Створено {len(new_tp_orders)} ордерів.")
            print("\nРезультати нових TP ордерів:")
            for i, tp_order in enumerate(new_tp_orders):
                print(f"--- TP {i+1} ---")
                print(tp_order)

    except Exception as init_error:
        main_logger.critical(f"Не вдалося ініціалізувати BingXClient або виконати тест: {init_error}", exc_info=True)
        sys.exit(1)

    main_logger.info("\nРоботу тестового блоку завершено.")