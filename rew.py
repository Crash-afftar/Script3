import ccxt
import os
from dotenv import load_dotenv
import time
import json

# Завантаження змінних середовища
load_dotenv()
api_key = os.getenv("BINGX_API_KEY")
api_secret = os.getenv("BINGX_API_SECRET")

if not all([api_key, api_secret]):
    print("Помилка: Не знайдено BINGX_API_KEY або BINGX_SECRET_KEY в .env файлі.")
    exit()

# Ініціалізація біржі BingX
exchange = ccxt.bingx({
    'apiKey': api_key,
    'secret': api_secret,
    'enableRateLimit': True,
})

# Встановлення типу ринку на ф’ючерси (swap)
exchange.options['defaultType'] = 'swap'

# --- Параметри для тесту ---
symbol_ccxt = 'LTC/USDT:USDT' # Формат для ccxt
order_side = 'buy'
position_side = 'LONG'
leverage = 10
margin_usdt = 5 # Маленька маржа для тесту
stop_loss_price = 60 # ! Встановіть РЕАЛЬНУ ціну SL нижче поточної ринкової!
# take_profit_price = 90 # Можна додати TP, якщо потрібно

print(f"--- Тест встановлення SL при створенні ордера для {symbol_ccxt} ---")
print(f"Маржа: {margin_usdt} USDT, Плече: {leverage}x")
print(f"Цільовий SL: {stop_loss_price}")

# 0. Встановлення кредитного плеча
try:
    print(f"\nВстановлення плеча {leverage}x для {symbol_ccxt} ({position_side})...")
    exchange.set_leverage(leverage, symbol_ccxt, params={'side': position_side})
    print(f"Плече встановлено.")
except Exception as e:
    print(f"Помилка при встановленні плеча: {e}. Продовжуємо...")

# 1. Відкриття ринкової позиції ЗІ СПРОБОЮ ВСТАНОВЛЕННЯ SL
market_order = None
order_id = None
actual_amount = None
try:
    print(f"\n--- Крок 1: Відкриття {position_side} позиції по ринку з параметром SL ---")
    ticker = exchange.fetch_ticker(symbol_ccxt)
    current_price = ticker['last']
    if not current_price: raise ValueError("Не вдалося отримати поточну ціну.")
    print(f"Поточна ціна: {current_price}")
    
    position_size_usdt = margin_usdt * leverage
    amount = position_size_usdt / current_price
    amount = exchange.amount_to_precision(symbol_ccxt, amount)
    
    print(f"Розрахована кількість: {amount} {symbol_ccxt.split('/')[0]}")

    # Формуємо параметри для ордера
    params = {
        'positionSide': position_side,
        'stopLossPrice': stop_loss_price, # <--- Спроба встановити SL тут
        # 'takeProfitPrice': take_profit_price, # <--- Можна додати і TP
        # Можливо, потрібен інший формат, наприклад:
        # 'stopLoss': { 'price': stop_loss_price }
        # 'slTriggerPrice': stop_loss_price
    }
    print(f"Параметри для create_order: {params}")

    market_order = exchange.create_market_order(
        symbol=symbol_ccxt,
        side=order_side,
        amount=amount,
        params=params
    )
    order_id = market_order.get('id')
    actual_amount = market_order.get('filled')
    print(f"Маркет-ордер створено. ID: {order_id}")
    print(f"Деталі ордера: {market_order}")

    if market_order.get('status') != 'closed': # 'closed' означає FILLED для market order в ccxt
        print("ПОПЕРЕДЖЕННЯ: Ордер не був повністю виконаний одразу!")
        # Можливо, потрібно додати перевірку статусу ордера в циклі

    # Затримка, щоб позиція точно з'явилась
    print("\nПауза 5 секунд...")
    time.sleep(5)

except Exception as e:
    print(f"Помилка при створенні ринкового ордера з SL: {e}")
    # Не виходимо, спробуємо перевірити позиції, якщо ордер частково створився
    # exit() # Розкоментуйте, якщо хочете зупинятися при помилці створення

# 2. Перевірка відкритої позиції та її SL/TP
print(f"\n--- Крок 2: Перевірка відкритої позиції {symbol_ccxt} ({position_side}) ---")
try:
    # Використовуємо fetch_positions для отримання всіх позицій
    # Або fetch_position, якщо потрібно отримати конкретну (але ccxt може не мати fetch_position)
    # Деякі біржі потребують передати символ у fetch_positions
    positions = exchange.fetch_positions(symbols=[symbol_ccxt]) # Передаємо символ
    
    found_position = None
    if positions:
        print(f"Знайдено {len(positions)} позицій для {symbol_ccxt}.")
        # Шукаємо нашу позицію (за символом та стороною)
        for pos in positions:
            # Перевіряємо символ і бік позиції
            # BingX може повертати 'side' як 'long'/'short' або 'both'
            pos_side_api = pos.get('side') 
            # Порівняння сторін може бути складним, positionSide надійніше, ЯКЩО він є
            if pos.get('symbol') == symbol_ccxt and pos_side_api and pos_side_api.upper() == position_side:
                 found_position = pos
                 print(f"\nЗнайдено відповідну позицію:")
                 # Виводимо ключові поля (можуть відрізнятися для BingX)
                 print(f"  Symbol: {pos.get('symbol')}")
                 print(f"  Side: {pos.get('side')}")
                 print(f"  Contracts: {pos.get('contracts')}") # Або 'contractSize'
                 print(f"  Entry Price: {pos.get('entryPrice')}")
                 print(f"  Leverage: {pos.get('leverage')}")
                 print(f"  Liquidation Price: {pos.get('liquidationPrice')}")
                 # Найважливіше - шукаємо SL/TP
                 print(f"  Stop Loss Price: {pos.get('stopLossPrice')}") 
                 print(f"  Take Profit Price: {pos.get('takeProfitPrice')}")
                 # Іноді SL/TP може бути в info або params
                 print(f"  Raw Info (частково): {str(pos.get('info', {}))[:200]}...") 
                 break # Знайшли нашу позицію
        if not found_position:
            print(f"Не знайдено активної позиції для {symbol_ccxt} ({position_side}) серед отриманих.")
    else:
        print(f"Не знайдено активних позицій для {symbol_ccxt}.")

except ccxt.NotSupported as e:
    print(f"Помилка: Біржа BingX через ccxt не підтримує fetch_positions або потребує інших параметрів: {e}")
except Exception as e:
    print(f"Помилка при отриманні позицій: {e}")


# 3. (Опціонально) Закриття позиції для очищення
close_position = False # Змініть на True, якщо хочете закривати
if close_position and order_id and actual_amount and actual_amount > 0:
    print("\n--- Крок 3: Закриття позиції ---")
    try:
        close_side = 'sell' if position_side == 'LONG' else 'buy'
        print(f"Спроба закрити {actual_amount} {symbol_ccxt.split('/')[0]}...")
        close_order = exchange.create_market_order(
            symbol=symbol_ccxt,
            side=close_side,
            amount=actual_amount,
            params={
                'positionSide': position_side,
                'reduceOnly': True
            }
        )
        print("Ордер на закриття позиції відправлено.")
        print(f"Деталі ордера закриття: {close_order}")
    except Exception as e:
        print(f"Помилка при закритті позиції: {e}")
elif close_position:
     print("\nПропуск закриття позиції (немає ID ордера або обсягу).")

print("\n--- Тест завершено ---")