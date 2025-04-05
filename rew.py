import ccxt

# Ваші API ключі від BingX
api_key = "6M15q6s4P0vQT9I2BwdPoBgqbrPAwUQJqzcKMzAMY0jpk5zP1ToSklF2iDnGj5obmtHmdh6knNYFTNA"
api_secret = "1g8c3ZV5TGGFYE9ZJTSXMEfKs9rusx9bMHYLhruprks6oBezJy1PurHe9EQFBbNq9FQlZjLzssoNXkyhXGQ"

# Ініціалізація біржі BingX
exchange = ccxt.bingx({
    'apiKey': api_key,
    'secret': api_secret,
    'enableRateLimit': True,  # Обмеження запитів
})

# Встановлення типу ринку на ф’ючерси (perpetual futures)
exchange.options['defaultType'] = 'swap'

# Параметри ордеру
symbol = 'BTC-USDT'  # Торгова пара Bitcoin/USDT
margin_usdt = 10     # Маржа в USDT для кожного ордеру
leverage = 10        # Кредитне плече

# Встановлення кредитного плеча для LONG-позиції
try:
    exchange.set_leverage(leverage, symbol, params={'side': 'LONG'})
    print(f"Кредитне плече {leverage}x встановлено для {symbol} (LONG)")
except Exception as e:
    print("Помилка при встановленні плеча:", str(e))

# Отримання поточної ринкової ціни BTC/USDT
ticker = exchange.fetch_ticker(symbol)
current_price = ticker['last']  # Остання ціна BTC в USDT
print(f"Поточна ціна BTC/USDT: {current_price} USDT")

# Розрахунок загального розміру позиції (notional value) на основі маржі та плеча
position_size_usdt = margin_usdt * leverage  # 10 USDT * 10x = 100 USDT

# Розрахунок кількості BTC на основі загального розміру позиції
amount_in_btc = position_size_usdt / current_price

# Перевірка мінімальної кількості
min_amount = 0.0001  # Мінімальна кількість BTC для BTC/USDT
if amount_in_btc < min_amount:
    amount_in_btc = min_amount
    position_size_usdt = amount_in_btc * current_price  # Оновлюємо розмір позиції
    margin_usdt = position_size_usdt / leverage  # Оновлюємо маржу
    print(f"Попередження: сума скоригована до мінімальної кількості {min_amount} BTC")
    print(f"Нова маржа: {margin_usdt:.2f} USDT, розмір позиції: {position_size_usdt:.2f} USDT")

# Округлення кількості до 4 знаків після коми (як це робить BingX)
amount_in_btc = round(amount_in_btc, 4)
print(f"Округлена кількість для одного ордеру: {amount_in_btc:.6f} BTC")

# 1. Відкриття ринкового ордеру на купівлю (long) на 10 USDT
try:
    market_order = exchange.create_market_buy_order(
        symbol=symbol,
        amount=amount_in_btc,  # Кількість у BTC
        params={'leverage': leverage, 'positionSide': 'LONG'}
    )
    print(f"Маркет-ордер успішно створено: куплено {amount_in_btc:.6f} BTC")
    print(f"Маржа: {margin_usdt:.2f} USDT, розмір позиції: {position_size_usdt:.2f} USDT")
    print("Деталі маркет-ордеру:", market_order)
except Exception as e:
    print("Помилка при створенні маркет-ордеру:", str(e))

# 2. Встановлення лімітного ордеру на купівлю (long) на ціні 82,000 USDT на 10 USDT
limit_price = 82000  # Ціна лімітного ордеру
limit_amount_in_btc = position_size_usdt / limit_price  # Кількість BTC для лімітного ордеру
limit_amount_in_btc = round(limit_amount_in_btc, 4)  # Округлення до 4 знаків
try:
    limit_order = exchange.create_limit_buy_order(
        symbol=symbol,
        amount=limit_amount_in_btc,  # Кількість у BTC
        price=limit_price,  # Ціна лімітного ордеру
        params={'leverage': leverage, 'positionSide': 'LONG'}
    )
    print(f"Лімітний ордер успішно створено: купівля {limit_amount_in_btc:.6f} BTC на ціні {limit_price} USDT")
    print("Деталі лімітного ордеру:", limit_order)
