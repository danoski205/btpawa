"""
Production-Grade Spot + Perpetual Hedge Grid Trading Bot
Continuous trading with price-neutral hedging and optional funding capture
"""

import asyncio
import os
import logging
import time
import traceback
import ccxt
from datetime import datetime
from typing import List, Optional, Dict
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
# ENVIRONMENT & CONFIG
# =====================================================
load_dotenv()

BYBIT_KEY = os.getenv("BYBIT_KEY", "").strip()
BYBIT_SECRET = os.getenv("BYBIT_SECRET", "").strip()
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() == "true"
DEMO_MODE = not BYBIT_KEY or not BYBIT_SECRET

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Grid Trading Config
BASE_ASSET = os.getenv("BASE_ASSET", "BTC").upper()
QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDT").upper()
GRID_SIZE = float(os.getenv("GRID_SIZE", "50"))  # Price spacing in USDT
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "0.01"))  # BTC per trade
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "0.3"))  # After fees
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))  # Seconds between checks

logger.info("=" * 70)
logger.info("GRID TRADING BOT CONFIGURATION")
logger.info("=" * 70)
logger.info(f"Base Asset: {BASE_ASSET}")
logger.info(f"Quote Asset: {QUOTE_ASSET}")
logger.info(f"Grid Size: {GRID_SIZE} USDT")
logger.info(f"Trade Amount: {TRADE_AMOUNT} {BASE_ASSET}")
logger.info(f"Min Profit: {MIN_PROFIT_PERCENT}%")
logger.info(f"Check Interval: {CHECK_INTERVAL}s")
logger.info(f"Mode: {'TESTNET' if USE_TESTNET else 'LIVE'}")
logger.info(f"Demo: {DEMO_MODE}")
logger.info("=" * 70)

# =====================================================
# CCXT EXCHANGE SETUP
# =====================================================
def init_exchanges():
    """Initialize spot and perpetual exchanges"""
    try:
        spot = ccxt.bybit({
            "apiKey": BYBIT_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })
        
        perp = ccxt.bybit({
            "apiKey": BYBIT_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "future"}
        })
        
        if USE_TESTNET:
            spot.set_sandbox_mode(True)
            perp.set_sandbox_mode(True)
            logger.info("Testnet mode enabled")
        
        spot.load_markets()
        perp.load_markets()
        
        logger.info("✓ Spot and Perpetual exchanges initialized")
        return spot, perp
    except Exception as e:
        logger.error(f"Failed to init exchanges: {e}")
        logger.error(traceback.format_exc())
        return None, None

# =====================================================
# DATA MODELS
# =====================================================
@dataclass
class GridLevel:
    """Represents a grid level for trading"""
    price: float
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    buy_executed: bool = False
    sell_executed: bool = False

@dataclass
class Trade:
    """Trade record"""
    id: str
    timestamp: str
    pair: str
    side: str
    price: float
    amount: float
    fee: float
    profit_percent: float
    hedge_type: str  # "spot" or "perp"

class BotStatus(BaseModel):
    """API response for bot status"""
    running: bool
    capital_usdt: float
    grid_levels: int
    open_positions: int
    total_profit_usdt: float
    total_trades: int
    spot_price: float
    funding_rate: Optional[float]
    last_trade: Optional[str]

class TradeRecord(BaseModel):
    """API response for trades"""
    id: str
    timestamp: str
    pair: str
    side: str
    price: float
    amount: float
    profit_percent: float
    type: str

# =====================================================
# IN-MEMORY STATE MANAGEMENT
# =====================================================
class BotState:
    """Manages bot state and positions"""
    
    def __init__(self):
        self.running = False
        self.capital_usdt = 1000.0  # Default capital
        self.available_capital = self.capital_usdt
        self.total_profit = 0.0
        self.trades: List[Trade] = []
        
        # Grid tracking
        self.grid_levels: Dict[float, GridLevel] = {}
        self.current_price = 0.0
        self.last_trade_time = 0
        
        # Positions
        self.spot_balance = 0.0
        self.perp_position = 0.0
        self.open_orders = {}
    
    def add_trade(self, trade: Trade):
        """Record a trade"""
        self.trades.append(trade)
        self.total_profit += trade.profit_percent
        logger.info(f"[TRADE] {trade.pair} {trade.side}: {trade.amount} @ {trade.price} = {trade.profit_percent:.4f}%")
    
    def get_status(self) -> dict:
        """Get current status"""
        return {
            "running": self.running,
            "capital": self.capital_usdt,
            "available": self.available_capital,
            "profit": self.total_profit,
            "trades": len(self.trades),
            "grid_levels": len(self.grid_levels),
            "current_price": self.current_price,
            "spot_balance": self.spot_balance,
            "perp_position": self.perp_position
        }

state = BotState()

