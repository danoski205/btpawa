"""
Arbitrage Bot Server - Production Ready
========================================
A FastAPI server for automated triangular arbitrage trading on Bybit.

Features:
- CORS enabled for frontend communication
- Async-safe exchange calls using run_in_executor
- Real bid/ask pricing (not ticker last)
- Proper error handling and logging
- Trade history tracking
- Input validation
"""

import asyncio
import os
import logging
import random
import time
import traceback
import requests
from datetime import datetime
from typing import List, Optional, Literal
from concurrent.futures import ThreadPoolExecutor

import ccxt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from dotenv import load_dotenv

# =====================================================
# LOGGING SETUP
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =====================================================
# ENV SETUP
# =====================================================

load_dotenv()

BYBIT_KEY = os.getenv("BYBIT_KEY")
BYBIT_SECRET = os.getenv("BYBIT_SECRET")

# Debug: Log raw USE_TESTNET value
_use_testnet_raw = os.getenv("USE_TESTNET", "False")
USE_TESTNET = _use_testnet_raw.lower() == "true"

# Allow running in demo mode without credentials
DEMO_MODE = not BYBIT_KEY or not BYBIT_SECRET

# =====================================================
# DEBUG LOGGING - CRITICAL FOR TROUBLESHOOTING
# =====================================================
logger.info(f"[DEBUG] USE_TESTNET raw value: {_use_testnet_raw}")
logger.info(f"[DEBUG] USE_TESTNET parsed: {USE_TESTNET}")
logger.info(f"[DEBUG] BYBIT_KEY set: {bool(BYBIT_KEY)}")
logger.info(f"[DEBUG] BYBIT_SECRET set: {bool(BYBIT_SECRET)}")
logger.info(f"[DEBUG] DEMO_MODE: {DEMO_MODE}")

if DEMO_MODE:
    logger.warning("Running in DEMO MODE - No real trades will be executed")
    logger.warning("   Set BYBIT_KEY and BYBIT_SECRET in .env for live trading")
else:
    if USE_TESTNET:
        logger.info("Bybit TESTNET credentials loaded - Testnet trading enabled (sandbox mode)")
    else:
        logger.info("Bybit LIVE credentials loaded - Live trading enabled")

# =====================================================
# APP INIT
# =====================================================

app = FastAPI(
    title="Arbitrage Bot API",
    description="Automated triangular arbitrage trading on Bybit",
    version="2.1.0"
)

# CORS Configuration - Allow frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Thread pool for blocking ccxt operations
executor = ThreadPoolExecutor(max_workers=4)

# =====================================================
# MODELS
# =====================================================

class Step(BaseModel):
    """A single step in the arbitrage path"""
    symbol: str = Field(..., description="Trading pair, e.g., BTC/USDT")
    side: Literal["buy", "sell"] = Field(..., description="Order side")
    
    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        v = v.upper().strip()
        if "/" not in v:
            raise ValueError("Symbol must be in format BASE/QUOTE (e.g., BTC/USDT)")
        return v
    
    @field_validator("side")
    @classmethod
    def validate_side(cls, v: str) -> str:
        return v.lower().strip()


class BotConfig(BaseModel):
    """Bot configuration for an arbitrage session"""
    amount: float = Field(..., gt=0, description="Initial trading amount in quote currency")
    loops: int = Field(..., ge=1, le=1000, description="Number of arbitrage cycles to run")
    min_profit_percent: float = Field(default=0.3, ge=0, le=100, description="Minimum profit % to execute")
    steps: List[Step] = Field(..., min_length=2, max_length=10, description="Arbitrage path steps")


class TradeRecord(BaseModel):
    """Record of an executed trade"""
    timestamp: str
    cycle: int
    step: int
    symbol: str
    side: str
    quantity: float
    price: float
    value: float


class StatusResponse(BaseModel):
    """Bot status response"""
    running: bool
    balance: float
    profit: float
    profit_percent: float
    loops_left: int
    loops_completed: int
    last_error: Optional[str]
    demo_mode: bool
    testnet_mode: bool