except Exception as e:
    print("Помилка при створенні лімітного ордеру:", str(e))

# Загальна кількість BTC (якщо обидва ордери виконаються)
total_amount_in_btc = amount_in_btc + limit_amount_in_btc
print(f"Загальна кількість BTC (якщо лімітний ордер виконається): {total_amount_in_btc:.6f} BTC")

# 3. Встановлення стоп-лоссу на рівні 75,000 USDT для всієї позиції
stop_loss_price = 75000  # Ціна стоп-лоссу в USDT
try:
    # Спочатку встановлюємо стоп-лосс для поточної позиції (маркет-ордер)
    stop_loss_order = exchange.create_order(
        symbol=symbol,
        type='STOP_MARKET',  # Тип ордеру - стоп-маркет
        side='sell',         # Продаж для закриття LONG-позиції
        amount=amount_in_btc, # Кількість для поточної позиції
        params={
            'stopPrice': stop_loss_price,  # Ціна активації стоп-лоссу
            'positionSide': 'LONG',        # Для LONG-позиції
            'workingType': 'MARK_PRICE'    # Виконання за ринковою ціною
        }
    )
    print(f"Стоп-лосс успішно встановлено на {stop_loss_price} USDT для {amount_in_btc:.6f} BTC")
    print("Деталі стоп-лосс ордеру:", stop_loss_order)
except Exception as e:
    print("Помилка при встановленні стоп-лоссу:", str(e))

# 4. Встановлення тейк-профітів (для поточної позиції, але оновимо після виконання лімітного ордеру)
# TP1: 70% від позиції на 87,000 USDT
tp1_percentage = 0.7  # 70%
tp1_amount = amount_in_btc * tp1_percentage  # 70% від поточної позиції
tp1_amount = round(tp1_amount, 4)  # Округлення до 4 знаків
try:
    tp1_order = exchange.create_order(
        symbol=symbol,
        type='TAKE_PROFIT_MARKET',
        side='sell',
        amount=tp1_amount,
        params={
            'stopPrice': 87000,  # Ціна TP1
            'positionSide': 'LONG',
            'workingType': 'MARK_PRICE'
        }
    )
    print(f"TP1 ({tp1_percentage*100}%) успішно встановлено на 87,000 USDT для {tp1_amount:.6f} BTC")
    print("Деталі TP1:", tp1_order)
except Exception as e:
    print("Помилка при встановленні TP1:", str(e))

# TP2: 30% від позиції на 90,000 USDT
tp2_percentage = 0.3  # 30%
tp2_amount = amount_in_btc * tp2_percentage  # 30% від поточної позиції
tp2_amount = round(tp2_amount, 4)  # Округлення до 4 знаків
try:
    tp2_order = exchange.create_order(
        symbol=symbol,
        type='TAKE_PROFIT_MARKET',
        side='sell',
        amount=tp2_amount,
        params={
            'stopPrice': 90000,  # Ціна TP2
            'positionSide': 'LONG',
            'workingType': 'MARK_PRICE'
        }
    )
    print(f"TP2 ({tp2_percentage*100}% від початкової) успішно встановлено на 90,000 USDT для {tp2_amount:.6f} BTC")
    print("Деталі TP2:", tp2_order)
except Exception as e:
    print("Помилка при встановленні TP2:", str(e))

# Додаткова логіка: оновлення TP/SL після виконання лімітного ордеру
print("УВАГА: Якщо лімітний ордер виконається, потрібно оновити TP/SL для загальної позиції!")
print("Загальна кількість для TP/SL після виконання лімітного ордеру:")
print(f"TP1: {total_amount_in_btc * 0.7:.6f} BTC")
print(f"TP2: {total_amount_in_btc * 0.3:.6f} BTC")
print(f"SL: {total_amount_in_btc:.6f} BTC")