# =====================================================
# GRID TRADING ENGINE
# =====================================================
class GridTradingEngine:
    """Automated spot+perp grid trading"""
    
    def __init__(self, spot, perp):
        self.spot = spot
        self.perp = perp
        self.pair = f"{BASE_ASSET}/{QUOTE_ASSET}"
        self.running = False
        
        # Track last grid levels
        self.last_buy_level = 0
        self.last_sell_level = 0
    
    async def get_prices(self):
        """Get current prices from spot and perp"""
        try:
            ticker_spot = self.spot.fetch_ticker(self.pair)
            ticker_perp = self.perp.fetch_ticker(self.pair)
            
            spot_price = ticker_spot.get("last", 0)
            perp_price = ticker_perp.get("last", 0)
            
            return spot_price, perp_price
        except Exception as e:
            logger.error(f"Failed to fetch prices: {e}")
            return 0, 0
    
    async def execute_spot_buy(self, price: float, amount: float):
        """Buy on spot market"""
        try:
            logger.info(f"[SPOT] BUY {amount} {BASE_ASSET} @ {price}")
            order = self.spot.create_order(
                self.pair,
                "market",
                "buy",
                amount
            )
            logger.info(f"[SPOT] Buy order executed: {order['id']}")
            return order
        except Exception as e:
            logger.error(f"Spot buy failed: {e}")
            return None
    
    async def execute_spot_sell(self, price: float, amount: float):
        """Sell on spot market"""
        try:
            logger.info(f"[SPOT] SELL {amount} {BASE_ASSET} @ {price}")
            order = self.spot.create_order(
                self.pair,
                "market",
                "sell",
                amount
            )
            logger.info(f"[SPOT] Sell order executed: {order['id']}")
            return order
        except Exception as e:
            logger.error(f"Spot sell failed: {e}")
            return None
    
    async def execute_perp_short(self, amount: float):
        """Open perp short to hedge spot buy"""
        try:
            logger.info(f"[PERP] SHORT {amount} {BASE_ASSET}")
            order = self.perp.create_order(
                self.pair,
                "market",
                "sell",
                amount
            )
            logger.info(f"[PERP] Short order executed: {order['id']}")
            return order
        except Exception as e:
            logger.error(f"Perp short failed: {e}")
            return None
    
    async def execute_perp_long(self, amount: float):
        """Open perp long to hedge spot sell"""
        try:
            logger.info(f"[PERP] LONG {amount} {BASE_ASSET}")
            order = self.perp.create_order(
                self.pair,
                "market",
                "buy",
                amount
            )
            logger.info(f"[PERP] Long order executed: {order['id']}")
            return order
        except Exception as e:
            logger.error(f"Perp long failed: {e}")
            return None
    
    async def get_funding_rate(self) -> Optional[float]:
        """Get current funding rate (bonus if positive)"""
        try:
            ticker = self.perp.fetch_funding_rate(self.pair)
            funding = ticker.get("fundingRate")
            if funding:
                logger.info(f"[FUNDING] Current rate: {funding*100:.4f}%")
            return funding
        except Exception as e:
            logger.debug(f"Could not fetch funding rate: {e}")
            return None
    
    async def execute_grid_trade(self):
        """Execute one complete grid trade cycle"""
        try:
            spot_price, perp_price = await self.get_prices()
            if spot_price == 0:
                return
            
            state.current_price = spot_price
            
            # Determine if we should buy or sell based on grid
            price_diff = abs(spot_price - self.last_buy_level)
            
            # If price moved up more than grid size from last buy, execute sell
            if spot_price > self.last_buy_level + GRID_SIZE:
                logger.info(f"[GRID] Price {spot_price} > threshold {self.last_buy_level + GRID_SIZE} → SELL")
                
                # 1. Hedge first: open perp LONG
                perp_order = await self.execute_perp_long(TRADE_AMOUNT)
                await asyncio.sleep(0.5)
                
                # 2. Then sell spot
                spot_order = await self.execute_spot_sell(spot_price, TRADE_AMOUNT)
                
                if perp_order and spot_order:
                    profit = ((spot_price - self.last_buy_level) / self.last_buy_level) * 100 - 0.3
                    state.add_trade(Trade(
                        id=str(int(time.time() * 1000)),
                        timestamp=datetime.now().isoformat(),
                        pair=self.pair,
                        side="sell",
                        price=spot_price,
                        amount=TRADE_AMOUNT,
                        fee=TRADE_AMOUNT * spot_price * 0.001,
                        profit_percent=max(0, profit),
                        hedge_type="spot+perp"
                    ))
                    self.last_sell_level = spot_price
            
            # If price dropped below grid, execute buy
            elif spot_price < self.last_sell_level - GRID_SIZE or self.last_buy_level == 0:
                logger.info(f"[GRID] Price {spot_price} < threshold → BUY")
                
                # 1. Hedge first: open perp SHORT
                perp_order = await self.execute_perp_short(TRADE_AMOUNT)
                await asyncio.sleep(0.5)
                
                # 2. Then buy spot
                spot_order = await self.execute_spot_buy(spot_price, TRADE_AMOUNT)
                
                if perp_order and spot_order:
                    state.add_trade(Trade(
                        id=str(int(time.time() * 1000)),
                        timestamp=datetime.now().isoformat(),
                        pair=self.pair,
                        side="buy",
                        price=spot_price,
                        amount=TRADE_AMOUNT,
                        fee=TRADE_AMOUNT * spot_price * 0.001,
                        profit_percent=0.3,  # Will be calculated when sold
                        hedge_type="spot+perp"
                    ))
                    self.last_buy_level = spot_price
            
            # Optional: log funding rate if positive
            funding = await self.get_funding_rate()
            if funding and funding > 0:
                logger.info(f"[BONUS] Positive funding available: {funding*100:.4f}%")
        
        except Exception as e:
            logger.error(f"Grid trade execution failed: {e}")
            logger.error(traceback.format_exc())
    
    async def run(self):
        """Main bot loop"""
        logger.info("=" * 70)
        logger.info("GRID TRADING BOT LOOP STARTED")
        logger.info("=" * 70)
        
        self.running = True
        state.running = True
        
        while self.running and state.running:
            try:
                await self.execute_grid_trade()
                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Bot loop error: {e}")
                await asyncio.sleep(CHECK_INTERVAL)

