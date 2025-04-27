from fastapi import FastAPI, HTTPException, BackgroundTasks
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
from typing import Dict, List, Optional
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from datetime import datetime
import threading
import time
import schedule
import asyncio
import json
from binance import AsyncClient, BinanceSocketManager
import pdb # breakpoint code # pdb.set_trace()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Binance Testnet Trading Bot",
    description="A simple trading bot for Binance Testnet using the official Python client",
    version="1.0.0"
)

# Configuration
# Load environment variables from .env file
load_dotenv()
# In production, use environment variables instead of hardcoding
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
DEFAULT_SYMBOL = "BTCUSDT"

# Validate that keys are available
if not API_KEY or not API_SECRET:
    raise ValueError("Binance API credentials not found in environment variables")

# Initialize Binance client
client = Client(API_KEY, API_SECRET, testnet=True)

# Bot state
bot_running = False
last_check_time = None
price_history = []
trade_history = []
websocket_running = False
latest_price = None
latest_price_time = None

# Models
class PriceData(BaseModel):
    symbol: str
    price: float
    timestamp: str

class TradeSimulation(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float
    timestamp: str
    order_id: str

class BotStatus(BaseModel):
    is_running: bool
    last_check_time: Optional[str] = None
    price_history: List[PriceData] = []
    trade_history: List[TradeSimulation] = []
    websocket_status: bool = False
    latest_price: Optional[float] = None
    latest_price_time: Optional[str] = None

# Helper functions
def get_current_price(symbol: str = DEFAULT_SYMBOL) -> float:
    """Get current price for a symbol using Binance client."""
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        return float(ticker['price'])
    except BinanceAPIException as e:
        logger.error(f"Binance API error: {e}")
        raise HTTPException(status_code=500, detail=f"Binance API error: {str(e)}")
    except Exception as e:
        logger.error(f"Error getting price: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get price: {str(e)}")

# Add this function to set and maintain leverage
def set_and_verify_leverage(symbol="BTCUSDT", target_leverage=2):
    """Set and verify leverage for the specified symbol"""
    try:
        # Set leverage to target value
        response = client.futures_change_leverage(
            symbol=symbol, 
            leverage=target_leverage
        )
        
        # Verify the leverage was set correctly
        current_leverage = int(response['leverage'])
        
        if current_leverage == target_leverage:
            logger.info(f"Successfully set {symbol} leverage to {target_leverage}x")
            return True
        else:
            logger.error(f"Failed to set leverage. Current: {current_leverage}x, Target: {target_leverage}x")
            return False
            
    except BinanceAPIException as e:
        logger.error(f"Binance API error setting leverage: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error setting leverage: {e}")
        return False

# Add this function to ensure the margin is isolated and not crossed
def set_margin_mode_isolated(symbol: str):
    """Set margin mode to ISOLATED for a symbol."""
    try:
        response = client.futures_change_margin_type(symbol=symbol, marginType='ISOLATED')
        if response.get('code') == 200 or response.get('msg') == 'success':
            logger.info(f"Margin mode set to ISOLATED for {symbol}")
            return True
        else:
            logger.error(f"Failed to set margin mode to ISOLATED for {symbol}: {response}")
            return False
    except BinanceAPIException as e:
        # Handle the case where margin mode is already set to ISOLATED
        if e.code == -4046:  # "No need to change margin type."
            logger.info(f"Margin mode is already ISOLATED for {symbol}")
            return True
        else:
            logger.error(f"Binance API Exception setting margin mode to ISOLATED for {symbol}: {e}")
            return False
    except Exception as e:
        logger.error(f"Exception setting margin mode to ISOLATED for {symbol}: {e}")
        return False


# Add a function to periodically check and maintain leverage
def enhanced_leverage_maintenance_task():
    """Check and maintain 2x effective leverage by closing and reopening positions if needed."""
    try:
        # Calculate current effective leverage
        effective_leverage = calculate_effective_leverage(DEFAULT_SYMBOL)
        
        # If no position exists, open one
        if effective_leverage == 0:
            logger.info("No position exists, opening new position with 2x leverage")
            open_new_position_with_2x_leverage()
            return
            
        # If leverage has drifted more than 10% from target (1.9x to 2.1x acceptable range)
        if effective_leverage is not None and (effective_leverage < 1.9 or effective_leverage > 2.1):
            logger.warning(f"Leverage drift detected! Current: {effective_leverage:.2f}x, Target: 2.00x")
            logger.info("Rebalancing position to maintain 2x leverage")
            
            # Store current direction before closing
            positions = client.futures_position_information(symbol=DEFAULT_SYMBOL)
            for position in positions:
                if position['symbol'] == DEFAULT_SYMBOL and float(position['positionAmt']) != 0:
                    current_direction = "BUY" if float(position['positionAmt']) > 0 else "SELL"
                    
                    position_amount = abs(float(position['positionAmt']))
                    mark_price = float(position['markPrice'])
                    
                    # Calculate current position value
                    position_value = position_amount * mark_price
                    
                    # Close all existing positions
                    if close_all_btcusdt_positions():
                        # Calculate new position size at 2x leverage
                        # We need to maintain the same position value
                        position_maintained_successfully = maintain_position_value_with_2x_leverage(
                            direction=current_direction,
                            previous_position_value=position_value
                        )

                        if not position_maintained_successfully:
                            logger.error("Failed to maintain position with 2x leverage")
                    break
        else:
            logger.info(f"Current leverage {effective_leverage:.2f}x is within acceptable range of 2x target")
            
    except Exception as e:
        logger.error(f"Error in enhanced leverage maintenance task: {e}")

def maintain_position_value_with_2x_leverage(direction, previous_position_value):
    """Open a new position maintaining the same position value but with 2x leverage.
    
    Args:
        direction: "BUY" or "SELL" direction
        previous_position_value: The value of the previous position to maintain
    """
    try:
        # Ensure margin mode is ISOLATED
        set_margin_mode_isolated(DEFAULT_SYMBOL)

        # Ensure leverage is set to 2x
        set_and_verify_leverage(DEFAULT_SYMBOL, 2)
        
        # Get current price
        current_price = get_current_price(DEFAULT_SYMBOL)
        
        # Calculate position size in BTC to maintain same position value
        position_size = previous_position_value / current_price
        
        # Round to appropriate precision (usually 3 decimal places for BTC)
        position_size = round(position_size, 3)
        
        logger.info(f"Opening new {direction} position with consistent value: {position_size} {DEFAULT_SYMBOL} at {current_price}")
        
        # Create the new position
        order = client.futures_create_order(
            symbol=DEFAULT_SYMBOL,
            side=direction,
            type="MARKET",
            quantity=position_size
        )
        
        logger.info(f"New position opened with order ID: {order['orderId']}")
        return True
        
    except BinanceAPIException as e:
        logger.error(f"Binance API error opening position: {e}")
        return False
    except Exception as e:
        logger.error(f"Error opening position: {e}")
        return False

# Start a background thread for leverage maintenance
def start_leverage_maintenance():
    """Start the leverage maintenance background task"""
    def run_maintenance():

        # Schedule to check every minute
        schedule.every(1).minutes.do(enhanced_leverage_maintenance_task)
        
        while True:
            schedule.run_pending()
            time.sleep(1)
    
    maintenance_thread = threading.Thread(target=run_maintenance)
    maintenance_thread.daemon = True
    maintenance_thread.start()
    logger.info("Leverage maintenance service started")

def close_all_btcusdt_positions():
    """Close all existing BTCUSDT positions."""
    try:
        # Get current positions
        positions = client.futures_position_information(symbol=DEFAULT_SYMBOL)
        
        for position in positions:
            if position['symbol'] == DEFAULT_SYMBOL and float(position['positionAmt']) != 0:
                logger.info(f"Found existing position: {position['positionAmt']} {DEFAULT_SYMBOL}")
                
                # Determine the close direction (opposite of current position)
                close_side = "SELL" if float(position['positionAmt']) > 0 else "BUY"
                
                # Get absolute position size
                position_size = abs(float(position['positionAmt']))
                
                logger.info(f"Closing position with {close_side} order of {position_size} {DEFAULT_SYMBOL}")
                
                # Execute closing order
                order = client.futures_create_order(
                    symbol=DEFAULT_SYMBOL,
                    side=close_side,
                    type="MARKET",
                    quantity=position_size
                )
                
                logger.info(f"Position closed with order ID: {order['orderId']}")
                return True
                
        logger.info(f"No open {DEFAULT_SYMBOL} positions found to close")
        return False
        
    except BinanceAPIException as e:
        logger.error(f"Binance API error closing positions: {e}")
        return False
    except Exception as e:
        logger.error(f"Error closing positions: {e}")
        return False

def open_new_position_with_2x_leverage(direction="BUY", allocation_percentage=10):
    """Open a new BTCUSDT position with exactly 2x leverage.
    
    Args:
        direction: "BUY" for long position, "SELL" for short position
        allocation_percentage: Percentage of available balance to allocate to this position
    """
    try:
        # Ensure leverage is set to 2x
        set_and_verify_leverage(DEFAULT_SYMBOL, 2)

        # Set margin mode to ISOLATED
        set_margin_mode_isolated(DEFAULT_SYMBOL)

        # Get account information to calculate position size
        account = client.futures_account()
        available_balance = float(account['availableBalance'])
        
        # Calculate allocation amount (e.g., 10% of available balance)
        allocation = available_balance * (allocation_percentage / 100)
        logger.info(f"""
            Available Balance: {available_balance}
            Allocation: {allocation}        
        """)
        
        # Get current price
        current_price = get_current_price(DEFAULT_SYMBOL)
        
        # Calculate position size in BTC
        # For 2x leverage: position_size = (allocation * 2) / price
        position_size = (allocation * 2) / current_price
        
        # Round to appropriate precision (usually 3 decimal places for BTC)
        position_size = round(position_size, 3)
        
        logger.info(f"Opening new {direction} position: {position_size} {DEFAULT_SYMBOL} at {current_price}")
        
        # Create the new position
        order = client.futures_create_order(
            symbol=DEFAULT_SYMBOL,
            side=direction,
            type="MARKET",
            quantity=position_size
        )
        
        logger.info(f"New position opened with order ID: {order['orderId']}")
        return True
        
    except BinanceAPIException as e:
        logger.error(f"Binance API error opening position: {e}")
        return False
    except Exception as e:
        logger.error(f"Error opening position: {e}")
        return False

def calculate_effective_leverage(symbol=DEFAULT_SYMBOL):
    """Calculate the actual effective leverage of the current position."""
    try:
        # Get position information
        positions = client.futures_position_information(symbol=symbol)
                
        for position in positions:
            if position['symbol'] == symbol and float(position['positionAmt']) != 0:
                # Get position details
                position_amount = abs(float(position['positionAmt']))
                mark_price = float(position['markPrice'])

                # Calculate position value
                position_value = position_amount * mark_price

                # Get the margin used for this position (use isolatedWallet for isolated margin)
                if 'isolatedWallet' in position and float(position['isolatedWallet']) > 0:
                    margin_used = float(position['isolatedWallet'])
                else:
                    # If isolatedWallet is not available, calculate based on the leverage setting
                    leverage_setting = int(position['leverage'])
                    margin_used = position_value / leverage_setting
                
                # Calculate effective leverage
                effective_leverage = position_value / margin_used
                logger.info(f"""
                    Position Value: {position_value}
                    Position Amount: {position_amount}
                    Margin Used: {margin_used}
                    Current effective leverage: {effective_leverage:.2f}x
                """)
                return effective_leverage
                
        logger.info(f"No open position found for {symbol}")
        return 0
        
    except Exception as e:
        logger.error(f"Error calculating effective leverage: {e}")
        return None


def bot_loop():
    """Main bot loop that runs in a separate thread."""
    global bot_running, last_check_time
    
    logger.info("Starting trading bot loop")
    
    # Ensure margin mode is ISOLATED
    set_margin_mode_isolated(DEFAULT_SYMBOL)

    # Ensure leverage is set properly at bot start
    set_and_verify_leverage(DEFAULT_SYMBOL, 2)
    
    while bot_running:
        try:
            last_check_time = datetime.now().isoformat()

            # Always verify leverage before trading
            enhanced_leverage_maintenance_task()
            
            # Sleep for 1 minute between checks
            time.sleep(60)
        except Exception as e:
            logger.error(f"Error in bot loop: {e}")
            time.sleep(10)  # Wait before retrying

# WebSocket price streaming functions
async def btcusdt_price_socket():
    """Function to stream BTCUSDT price using WebSockets."""
    global websocket_running, latest_price, latest_price_time, price_history
    
    logger.info("Starting BTCUSDT price WebSocket stream")
    
    while websocket_running:
        try:
            # Create async client
            async_client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
            
            # Initialize socket manager
            bsm = BinanceSocketManager(async_client)
            
            # Start ticker socket for BTCUSDT
            # Using ticker socket as it updates every second
            socket = bsm.symbol_ticker_socket(DEFAULT_SYMBOL)
            
            async with socket as ts:
                while websocket_running:
                    # Receive message from WebSocket
                    msg = await ts.recv()
                    
                    # Extract price information
                    if msg and 'c' in msg:
                        price = float(msg['c'])
                        timestamp = datetime.now().isoformat()
                        
                        # Update latest price
                        latest_price = price
                        latest_price_time = timestamp
                        
                        # Add to price history
                        price_data = PriceData(
                            symbol=DEFAULT_SYMBOL,
                            price=price,
                            timestamp=timestamp
                        )
                        price_history.append(price_data)
                        
                        # Keep only the last 100 price points
                        if len(price_history) > 100:
                            price_history = price_history[-100:]
                        
                        # Wait for 5 seconds before processing the next update
                        # This controls the update frequency
                        await asyncio.sleep(5)
            
            # Close the connection
            await async_client.close_connection()
            
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            # Wait before reconnecting
            await asyncio.sleep(5)
    
    logger.info("BTCUSDT price WebSocket stream stopped")

def start_websocket_in_thread():
    """Start the WebSocket in a separate thread."""
    def run_async_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(btcusdt_price_socket())
    
    websocket_thread = threading.Thread(target=run_async_loop)
    websocket_thread.daemon = True
    websocket_thread.start()
    return websocket_thread

# API Endpoints
@app.get("/")
async def root():
    return {"message": "Welcome to the Binance Testnet Trading Bot API"}

# @app.get("/account", response_model=Dict)
# async def get_account_info():
#     """Get account information from Binance Testnet."""
#     try:
#         return client.futures_account()
#     except BinanceAPIException as e:
#         logger.error(f"Binance API error: {e}")
#         raise HTTPException(status_code=e.status_code, detail=str(e))
#     except Exception as e:
#         logger.error(f"Error getting account info: {e}")
#         raise HTTPException(status_code=500, detail=str(e))

@app.get("/price/{symbol}")
async def get_symbol_price(symbol: str = DEFAULT_SYMBOL):
    """Get current price for a symbol."""
    price = get_current_price(symbol)
    return {"symbol": symbol, "price": price, "timestamp": datetime.now().isoformat()}

@app.get("/bot/start")
async def start_bot(background_tasks: BackgroundTasks):
    """Start the trading bot."""
    global bot_running
    
    if bot_running:
        return {"status": "already_running", "message": "Bot is already running"}
    
    bot_running = True
    
    # Start the bot in a background thread
    bot_thread = threading.Thread(target=bot_loop)
    bot_thread.daemon = True
    bot_thread.start()
    
    return {"status": "started", "message": "Trading bot started successfully"}

@app.get("/bot/stop")
async def stop_bot():
    """Stop the trading bot."""
    global bot_running
    
    if not bot_running:
        return {"status": "not_running", "message": "Bot is not running"}
    
    bot_running = False
    return {"status": "stopped", "message": "Trading bot stopped successfully"}

@app.get("/websocket/start")
async def start_websocket():
    """Start the WebSocket price stream."""
    global websocket_running
    
    if websocket_running:
        return {"status": "already_running", "message": "WebSocket is already running"}
    
    websocket_running = True
    start_websocket_in_thread()
    
    return {"status": "started", "message": "WebSocket price stream started successfully"}

@app.get("/websocket/stop")
async def stop_websocket():
    """Stop the WebSocket price stream."""
    global websocket_running
    
    if not websocket_running:
        return {"status": "not_running", "message": "WebSocket is not running"}
    
    websocket_running = False
    return {"status": "stopping", "message": "WebSocket price stream is stopping"}

@app.get("/websocket/price")
async def get_websocket_price():
    """Get the latest price from the WebSocket stream."""
    if not websocket_running:
        raise HTTPException(status_code=400, detail="WebSocket stream is not running")
    
    if latest_price is None:
        raise HTTPException(status_code=404, detail="No price data available yet")
    
    return {
        "symbol": DEFAULT_SYMBOL,
        "price": latest_price,
        "timestamp": latest_price_time
    }

@app.get("/bot/status", response_model=BotStatus)
async def get_bot_status():
    """Get the current status of the trading bot."""
    return BotStatus(
        is_running=bot_running,
        last_check_time=last_check_time,
        price_history=price_history[-5:] if price_history else [],
        trade_history=trade_history[-5:] if trade_history else [],
        websocket_status=websocket_running,
        latest_price=latest_price,
        latest_price_time=latest_price_time
    )

@app.get("/leverage/status")
async def get_leverage_status(symbol: str = DEFAULT_SYMBOL):
    """Get current leverage settings for the specified symbol."""
    try:
        positions = client.futures_position_information(symbol=symbol)

        for position in positions:
            if position['symbol'] == symbol:
                return position
                
        # If no position found
        return {
            "symbol": symbol,
            "description": "No position found",
            "target_leverage": 2,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting leverage status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Start the WebSocket stream when the application starts
@app.on_event("startup")
async def startup_event():
    global websocket_running
    logger.info("Starting application and initializing 2x leveraged BTCUSDT position")

    # Close any existing positions
    close_all_btcusdt_positions()

    # Set margin mode to ISOLATED
    set_margin_mode_isolated(DEFAULT_SYMBOL)
    logger.info(f"Set {DEFAULT_SYMBOL} with isolated margin on startup")

    # Set leverage for BTCUSDT to 2x
    set_and_verify_leverage(DEFAULT_SYMBOL, 2)
    logger.info(f"Set {DEFAULT_SYMBOL} leverage to 2x on startup")
    
    # Open a new position with 2x leverage
    open_new_position_with_2x_leverage()    
    logger.info(f"Open new position with 2x leverage")
    
    # Start leverage maintenance service
    start_leverage_maintenance()
    logger.info("Enhanced leverage maintenance service started")

    websocket_running = True
    start_websocket_in_thread()
    logger.info("WebSocket price stream started automatically on startup")

# Stop the WebSocket stream when the application shuts down
@app.on_event("shutdown")
async def shutdown_event():
    global websocket_running
    websocket_running = False
    logger.info("WebSocket price stream stopped on shutdown")

# For running the application directly
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

