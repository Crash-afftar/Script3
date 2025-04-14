import websocket
import json
import time
import hmac
import hashlib
import threading
import gzip
import io

# Налаштування API ключів
API_KEY = '6M15q6s4P0vQT9I2BwdPoBgqbrPAwUQJqzcKMzAMY0jpk5zP1ToSklF2iDnGj5obmtHmdh6knNYFTNA'
API_SECRET = '1g8c3ZV5TGGFYE9ZJTSXMEfKs9rusx9bMHYLhruprks6oBezJy1PurHe9EQFBbNq9FQlZjLzssoNXkyhXGQ'

# Правильна URL для WebSocket API BingX згідно документації
WS_URL = 'wss://open-api-swap.bingx.com/swap-market'

# Функція для генерації підпису
def generate_signature(params, secret_key):
    # Відсортуйте параметри за ключами
    sorted_params = sorted(params.items())
    
    # Створіть рядок запиту в точному форматі, який очікує BingX
    query_string = '&'.join([f"{key}={value}" for key, value in sorted_params])
    
    # Генеруємо HMAC-SHA256 підпис
    signature = hmac.new(
        secret_key.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    return signature

# Функція для декодування відповіді WebSocket (розпакування GZIP)
def decode_message(message):
    # Перевіряємо, чи дані стиснуті GZIP
    if message.startswith(b'\x1f\x8b\x08'):
        try:
            # Розпаковуємо GZIP дані
            with gzip.GzipFile(fileobj=io.BytesIO(message), mode='rb') as f:
                decompressed_data = f.read()
            # Декодуємо розпаковані дані в UTF-8
            return decompressed_data.decode('utf-8')
        except Exception as e:
            print(f"Помилка декодування GZIP: {e}")
            return None
    else:
        # Якщо дані не стиснуті, просто декодуємо їх
        try:
            return message.decode('utf-8')
        except Exception as e:
            print(f"Помилка декодування: {e}")
            return None

# Обробник отриманих повідомлень WebSocket
def on_message(ws, message):
    # Декодуємо отримане повідомлення
    decoded_message = decode_message(message)
    if decoded_message:
        try:
            data = json.loads(decoded_message)
            print(f"Отримано повідомлення: {json.dumps(data, indent=2)}")
            
            # Обробка відповіді на пінг
            if 'pong' in data:
                print("Отримано pong")
                return
                
            # Перевірка на успішність автентифікації
            if 'id' in data and data['id'] == 'auth':
                if data.get('code') == 0:
                    print("Автентифікація успішна")
                    # Після успішної автентифікації підписуємося на дані
                    subscribe_to_topics(ws)
                else:
                    print(f"Помилка автентифікації: {data.get('msg')}")
                return
                
            # Перевірка підписки
            if 'id' in data and data['id'] in ['positions', 'userTrades', 'balance']:
                if data.get('code') == 0:
                    print(f"Підписка успішна: {data['id']}")
                else:
                    print(f"Помилка підписки {data['id']}: {data.get('msg')}")
                return
                
            # Обробка повідомлень з даними про позиції
            if 'topic' in data:
                if data['topic'] == 'positions':
                    process_positions_data(data)
                elif data['topic'] == 'userTrades':
                    process_user_trades_data(data)
                elif data['topic'] == 'balance':
                    process_balance_data(data)
                
        except json.JSONDecodeError as e:
            print(f"Помилка розбору JSON: {e}")
            print(f"Отримане повідомлення: {decoded_message}")
    else:
        print("Не вдалося декодувати повідомлення")

# Обробка даних про позиції
def process_positions_data(data):
    positions_data = data.get('data', [])
    
    if positions_data:
        print("\n=== ІНФОРМАЦІЯ ПРО ПОЗИЦІЇ ===")
        for position in positions_data:
            print(f"Символ: {position.get('symbol')}")
            print(f"Розмір позиції: {position.get('positionAmt')}")
            side = 'LONG' if float(position.get('positionAmt', '0')) > 0 else 'SHORT'
            print(f"Сторона: {side}")
            print(f"Вхідна ціна: {position.get('entryPrice')}")
            print(f"Плече: {position.get('leverage')}")
            
            # Інформація про TP/SL
            take_profit = position.get('takeProfitPrice', 'Не встановлено')
            stop_loss = position.get('stopLossPrice', 'Не встановлено')
            
            print(f"Take Profit (ТП): {take_profit}")
            print(f"Stop Loss (СЛ): {stop_loss}")
            print("---------------------")

# Обробка даних про трейди користувача
def process_user_trades_data(data):
    trades_data = data.get('data', [])
    
    if trades_data:
        print("\n=== ІНФОРМАЦІЯ ПРО ТРЕЙДИ ===")
        for trade in trades_data:
            print(f"Символ: {trade.get('symbol')}")
            print(f"ID ордеру: {trade.get('orderId')}")
            print(f"Ціна: {trade.get('price')}")
            print(f"Кількість: {trade.get('qty')}")
            print(f"Сторона: {trade.get('side')}")
            print(f"Час: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(trade.get('time', 0)) / 1000))}")
            print("---------------------")

# Обробка даних про баланс
def process_balance_data(data):
    balance_data = data.get('data', {})
    
    if balance_data:
        print("\n=== ІНФОРМАЦІЯ ПРО БАЛАНС ===")
        print(f"Загальний баланс: {balance_data.get('totalWalletBalance')}")
        print(f"Доступний баланс: {balance_data.get('availableBalance')}")
        print("---------------------")

def on_error(ws, error):
    print(f"Помилка WebSocket: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"WebSocket з'єднання закрито: {close_status_code} - {close_msg}")

def on_open(ws):
    print("WebSocket з'єднання відкрито")
    
    # Виконуємо автентифікацію
    authenticate(ws)
    
    # Підтримуємо з'єднання активним
    def ping_server():
        while True:
            time.sleep(30)
            ping_data = {"ping": int(time.time() * 1000)}
            ws.send(json.dumps(ping_data))
            print(f"Відправлено пінг: {json.dumps(ping_data)}")
    
    threading.Thread(target=ping_server, daemon=True).start()

def authenticate(ws):
    timestamp = int(time.time() * 1000)
    params = {
        'apiKey': API_KEY,
        'timestamp': timestamp
    }
    
    signature = generate_signature(params, API_SECRET)
    
    # Формат автентифікації згідно документації
    auth_message = {
        'id': 'auth',
        'apiKey': API_KEY,
        'timestamp': timestamp,
        'signature': signature
    }
    
    ws.send(json.dumps(auth_message))
    print(f"Надіслано запит на автентифікацію: {json.dumps(auth_message)}")

def subscribe_to_topics(ws):
    # Підписка на позиції (згідно документації)
    positions_subscribe = {
        'id': 'positions',
        'topic': 'positions',
        'params': {}
    }
    ws.send(json.dumps(positions_subscribe))
    print(f"Підписка на позиції: {json.dumps(positions_subscribe)}")
    
    # Підписка на трейди користувача
    user_trades_subscribe = {
        'id': 'userTrades',
        'topic': 'userTrades',
        'params': {}
    }
    ws.send(json.dumps(user_trades_subscribe))
    print(f"Підписка на трейди: {json.dumps(user_trades_subscribe)}")
    
    # Підписка на дані балансу
    balance_subscribe = {
        'id': 'balance',
        'topic': 'balance',
        'params': {}
    }
    ws.send(json.dumps(balance_subscribe))
    print(f"Підписка на баланс: {json.dumps(balance_subscribe)}")

def run_websocket():
    print("Запуск WebSocket підключення до BingX...")
    websocket.enableTrace(True)  # Включаємо логування для діагностики
    ws = websocket.WebSocketApp(WS_URL,
                              on_open=on_open,
                              on_message=on_message,
                              on_error=on_error,
                              on_close=on_close)
    
    # Запускаємо WebSocket з повторними спробами підключення
    while True:
        try:
            ws.run_forever()
            print("Перепідключення за 5 секунд...")
            time.sleep(5)
        except Exception as e:
            print(f"Критична помилка: {e}")
            print("Перезапуск за 10 секунд...")
            time.sleep(10)

if __name__ == "__main__":
    run_websocket()