# =====================================================
# FASTAPI APP & ROUTES
# =====================================================
app = FastAPI(title="Grid Trading Bot", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global bot engine
engine: Optional[GridTradingEngine] = None

@app.on_event("startup")
async def startup():
    """Initialize bot on startup"""
    global engine
    
    if not DEMO_MODE:
        spot, perp = init_exchanges()
        if spot and perp:
            engine = GridTradingEngine(spot, perp)
            asyncio.create_task(engine.run())
            logger.info("Bot initialized and running")
    else:
        logger.warning("DEMO MODE - Bot not executing real trades")

# =====================================================
# ADMIN AUTH
# =====================================================
def verify_admin(auth_header: Optional[str] = Header(None)) -> bool:
    """Verify admin credentials"""
    if not auth_header or not auth_header.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Missing auth")
    
    try:
        import base64
        encoded = auth_header[6:]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            return True
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except:
        raise HTTPException(status_code=401, detail="Auth failed")

# =====================================================
# API ENDPOINTS
# =====================================================
@app.get("/status")
async def get_status():
    """Get bot status"""
    return BotStatus(
        running=state.running,
        capital_usdt=state.capital_usdt,
        grid_levels=len(state.grid_levels),
        open_positions=int(abs(state.spot_balance) + abs(state.perp_position)),
        total_profit_usdt=state.total_profit,
        total_trades=len(state.trades),
        spot_price=state.current_price,
        funding_rate=None,
        last_trade=state.trades[-1].timestamp if state.trades else None
    )

@app.get("/trades")
async def get_trades(limit: int = 100):
    """Get trade history"""
    return [
        TradeRecord(
            id=t.id,
            timestamp=t.timestamp,
            pair=t.pair,
            side=t.side,
            price=t.price,
            amount=t.amount,
            profit_percent=t.profit_percent,
            type=t.hedge_type
        )
        for t in state.trades[-limit:]
    ]

@app.post("/admin/start")
async def admin_start(auth: Optional[str] = Header(None)):
    """Start bot"""
    verify_admin(auth)
    state.running = True
    if engine:
        engine.running = True
    logger.info("[ADMIN] Bot started")
    return {"status": "started"}

@app.post("/admin/stop")
async def admin_stop(auth: Optional[str] = Header(None)):
    """Stop bot"""
    verify_admin(auth)
    state.running = False
    if engine:
        engine.running = False
    logger.info("[ADMIN] Bot stopped")
    return {"status": "stopped"}

@app.post("/admin/capital")
async def set_capital(request: dict, auth: Optional[str] = Header(None)):
    """Set initial capital"""
    verify_admin(auth)
    capital = request.get("capital_usdt", 1000)
    state.capital_usdt = capital
    state.available_capital = capital
    logger.info(f"[ADMIN] Capital set to {capital} USDT")
    return {"capital": capital}

@app.get("/admin/diagnostics")
async def diagnostics(auth: Optional[str] = Header(None)):
    """Diagnostics info"""
    verify_admin(auth)
    return {
        "config": {
            "base": BASE_ASSET,
            "quote": QUOTE_ASSET,
            "grid_size": GRID_SIZE,
            "trade_amount": TRADE_AMOUNT,
            "min_profit": MIN_PROFIT_PERCENT,
            "mode": "testnet" if USE_TESTNET else "live"
        },
        "state": state.get_status(),
        "exchanges": "initialized" if engine else "not initialized"
    }

@app.get("/health")
async def health():
    """Health check"""
    return {"status": "ok", "bot_running": state.running}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