# =====================================================
# TRADER ENGINE
# =====================================================

class Trader:
    """Main trading engine for arbitrage execution"""

    def __init__(self):
        # =====================================================
        # EXTENSIVE DEBUG LOGGING ON BOT INITIALIZATION
        # =====================================================
        logger.info("=" * 60)
        logger.info("BOT INITIALIZING")
        logger.info("=" * 60)
        logger.info(f"DEMO_MODE: {DEMO_MODE}")
        logger.info(f"USE_TESTNET: {USE_TESTNET}")
        logger.info(f"BYBIT_KEY exists: {bool(BYBIT_KEY)}")
        logger.info(f"BYBIT_SECRET exists: {bool(BYBIT_SECRET)}")
        
        self.exchange: Optional[ccxt.bybit] = None
        self.running = False
        self.config: Optional[BotConfig] = None
        self.task: Optional[asyncio.Task] = None

        # Tracking
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.total_profit = 0.0
        self.loops_left = 0
        self.loops_completed = 0
        self.last_error: Optional[str] = None
        self.trade_history: List[TradeRecord] = []

        # Bybit spot fee (maker/taker)
        self.fee = 0.001  # 0.1%

        # Initialize exchange if credentials available
        logger.info(f"Checking if should init exchange...")
        logger.info(f"  DEMO_MODE={DEMO_MODE}, will init={not DEMO_MODE}")
        if not DEMO_MODE:
            logger.info("→ Calling _init_exchange() because not in DEMO_MODE")
            self._init_exchange()
        else:
            logger.info("→ Skipping _init_exchange() because DEMO_MODE=True")
            self.exchange = None
        
        logger.info(f"Bot init complete. Exchange object: {self.exchange is not None}")
        logger.info("=" * 60)

    def _init_exchange(self):
        """Initialize the Bybit exchange connection (live or testnet)"""
        logger.info("=" * 60)
        logger.info("_INIT_EXCHANGE STARTING")
        logger.info("=" * 60)
        try:
            # Test Bybit API connectivity first
            logger.info("[STEP 1/5] Testing Bybit API connectivity (public endpoint)...")
            cloudflare_blocking = False
            try:
                # Use a browser-like User-Agent to avoid bot detection
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                logger.info("  Attempting GET https://api.bybit.com/v5/market/time...")
                response = requests.get("https://api.bybit.com/v5/market/time", 
                                       timeout=5, 
                                       headers=headers)
                logger.info(f"  ✓ API Response status: {response.status_code}")
                
                if response.status_code == 403:
                    cloudflare_blocking = True
                    logger.error("  ✗✗✗ 403 CLOUDFLARE BLOCKING DETECTED")
                    logger.error("  Render IP is blocked by Bybit's Cloudflare WAF")
                    logger.error("  Response contains: " + response.text[:200] if response.text else "No response body")
                    logger.error("  ccxt will face the same 403 error on load_markets()")
                elif response.status_code != 200:
                    logger.warning(f"  ⚠ Bybit API returned {response.status_code} - may indicate other issues")
                    logger.warning(f"  Response: {response.text[:200]}")
            except requests.exceptions.Timeout:
                logger.error("  ✗ TIMEOUT reaching Bybit API - server not responding")
            except requests.exceptions.ConnectionError:
                logger.error("  ✗ CONNECTION ERROR - cannot reach Bybit API (network issue)")
            except Exception as api_test_error:
                logger.error(f"  ✗ API test failed: {api_test_error}")
            
            # Initialize ccxt exchange object
            logger.info("[STEP 2/5] Creating ccxt.bybit object...")
            logger.info(f"  USE_TESTNET={USE_TESTNET}")
            logger.info(f"  BYBIT_KEY={BYBIT_KEY[:8]}...***" if BYBIT_KEY else "  BYBIT_KEY=NOT SET")
            logger.info(f"  BYBIT_SECRET={'*' * 10}***" if BYBIT_SECRET else "  BYBIT_SECRET=NOT SET")
            
            try:
                self.exchange = ccxt.bybit({
                    "apiKey": BYBIT_KEY,
                    "secret": BYBIT_SECRET,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"}
                })
                logger.info("  ✓ ccxt.bybit object created successfully")
            except Exception as e:
                logger.error(f"  ✗ FAILED to create ccxt.bybit: {e}")
                logger.error(f"  Full error: {traceback.format_exc()}")
                raise

            # Set sandbox mode if testnet
            logger.info("[STEP 3/5] Setting sandbox mode...")
            if USE_TESTNET:
                logger.info("  USE_TESTNET=True, calling set_sandbox_mode(True)...")
                try:
                    self.exchange.set_sandbox_mode(True)
                    logger.info("  ✓ Sandbox mode enabled")
                except Exception as e:
                    logger.error(f"  ✗ FAILED to set sandbox mode: {e}")
                    raise
            else:
                logger.info("  USE_TESTNET=False, using LIVE endpoints")

            # Load markets with retry (longer delays for Cloudflare rate limiting)
            logger.info("[STEP 4/5] Loading markets (5 retries with exponential backoff)...")
            market_load_success = False
            for attempt in range(5):
                try:
                    logger.info(f"  Attempt {attempt + 1}/5...")
                    self.exchange.load_markets()
                    logger.info(f"  ✓ load_markets() succeeded on attempt {attempt + 1}")
                    market_load_success = True
                    break
                except Exception as e:
                    error_str = str(e)
                    logger.warning(f"  ✗ Attempt {attempt + 1} failed: {type(e).__name__}: {e}")
                    
                    # Check if it's a 403 Cloudflare error
                    if "403" in error_str or "Cloudflare" in error_str or "blocked" in error_str.lower():
                        logger.error(f"  ✗✗✗ 403 CLOUDFLARE BLOCK DETECTED - Render IP is blocked")
                        logger.error(f"  Error details: {error_str}")
                    
                    logger.warning(f"  Traceback: {traceback.format_exc()}")
                    
                    if attempt < 4:
                        # Exponential backoff: 2s, 4s, 8s, 16s
                        wait_time = 2 ** (attempt + 1)
                        logger.info(f"  Waiting {wait_time} seconds before retry (exponential backoff)...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"  ✗ ALL 5 ATTEMPTS FAILED")
            
            if not market_load_success:
                logger.error("=" * 60)
                logger.error("CLOUDFLARE 403 BLOCKING DETECTED")
                logger.error("=" * 60)
                logger.error("Your Render IP is being blocked by Bybit's Cloudflare WAF")
                logger.error("")
                logger.error("SOLUTIONS:")
                logger.error("1. RECOMMENDED: Switch to LIVE API keys (if you have them)")
                logger.error("   - Bybit's Cloudflare sometimes blocks testnet access")
                logger.error("   - LIVE API access has better IP whitelisting")
                logger.error("   - Set USE_TESTNET=False and use real API keys")
                logger.error("")
                logger.error("2. Wait 5-10 minutes and redeploy")
                logger.error("   - The IP block may be temporary")
                logger.error("")
                logger.error("3. Use VPN/Proxy")
                logger.error("   - If using locally, try a different network")
                logger.error("=" * 60)
                raise Exception("Failed to load markets after 5 attempts - Cloudflare 403 blocking detected")

            # Final verification
            logger.info("[STEP 5/5] Final verification...")
            if self.exchange is None:
                raise Exception("Exchange object is None after initialization")
            logger.info(f"  ✓ Exchange object exists: {type(self.exchange)}")
            
            if USE_TESTNET:
                logger.info("✓✓✓ Connected to Bybit TESTNET (sandbox mode)")
            else:
                logger.info("✓✓✓ Connected to Bybit LIVE account")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error("=" * 60)
            logger.error("✗✗✗ EXCHANGE INITIALIZATION FAILED")
            logger.error("=" * 60)
            logger.error(f"Exception type: {type(e).__name__}")
            logger.error(f"Exception message: {e}")
            logger.error(f"Full traceback:\n{traceback.format_exc()}")
            self.exchange = None
            logger.error("=" * 60)

    # -------------------------
    # CONFIG
    # -------------------------

    def set_config(self, config: BotConfig):
        """Set the bot configuration"""
        self.config = config
        self.initial_balance = config.amount
        self.current_balance = config.amount
        self.loops_left = config.loops
        self.loops_completed = 0
        self.total_profit = 0.0
        self.last_error = None
        self.trade_history = []
        logger.info(f"Config set: amount={config.amount}, loops={config.loops}, steps={len(config.steps)}")

    # -------------------------
    # STATUS
    # -------------------------

    def status(self) -> dict:
        """Get current bot status"""
        profit_percent = 0.0
        if self.initial_balance > 0:
            profit_percent = (self.total_profit / self.initial_balance) * 100

        return {
            "running": self.running,
            "balance": round(self.current_balance, 8),
            "profit": round(self.total_profit, 8),
            "profit_percent": round(profit_percent, 4),
            "loops_left": self.loops_left,
            "loops_completed": self.loops_completed,
            "last_error": self.last_error,
            "demo_mode": DEMO_MODE,
            "testnet_mode": USE_TESTNET
        }

    # -------------------------
    # CONTROL
    # -------------------------

    def stop(self):
        """Stop the bot gracefully"""
        self.running = False
        logger.info("Stop signal received")

    def reset(self):
        """Reset the bot state"""
        self.running = False
        self.current_balance = self.initial_balance
        self.total_profit = 0.0
        self.loops_left = self.config.loops if self.config else 0
        self.loops_completed = 0
        self.last_error = None
        self.trade_history = []
        logger.info("Bot state reset")

    # -------------------------
    # ASYNC EXCHANGE CALLS
    # -------------------------

    async def _fetch_ticker(self, symbol: str) -> dict:
        """Fetch ticker data asynchronously (non-blocking)"""
        loop = asyncio.get_event_loop()
        
        logger.info(f"[_fetch_ticker] symbol={symbol}, DEMO_MODE={DEMO_MODE}, exchange_exists={self.exchange is not None}")
        
        if DEMO_MODE:
            # Demo mode: return simulated prices with bid/ask
            logger.info(f"[_fetch_ticker] Using DEMO ticker for {symbol}")
            return await self._get_demo_ticker(symbol)
        
        if self.exchange is None:
            logger.error(f"[_fetch_ticker] CRITICAL: self.exchange is None!")
            logger.error(f"[_fetch_ticker] This means _init_exchange() was never called or failed")
            logger.error(f"[_fetch_ticker] DEMO_MODE={DEMO_MODE}, USE_TESTNET={USE_TESTNET}")
            raise Exception("Bybit exchange not initialized. Cannot fetch ticker. Check your API credentials and internet connection.")
        
        logger.info(f"[_fetch_ticker] Fetching real ticker from exchange for {symbol}")
        return await loop.run_in_executor(
            executor,
            self.exchange.fetch_ticker,
            symbol
        )

    async def _get_trade_price(self, symbol: str, side: str) -> float:
        """
        Returns the REAL executable price:
        - buy  -> ask (what sellers want)
        - sell -> bid (what buyers offer)
        
        This is critical for accurate arbitrage calculations.
        Using ticker["last"] causes fake profits due to spread blindness.
        """
        ticker = await self._fetch_ticker(symbol)
        
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        
        # Validate liquidity exists
        if bid is None or ask is None:
            raise Exception(f"No bid/ask liquidity for {symbol}")
        
        # Log the spread for debugging
        spread_percent = ((ask - bid) / bid) * 100
        logger.debug(f"{symbol} spread: {spread_percent:.4f}% (bid={bid}, ask={ask})")
        
        return ask if side == "buy" else bid

    async def _create_order(self, symbol: str, side: str, amount: float) -> dict:
        """Create market order asynchronously (non-blocking)"""
        loop = asyncio.get_event_loop()
        
        if DEMO_MODE:
            # Demo mode: simulate order with real bid/ask
            return await self._simulate_order(symbol, side, amount)
        
        return await loop.run_in_executor(
            executor,
            lambda: self.exchange.create_market_order(symbol, side, amount)
        )

    async def _get_demo_ticker(self, symbol: str) -> dict:
        """
        Generate demo ticker prices with realistic bid/ask spread.
        Simulates a 0.1% spread to match real market conditions.
        """
        # Base prices for demo mode
        demo_prices = {
            "BTC/USDT": 42000.0,
            "ETH/USDT": 2200.0,
            "ETH/BTC": 0.0524,
            "BNB/USDT": 320.0,
            "BNB/BTC": 0.0076,
            "XRP/USDT": 0.62,
            "SOL/USDT": 98.0,
            "DOGE/USDT": 0.082,
            "ADA/USDT": 0.45,
            "AVAX/USDT": 35.0,
            "MATIC/USDT": 0.85,
            "DOT/USDT": 7.20,
            "LINK/USDT": 14.50,
            "UNI/USDT": 6.80,
            "ATOM/USDT": 9.50,
        }
        
        mid_price = demo_prices.get(symbol.upper(), 100.0)
        
        # Add small random variance for realism (0.05% max)
        variance = mid_price * random.uniform(-0.0005, 0.0005)
        mid_price += variance
        
        # Simulate realistic spread (0.1% total, split between bid and ask)
        spread = mid_price * 0.001
        
        return {
            "symbol": symbol,
            "last": mid_price,
            "bid": mid_price - (spread / 2),   # What buyers are willing to pay
            "ask": mid_price + (spread / 2),   # What sellers want
            "high": mid_price * 1.02,
            "low": mid_price * 0.98,
            "volume": random.uniform(1000, 50000)
        }

    async def _simulate_order(self, symbol: str, side: str, amount: float) -> dict:
        """
        Simulate an order in demo mode using real bid/ask prices.
        This ensures demo mode accurately reflects live trading costs.
        """
        # Use the real executable price (bid for sell, ask for buy)
        price = await self._get_trade_price(symbol, side)
        
        if side == "buy":
            cost = amount * price
            filled = amount
        else:
            cost = amount * price
            filled = amount
        
        return {
            "id": f"demo_{datetime.now().timestamp()}",
            "symbol": symbol,
            "side": side,
            "amount": filled,
            "price": price,
            "cost": cost,
            "status": "closed",
            "type": "market"
        }

    # -------------------------
    # SIMULATION
    # -------------------------

    async def simulate_cycle(self, amount: float) -> float:
        """
        Simulate the arbitrage cycle without executing real trades.
        Uses real bid/ask to simulate accurate prices.
        Returns ending balance in quote currency (USDT).
        """
        logger.info(f"[simulate_cycle] Starting simulation with amount={amount}")
        balance = amount
        
        for step in self.config.steps:
            try:
                logger.info(f"[simulate_cycle] Step: {step.symbol} {step.side}")
                # Use real executable price (ask for buy, bid for sell)
                price = await self._get_trade_price(step.symbol, step.side)
                logger.info(f"[simulate_cycle]   Price: {price}")
                
                if step.side == "buy":
                    # Buying: spend quote currency, receive base currency
                    balance = (balance / price) * (1 - self.fee)
                    logger.info(f"[simulate_cycle]   Bought, new balance: {balance}")
                else:
                    # Selling: spend base currency, receive quote currency
                    balance = (balance * price) * (1 - self.fee)
                    logger.info(f"[simulate_cycle]   Sold, new balance: {balance}")
                    
            except Exception as e:
                logger.error(f"[simulate_cycle] ERROR for {step.symbol}: {e}")
                logger.error(f"[simulate_cycle] Traceback: {traceback.format_exc()}")
                raise

        logger.info(f"[simulate_cycle] Simulation complete, final balance: {balance}")
        return balance

    # -------------------------
    # EXECUTION
    # -------------------------

    async def execute_cycle(self, cycle_num: int, amount: float) -> float:
        """
        Execute a full arbitrage cycle.
        Uses real bid/ask prices for accurate execution.
        Returns the ending balance.
        """
        balance = amount

        for step_idx, step in enumerate(self.config.steps):
            try:
                # Use real executable price (ask for buy, bid for sell)
                price = await self._get_trade_price(step.symbol, step.side)

                # Calculate quantity based on side
                if step.side == "buy":
                    # When buying, we spend 'balance' (quote currency) to get base currency
                    qty = balance / price
                else:
                    # When selling, we sell 'balance' (base currency) for quote currency
                    qty = balance

                # Execute the order
                order = await self._create_order(step.symbol, step.side, qty)
                
                # Calculate new balance after trade + fees
                if step.side == "buy":
                    # After buying, we have qty of base currency (minus fees)
                    balance = qty * (1 - self.fee)
                else:
                    # After selling, we have qty * price of quote currency (minus fees)
                    balance = (qty * price) * (1 - self.fee)

                # Record the trade
                trade = TradeRecord(
                    timestamp=datetime.now().isoformat(),
                    cycle=cycle_num,
                    step=step_idx + 1,
                    symbol=step.symbol,
                    side=step.side,
                    quantity=round(qty, 8),
                    price=round(price, 8),
                    value=round(qty * price, 8)
                )
                self.trade_history.append(trade)
                
                logger.info(f"  Step {step_idx + 1}: {step.side.upper()} {step.symbol} | qty={qty:.8f} @ {price:.8f}")

                # Small delay between orders for rate limiting
                await asyncio.sleep(0.2)

            except Exception as e:
                self.last_error = f"Trade failed: {step.symbol} {step.side} - {str(e)}"
                logger.error(f"Trade error: {self.last_error}")
                raise

        return balance

    # -------------------------
    # MAIN LOOP
    # -------------------------

    async def run(self):
        """Main bot execution loop"""
        if not self.config:
            logger.error("No configuration set")
            return

        logger.info("=" * 60)
        logger.info("BOT EXECUTION STARTING")
        logger.info("=" * 60)
        logger.info(f"Mode: {'DEMO' if DEMO_MODE else ('TESTNET' if USE_TESTNET else 'LIVE')}")
        logger.info(f"Initial balance: {self.current_balance}")
        logger.info(f"Loops to run: {self.loops_left}")
        logger.info(f"Min profit: {self.config.min_profit_percent}%")
        logger.info(f"Exchange initialized: {self.exchange is not None}")
        logger.info("=" * 60)
        
        self.running = True

        while self.running and self.loops_left > 0:
            cycle_num = self.loops_completed + 1
            
            try:
                logger.info(f"Cycle {cycle_num} | Balance: {self.current_balance:.8f}")

                # First, simulate to check profitability (uses bid/ask)
                simulated = await self.simulate_cycle(self.current_balance)
                
                profit_percent = ((simulated - self.current_balance) / self.current_balance) * 100
                
                logger.info(f"   Simulated result: {simulated:.8f} ({profit_percent:+.4f}%)")

                # Check if profitable enough
                if profit_percent < self.config.min_profit_percent:
                    logger.info(f"Profit {profit_percent:.4f}% below threshold {self.config.min_profit_percent}%. Waiting...")
                    await asyncio.sleep(1)  # Wait and retry
                    continue

                # Execute the cycle (uses bid/ask)
                new_balance = await self.execute_cycle(cycle_num, self.current_balance)

                # Update tracking
                cycle_profit = new_balance - self.current_balance
                self.total_profit += cycle_profit
                self.current_balance = new_balance
                self.loops_left -= 1
                self.loops_completed += 1

                logger.info(f"Cycle {cycle_num} complete | Profit: {cycle_profit:+.8f} | Total: {self.total_profit:+.8f}")

                # Small delay between cycles
                await asyncio.sleep(0.5)

            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Cycle {cycle_num} failed: {e}")
                # Continue to next cycle after error
                await asyncio.sleep(1)

        self.running = False
        logger.info("Bot stopped")
        logger.info(f"   Final balance: {self.current_balance:.8f}")
        logger.info(f"   Total profit: {self.total_profit:+.8f}")
        logger.info(f"   Cycles completed: {self.loops_completed}")


# =====================================================
# INSTANCE
# =====================================================

bot = Trader()


# =====================================================
# API ROUTES
# =====================================================

@app.get("/", tags=["Health"])
def health():
    """Health check endpoint"""
    return {
        "status": "online",
        "message": "Arbitrage bot running",
        "demo_mode": DEMO_MODE,
        "version": "2.1.0"
    }


@app.post("/config", tags=["Control"])
def set_config(config: BotConfig):
    """Set bot configuration"""
    if bot.running:
        raise HTTPException(400, "Cannot change config while bot is running")
    
    bot.set_config(config)
    return {"message": "Configuration saved", "config": config.model_dump()}


@app.post("/start", tags=["Control"])
async def start():
    """Start the arbitrage bot"""
    if bot.running:
        raise HTTPException(400, "Bot is already running")
    
    if not bot.config:
        raise HTTPException(400, "No configuration set. Call /config first")
    
    bot.task = asyncio.create_task(bot.run())
    return {"message": "Bot started", "demo_mode": DEMO_MODE}


@app.post("/stop", tags=["Control"])
def stop():
    """Stop the arbitrage bot"""
    if not bot.running:
        raise HTTPException(400, "Bot is not running")
    
    bot.stop()
    return {"message": "Stop signal sent"}


@app.post("/reset", tags=["Control"])
def reset():
    """Reset bot state to initial configuration"""
    if bot.running:
        raise HTTPException(400, "Cannot reset while bot is running")
    
    bot.reset()
    return {"message": "Bot state reset"}


@app.get("/status", tags=["Status"], response_model=StatusResponse)
def status():
    """Get current bot status"""
    return bot.status()


@app.get("/trades", tags=["Status"])
def get_trades(limit: int = 50):
    """Get recent trade history"""
    trades = bot.trade_history[-limit:] if bot.trade_history else []
    return {
        "total": len(bot.trade_history),
        "trades": [t.model_dump() for t in trades]
    }


@app.get("/prices", tags=["Market"])
async def get_prices(symbols: str = "BTC/USDT,ETH/USDT,ETH/BTC"):
    """Get current bid/ask prices for symbols (comma-separated)"""
    symbol_list = [s.strip().upper() for s in symbols.split(",")]
    prices = {}
    
    for symbol in symbol_list:
        try:
            ticker = await bot._fetch_ticker(symbol)
            prices[symbol] = {
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
                "last": ticker.get("last"),
                "spread_percent": round(
                    ((ticker.get("ask", 0) - ticker.get("bid", 0)) / ticker.get("bid", 1)) * 100, 
                    4
                ) if ticker.get("bid") else None
            }
        except Exception as e:
            prices[symbol] = {"error": str(e)}
            logger.warning(f"Failed to fetch price for {symbol}: {e}")
    
    return {"prices": prices, "demo_mode": DEMO_MODE}


# =====================================================
# STARTUP / SHUTDOWN
# =====================================================

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 50)
    logger.info("Arbitrage Bot Server Starting")
    logger.info(f"   Demo Mode: {DEMO_MODE}")
    logger.info(f"   Version: 2.1.0 (bid/ask pricing)")
    logger.info("=" * 50)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Server shutting down...")
    bot.stop()
    executor.shutdown(wait=True)
