import time
import os
import ccxt
import pandas as pd
import ta

# --- CONFIGURATION & ENV VARIABLES ---
API_KEY = 'nGn7y68cpUDUC1AbEGbNnzcGu0FIXx'
API_SECRET = 'EbKGn8vw2f1HHacJRENw8ydqoV60rSlGWKks4nTYGdD9A2SSpEUimhL3RvP2'

SYMBOL = 'ETHUSD'
       # Delta Exchange trading pair
TIMEFRAME = '1Hr'         # 15 Minute Scalping Timeframe
LEVERAGE = 1              # Dynamic Lot Size multiplier / Leverage

# Strategy Parameters
EMA_FAST = 9
EMA_SLOW = 20
SR_PERIOD = 15
RR_RATIO = 2.0
ATR_LENGTH = 14
ATR_MULT = 1.0

# Trailing Stop Loss Settings
USE_TRAILING = True
TRAIL_OFFSET_ATR = 1.5   # ATR multiplier for trailing step

# --- DELTA EXCHANGE SETUP ---
exchange = ccxt.delta({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'urls': {
        'api': {
            'public': 'https://api.india.delta.exchange',
            'private': 'https://api.india.delta.exchange',
        }
    },
    'options': {'defaultType': 'swap'}
})

# Active Trade SL Tracker (Memory State)
active_trade = {
    'side': None,        # 'long' ya 'short'
    'entry_price': 0.0,
    'sl_price': 0.0,
    'tp_price': 0.0,
    'sl_order_id': None
}

def fetch_and_calculate():
    """Delta Exchange se candles fetch karke Pine Script strategy recalculate karta hai"""
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # Technical Indicators
    df['ema_fast'] = ta.trend.ema_indicator(df['close'], window=EMA_FAST)
    df['ema_slow'] = ta.trend.ema_indicator(df['close'], window=EMA_SLOW)
    
    # Structural S&R
    df['highest_high'] = df['high'].shift(1).rolling(window=SR_PERIOD).max()
    df['lowest_low'] = df['low'].shift(1).rolling(window=SR_PERIOD).min()
    
    # Volume Filter & ATR
    df['vol_sma'] = ta.trend.sma_indicator(df['volume'], window=20)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=ATR_LENGTH)
    
    return df

def get_position_size(current_price):
    """Account me jitna USDT balance hai, uske 100% ka lot size dynamically nikalta hai"""
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance['USDT']['free']
        trade_amount = (usdt_free * LEVERAGE) / current_price
        return round(trade_amount, 3)
    except Exception as e:
        print(f"Balance fetch error: {e}")
        return 0.0

def update_trailing_stop(current_price, atr_val):
    """Active Trade par Stop Loss ko trail (move) karne ka function"""
    global active_trade
    trail_dist = atr_val * TRAIL_OFFSET_ATR
    
    # LONG POSITION TRAILING LOGIC
    if active_trade['side'] == 'long':
        new_sl = current_price - trail_dist
        # Trailing SL humesha upar shift hoga, niche nahi
        if new_sl > active_trade['sl_price']:
            print(f"🔄 Trailing BUY SL: Old {active_trade['sl_price']:.2f} ➔ New {new_sl:.2f}")
            try:
                if active_trade['sl_order_id']:
                    exchange.cancel_order(active_trade['sl_order_id'], SYMBOL)
                
                # Naya Stop Loss order place karo
                amount = get_position_size(current_price)
                new_order = exchange.create_order(SYMBOL, 'stop_market', 'sell', amount, params={'stopPrice': round(new_sl, 2), 'reduceOnly': True})
                active_trade['sl_price'] = new_sl
                active_trade['sl_order_id'] = new_order['id']
            except Exception as e:
                print(f"Trailing SL Update Failed: {e}")

    # SHORT POSITION TRAILING LOGIC
    elif active_trade['side'] == 'short':
        new_sl = current_price + trail_dist
        # Trailing SL humesha niche shift hoga, upar nahi
        if new_sl < active_trade['sl_price'] or active_trade['sl_price'] == 0:
            print(f"🔄 Trailing SELL SL: Old {active_trade['sl_price']:.2f} ➔ New {new_sl:.2f}")
            try:
                if active_trade['sl_order_id']:
                    exchange.cancel_order(active_trade['sl_order_id'], SYMBOL)
                
                # Naya Stop Loss order place karo
                amount = get_position_size(current_price)
                new_order = exchange.create_order(SYMBOL, 'stop_market', 'buy', amount, params={'stopPrice': round(new_sl, 2), 'reduceOnly': True})
                active_trade['sl_price'] = new_sl
                active_trade['sl_order_id'] = new_order['id']
            except Exception as e:
                print(f"Trailing SL Update Failed: {e}")

