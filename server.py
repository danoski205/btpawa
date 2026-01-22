"""
Arbitrage Bot Server - Production Ready
========================================
A FastAPI server for automated triangular arbitrage trading on Bybit.

Features:
- CORS enabled for frontend communication
- Async-safe exchange calls using run_in_executor
- Proper error handling and logging
- Trade history tracking
- Input validation
"""

import asyncio
import os
import logging
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

# Allow running in demo mode without credentials
DEMO_MODE = not BYBIT_KEY or not BYBIT_SECRET

if DEMO_MODE:
    logger.warning("⚠️  Running in DEMO MODE - No real trades will be executed")
    logger.warning("   Set BYBIT_KEY and BYBIT_SECRET in .env for live trading")
else:
    logger.info("✅ Bybit credentials loaded - Live trading enabled")

# =====================================================
# APP INIT
# =====================================================

app = FastAPI(
    title="Arbitrage Bot API",
    description="Automated triangular arbitrage trading on Bybit",
    version="2.0.0"
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


# =====================================================
# TRADER ENGINE
# =====================================================

class Trader:
    """Main trading engine for arbitrage execution"""

    def __init__(self):
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
        if not DEMO_MODE:
            self._init_exchange()

    def _init_exchange(self):
        """Initialize the Bybit exchange connection"""
        try:
            self.exchange = ccxt.bybit({
                "apiKey": BYBIT_KEY,
                "secret": BYBIT_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"}
            })
            # Test connection
            self.exchange.load_markets()
            logger.info("✅ Exchange connection established")
        except Exception as e:
            logger.error(f"❌ Failed to connect to exchange: {e}")
            self.exchange = None

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
        logger.info(f"📋 Config set: amount={config.amount}, loops={config.loops}, steps={len(config.steps)}")

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
            "demo_mode": DEMO_MODE
        }

    # -------------------------
    # CONTROL
    # -------------------------

    def stop(self):
        """Stop the bot gracefully"""
        self.running = False
        logger.info("🛑 Stop signal received")

    def reset(self):
        """Reset the bot state"""
        self.running = False
        self.current_balance = self.initial_balance
        self.total_profit = 0.0
        self.loops_left = self.config.loops if self.config else 0
        self.loops_completed = 0
        self.last_error = None
        self.trade_history = []
        logger.info("🔄 Bot state reset")

    # -------------------------
    # ASYNC EXCHANGE CALLS
    # -------------------------

    async def _fetch_ticker(self, symbol: str) -> dict:
        """Fetch ticker data asynchronously (non-blocking)"""
        loop = asyncio.get_event_loop()
        
        if DEMO_MODE:
            # Demo mode: return simulated prices
            return await self._get_demo_ticker(symbol)
        
        return await loop.run_in_executor(
            executor,
            self.exchange.fetch_ticker,
            symbol
        )

    async def _create_order(self, symbol: str, side: str, amount: float) -> dict:
        """Create market order asynchronously (non-blocking)"""
        loop = asyncio.get_event_loop()
        
        if DEMO_MODE:
            # Demo mode: simulate order
            return await self._simulate_order(symbol, side, amount)
        
        return await loop.run_in_executor(
            executor,
            lambda: self.exchange.create_market_order(symbol, side, amount)
        )

    async def _get_demo_ticker(self, symbol: str) -> dict:
        """Generate demo ticker prices"""
        # Simulated prices for demo mode
        demo_prices = {
            "BTC/USDT": 42000.0,
            "ETH/USDT": 2200.0,
            "ETH/BTC": 0.0524,
            "BNB/USDT": 320.0,
            "BNB/BTC": 0.0076,
            "XRP/USDT": 0.62,
            "SOL/USDT": 98.0,
            "DOGE/USDT": 0.082,
        }
        
        price = demo_prices.get(symbol.upper(), 100.0)
        # Add small random variance for realism
        import random
        variance = price * random.uniform(-0.001, 0.001)
        
        return {"last": price + variance, "symbol": symbol}

    async def _simulate_order(self, symbol: str, side: str, amount: float) -> dict:
        """Simulate an order in demo mode"""
        ticker = await self._get_demo_ticker(symbol)
        price = ticker["last"]
        
        return {
            "id": f"demo_{datetime.now().timestamp()}",
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "cost": amount * price if side == "sell" else amount / price,
            "status": "closed"
        }

    # -------------------------
    # SIMULATION
    # -------------------------

    async def simulate_cycle(self, amount: float) -> float:
        """
        Simulate an arbitrage cycle to check profitability.
        Returns the expected ending balance.
        """
        balance = amount
        
        for step in self.config.steps:
            try:
                ticker = await self._fetch_ticker(step.symbol)
                price = ticker["last"]
                
                if step.side == "buy":
                    # Buying: spend quote currency, receive base currency
                    balance = (balance / price) * (1 - self.fee)
                else:
                    # Selling: spend base currency, receive quote currency
                    balance = (balance * price) * (1 - self.fee)
                    
            except Exception as e:
                logger.error(f"Simulation error for {step.symbol}: {e}")
                raise

        return balance

    # -------------------------
    # EXECUTION
    # -------------------------

    async def execute_cycle(self, cycle_num: int, amount: float) -> float:
        """
        Execute a full arbitrage cycle.
        Returns the ending balance.
        """
        balance = amount

        for step_idx, step in enumerate(self.config.steps):
            try:
                ticker = await self._fetch_ticker(step.symbol)
                price = ticker["last"]

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
                logger.error(f"❌ {self.last_error}")
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

        logger.info("🚀 Bot starting...")
        logger.info(f"   Initial balance: {self.current_balance}")
        logger.info(f"   Loops to run: {self.loops_left}")
        logger.info(f"   Min profit: {self.config.min_profit_percent}%")
        
        self.running = True

        while self.running and self.loops_left > 0:
            cycle_num = self.loops_completed + 1
            
            try:
                logger.info(f"📊 Cycle {cycle_num} | Balance: {self.current_balance:.8f}")

                # First, simulate to check profitability
                simulated = await self.simulate_cycle(self.current_balance)
                
                profit_percent = ((simulated - self.current_balance) / self.current_balance) * 100
                
                logger.info(f"   Simulated result: {simulated:.8f} ({profit_percent:+.4f}%)")

                # Check if profitable enough
                if profit_percent < self.config.min_profit_percent:
                    logger.info(f"⏸️  Profit {profit_percent:.4f}% below threshold {self.config.min_profit_percent}%. Waiting...")
                    await asyncio.sleep(1)  # Wait and retry
                    continue

                # Execute the cycle
                new_balance = await self.execute_cycle(cycle_num, self.current_balance)

                # Update tracking
                cycle_profit = new_balance - self.current_balance
                self.total_profit += cycle_profit
                self.current_balance = new_balance
                self.loops_left -= 1
                self.loops_completed += 1

                logger.info(f"✅ Cycle {cycle_num} complete | Profit: {cycle_profit:+.8f} | Total: {self.total_profit:+.8f}")

                # Small delay between cycles
                await asyncio.sleep(0.5)

            except Exception as e:
                self.last_error = str(e)
                logger.error(f"❌ Cycle {cycle_num} failed: {e}")
                # Continue to next cycle after error
                await asyncio.sleep(1)

        self.running = False
        logger.info("🏁 Bot stopped")
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
        "version": "2.0.0"
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
    """Get current prices for symbols (comma-separated)"""
    symbol_list = [s.strip().upper() for s in symbols.split(",")]
    prices = {}
    
    for symbol in symbol_list:
        try:
            ticker = await bot._fetch_ticker(symbol)
            prices[symbol] = ticker["last"]
        except Exception as e:
            prices[symbol] = None
            logger.warning(f"Failed to fetch price for {symbol}: {e}")
    
    return {"prices": prices, "demo_mode": DEMO_MODE}


# =====================================================
# STARTUP / SHUTDOWN
# =====================================================

@app.on_event("startup")
async def startup_event():
    logger.info("=" * 50)
    logger.info("🤖 Arbitrage Bot Server Starting")
    logger.info(f"   Demo Mode: {DEMO_MODE}")
    logger.info("=" * 50)


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("👋 Server shutting down...")
    bot.stop()
    executor.shutdown(wait=True)