def execute_trade():
    global active_trade
    df = fetch_and_calculate()
    
    last = df.iloc[-2]
    prev = df.iloc[-3]
    current_price = last['close']
    atr_val = last['atr']
    
    # Open positions check from exchange
    positions = exchange.fetch_positions([SYMBOL])
    has_open_position = any(float(p['contracts']) > 0 for p in positions)
    
    # Agar position closed ho chuki hai to state reset karein
    if not has_open_position:
        if active_trade['side'] is not None:
            print("Trade closed on exchange. Resetting tracker.")
            active_trade = {'side': None, 'entry_price': 0.0, 'sl_price': 0.0, 'tp_price': 0.0, 'sl_order_id': None}
    else:
        # Active trade chal raha hai -> Trailing SL Update check karein
        if USE_TRAILING:
            update_trailing_stop(current_price, atr_val)
        return

    # Fake Breakout Filters
    volume_filter = last['volume'] > last['vol_sma']
    bullish_candle = last['close'] > last['open']
    bearish_candle = last['close'] < last['open']
    
    # Entry Signals
    buy_signal = (prev['ema_fast'] <= prev['ema_slow']) and (last['ema_fast'] > last['ema_slow']) and (last['close'] > last['highest_high']) and volume_filter and bullish_candle
    sell_signal = (prev['ema_fast'] >= prev['ema_slow']) and (last['ema_fast'] < last['ema_slow']) and (last['close'] < last['lowest_low']) and volume_filter and bearish_candle

    # BUY TRADE LOGIC
    if buy_signal:
        amount = get_position_size(current_price)
        if amount <= 0:
            return

        sl_price = current_price - (atr_val * ATR_MULT)
        tp_price = current_price + ((current_price - sl_price) * RR_RATIO)
        
        print(f"🚀 BUY SIGNAL DETECTED | Price: {current_price} | SL: {sl_price:.2f} | TP: {tp_price:.2f}")
        
        # Market Order
        exchange.create_order(SYMBOL, 'market', 'buy', amount)
        
        # SL & TP Orders
        sl_order = exchange.create_order(SYMBOL, 'stop_market', 'sell', amount, params={'stopPrice': round(sl_price, 2), 'reduceOnly': True})
        exchange.create_order(SYMBOL, 'take_profit_market', 'sell', amount, params={'stopPrice': round(tp_price, 2), 'reduceOnly': True})

        # Save active trade state
        active_trade = {
            'side': 'long',
            'entry_price': current_price,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'sl_order_id': sl_order['id']
        }

    # SELL TRADE LOGIC
    elif sell_signal:
        amount = get_position_size(current_price)
        if amount <= 0:
            return

        sl_price = current_price + (atr_val * ATR_MULT)
        tp_price = current_price - ((sl_price - current_price) * RR_RATIO)
        
        print(f"📉 SELL SIGNAL DETECTED | Price: {current_price} | SL: {sl_price:.2f} | TP: {tp_price:.2f}")
        
        # Market Order
        exchange.create_order(SYMBOL, 'market', 'sell', amount)
        
        # SL & TP Orders
        sl_order = exchange.create_order(SYMBOL, 'stop_market', 'buy', amount, params={'stopPrice': round(sl_price, 2), 'reduceOnly': True})
        exchange.create_order(SYMBOL, 'take_profit_market', 'buy', amount, params={'stopPrice': round(tp_price, 2), 'reduceOnly': True})

        # Save active trade state
        active_trade = {
            'side': 'short',
            'entry_price': current_price,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'sl_order_id': sl_order['id']
        }

# --- CONTINUOUS 24/7 LOOP ---
if __name__ == "__main__":
    print("🤖 9/20 EMA Scalping Bot + Dynamic Trailing SL Active...")
    while True:
        try:
            execute_trade()
        except Exception as e:
            print(f"Error occurred: {e}")
        
        time.sleep(60)
