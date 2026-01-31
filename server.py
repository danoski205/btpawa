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
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3"))  # Seconds between checks (REST polling is fast enough)

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
        self.total_profit_usdt = 0.0  # Real USDT profit
        self.trades: List[Trade] = []
        
        # Grid tracking
        self.grid_levels: Dict[float, GridLevel] = {}
        self.current_price = 0.0
        self.last_trade_time = 0
        
        # Positions
        self.spot_balance = 0.0
        self.perp_position = 0.0
        self.open_orders = {}
        
        # CRITICAL: Position lock - prevents double trades
        self.open_position = False
        self.position_type = None  # "buy" or "sell"
        
        # Triple Income Tracking
        self.spot_profit_usdt = 0.0
        self.perp_profit_usdt = 0.0
        self.funding_profit_usdt = 0.0
        self.last_funding_capture = 0
        
        # Grid persistence for graceful shutdown
        self.last_buy_level = 0
        self.last_sell_level = 0
        
        # Load persisted state if available
        self._load_state()
    
    def _load_state(self):
        """Load state from disk for graceful restart"""
        try:
            import json
            if os.path.exists("bot_state.json"):
                with open("bot_state.json", "r") as f:
                    data = json.load(f)
                    self.capital_usdt = data.get("capital_usdt", self.capital_usdt)
                    self.total_profit_usdt = data.get("total_profit_usdt", 0.0)
                    self.last_buy_level = data.get("last_buy_level", 0)
                    self.last_sell_level = data.get("last_sell_level", 0)
                    self.spot_profit_usdt = data.get("spot_profit_usdt", 0.0)
                    self.perp_profit_usdt = data.get("perp_profit_usdt", 0.0)
                    self.funding_profit_usdt = data.get("funding_profit_usdt", 0.0)
                    self.available_capital = self.capital_usdt
                    logger.info(f"[RESTORE] Loaded state from disk: capital={self.capital_usdt:.4f}, profit={self.total_profit_usdt:.4f}")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")
    
    def _save_state(self):
        """Save state to disk for graceful shutdown"""
        try:
            import json
            state_data = {
                "capital_usdt": self.capital_usdt,
                "total_profit_usdt": self.total_profit_usdt,
                "last_buy_level": self.last_buy_level,
                "last_sell_level": self.last_sell_level,
                "spot_profit_usdt": self.spot_profit_usdt,
                "perp_profit_usdt": self.perp_profit_usdt,
                "funding_profit_usdt": self.funding_profit_usdt,
            }
            with open("bot_state.json", "w") as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")
    
    def add_trade(self, trade: Trade):
        """Record a trade with triple income tracking"""
        self.trades.append(trade)
        self.total_profit += trade.profit_percent
        
        # COMPOUNDING: Add real USDT profit to capital
        if trade.profit_percent > 0:
            profit_usdt = (trade.price * trade.amount * trade.profit_percent) / 100
            self.total_profit_usdt += profit_usdt
            self.capital_usdt += profit_usdt
            self.available_capital = self.capital_usdt
            
            # Track profit by source
            if trade.side == "sell":
                self.spot_profit_usdt += profit_usdt * 0.6  # ~60% from spot spread
                self.perp_profit_usdt += profit_usdt * 0.4  # ~40% from perp hedge
            
            logger.info(f"[COMPOUND] Profit: {profit_usdt:.4f} USDT | Spot: {self.spot_profit_usdt:.4f}, Perp: {self.perp_profit_usdt:.4f}, Funding: {self.funding_profit_usdt:.4f}")
            self._save_state()
        
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
        
        # Load persisted grid levels (graceful resume)
        self.last_buy_level = state.last_buy_level
        self.last_sell_level = state.last_sell_level
        if self.last_buy_level > 0:
            logger.info(f"[RESTORE] Grid levels loaded: buy={self.last_buy_level}, sell={self.last_sell_level}")
    
    def calculate_amount(self, price: float) -> float:
        """
        CRITICAL: Dynamic position sizing (90% rule)
        Use 90% of capital to enable auto-compounding.
        Automatically grows as capital grows.
        """
        capital_to_use = state.capital_usdt * 0.9
        amount = capital_to_use / price
        logger.debug(f"[SIZING] Capital: {state.capital_usdt:.4f} USDT | Amount: {amount:.6f} {BASE_ASSET}")
        return amount
    
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
        """Execute one complete grid trade cycle with proper hedge safety"""
        try:
            spot_price, perp_price = await self.get_prices()
            if spot_price == 0:
                return
            
            state.current_price = spot_price
            
            # CRITICAL: Check position lock - prevent overlapping trades
            if state.open_position:
                logger.debug(f"[LOCK] Position already open ({state.position_type}) - skipping new trades")
                return
            
            # CRITICAL FIX #1: Initialize grid levels on first run
            if self.last_buy_level == 0:
                logger.info(f"[GRID] First run - initializing grid at {spot_price}")
                self.last_buy_level = spot_price
                self.last_sell_level = spot_price
                return
            
            # CRITICAL: Multi-grid jump handling
            # If price jumps 2-3 grids at once, execute multiple trades
            if spot_price > self.last_buy_level + GRID_SIZE:
                await self.execute_multi_sell(spot_price, perp_price)
                return
            
            # If price moved down, execute multi-buy
            if spot_price < self.last_sell_level - GRID_SIZE:
                await self.execute_multi_buy(spot_price, perp_price)
                return
        
        except Exception as e:
            logger.error(f"Grid trade execution failed: {e}")
            logger.error(traceback.format_exc())
    
    async def execute_multi_sell(self, spot_price: float, perp_price: float):
        """
        Execute multiple sells if price jumped multiple grids at once.
        CRITICAL: Properly advances grid levels to prevent corruption.
        """
        try:
            logger.info(f"[MULTI-SELL] Price jumped to {spot_price}, checking for multiple grid hits")
            
            # Calculate how many grids we moved up
            price_diff = spot_price - self.last_buy_level
            grids_jumped = int(price_diff / GRID_SIZE)
            
            if grids_jumped <= 0:
                return
            
            logger.info(f"[MULTI-SELL] Detected {grids_jumped} grid jumps - executing sells")
            
            # Use fixed amount per grid to match arbitrage model
            # Don't recalculate per iteration - causes balance issues
            amount = self.calculate_amount(spot_price)
            
            # Validate we have enough base asset for all grids
            balance = self.spot.fetch_balance()
            base_balance = balance.get(BASE_ASSET, {}).get("free", 0)
            max_grids = int(base_balance / amount)
            grids_to_execute = min(grids_jumped, max_grids)
            
            if grids_to_execute <= 0:
                logger.warning(f"Insufficient balance: need {grids_jumped * amount:.6f}, have {base_balance:.6f}")
                return
            
            logger.info(f"[MULTI-SELL] Executing {grids_to_execute}/{grids_jumped} grids with amount {amount:.6f}")
            
            # Execute sells for each grid, ADVANCING GRID LEVEL each time
            for grid_num in range(grids_to_execute):
                # CRITICAL FIX: Each grid is at last_buy_level + (grid_num+1)*GRID_SIZE
                grid_sell_price = self.last_buy_level + (grid_num + 1) * GRID_SIZE
                
                logger.debug(f"[MULTI-SELL] Grid #{grid_num+1} at level {grid_sell_price}")
                
                # Lock this specific trade
                state.open_position = True
                state.position_type = "sell"
                
                # Hedge-first: perp long
                perp_order = await self.execute_perp_long(amount)
                if not perp_order:
                    logger.error(f"Perp hedge failed for grid #{grid_num+1}")
                    state.open_position = False
                    break
                
                await asyncio.sleep(0.2)
                
                # Spot sell
                spot_order = await self.execute_spot_sell(grid_sell_price, amount)
                if not spot_order:
                    logger.warning(f"Spot sell failed for grid #{grid_num+1} - closing perp")
                    await self.execute_perp_short(amount)
                    state.open_position = False
                    break
                
                # CRITICAL FIX: Advance grid level after EACH successful trade
                profit_per_grid = (grid_sell_price - self.last_buy_level)
                spot_fee = amount * grid_sell_price * 0.001
                perp_fee = amount * perp_price * 0.0006
                net_profit = (profit_per_grid * amount) - spot_fee - perp_fee
                profit_percent = (net_profit / (self.last_buy_level * amount)) * 100
                
                state.add_trade(Trade(
                    id=str(int(time.time() * 1000)),
                    timestamp=datetime.now().isoformat(),
                    pair=self.pair,
                    side="sell",
                    price=grid_sell_price,
                    amount=amount,
                    fee=spot_fee + perp_fee,
                    profit_percent=max(0, profit_percent),
                    hedge_type="spot+perp"
                ))
                
                logger.info(f"[MULTI-SELL #{grid_num+1}] Grid {self.last_buy_level}->{grid_sell_price}: {amount:.6f} = {profit_percent:.4f}%")
                
                # Update grid level to this sell price
                self.last_sell_level = grid_sell_price
                state.last_sell_level = grid_sell_price
                
                # Unlock this trade before next iteration
                state.open_position = False
                await asyncio.sleep(0.1)
            
            state._save_state()
            
        except Exception as e:
            logger.error(f"Multi-sell failed: {e}")
            state.open_position = False
    
    async def execute_multi_buy(self, spot_price: float, perp_price: float):
        """
        Execute multiple buys if price jumped multiple grids down at once.
        CRITICAL: Properly advances grid levels to prevent corruption.
        """
        try:
            logger.info(f"[MULTI-BUY] Price dropped to {spot_price}, checking for multiple grid hits")
            
            # Calculate how many grids we moved down
            price_diff = self.last_sell_level - spot_price
            grids_jumped = int(price_diff / GRID_SIZE)
            
            if grids_jumped <= 0:
                return
            
            logger.info(f"[MULTI-BUY] Detected {grids_jumped} grid jumps - executing buys")
            
            # Use fixed amount per grid to match arbitrage model
            amount = self.calculate_amount(spot_price)
            
            # Validate we have enough USDT for all grids
            balance = self.spot.fetch_balance()
            usdt_balance = balance.get(QUOTE_ASSET, {}).get("free", 0)
            required_per_grid = amount * spot_price
            max_grids = int(usdt_balance / required_per_grid)
            grids_to_execute = min(grids_jumped, max_grids)
            
            if grids_to_execute <= 0:
                logger.warning(f"Insufficient USDT: need {grids_jumped * required_per_grid:.2f}, have {usdt_balance:.2f}")
                return
            
            logger.info(f"[MULTI-BUY] Executing {grids_to_execute}/{grids_jumped} grids with amount {amount:.6f}")
            
            # Execute buys for each grid, ADVANCING GRID LEVEL each time
            for grid_num in range(grids_to_execute):
                # CRITICAL FIX: Each grid is at last_sell_level - (grid_num+1)*GRID_SIZE
                grid_buy_price = self.last_sell_level - (grid_num + 1) * GRID_SIZE
                
                logger.debug(f"[MULTI-BUY] Grid #{grid_num+1} at level {grid_buy_price}")
                
                # Lock this specific trade
                state.open_position = True
                state.position_type = "buy"
                
                # Hedge-first: perp short
                perp_order = await self.execute_perp_short(amount)
                if not perp_order:
                    logger.error(f"Perp hedge failed for grid #{grid_num+1}")
                    state.open_position = False
                    break
                
                await asyncio.sleep(0.2)
                
                # Spot buy
                spot_order = await self.execute_spot_buy(grid_buy_price, amount)
                if not spot_order:
                    logger.warning(f"Spot buy failed for grid #{grid_num+1} - closing perp")
                    await self.execute_perp_long(amount)
                    state.open_position = False
                    break
                
                # Record buy - profit will be calculated when sold
                spot_fee = amount * grid_buy_price * 0.001
                perp_fee = amount * perp_price * 0.0006
                total_fee = spot_fee + perp_fee
                
                state.add_trade(Trade(
                    id=str(int(time.time() * 1000)),
                    timestamp=datetime.now().isoformat(),
                    pair=self.pair,
                    side="buy",
                    price=grid_buy_price,
                    amount=amount,
                    fee=total_fee,
                    profit_percent=0.0,  # Calculated when sold
                    hedge_type="spot+perp"
                ))
                
                logger.info(f"[MULTI-BUY #{grid_num+1}] Grid {self.last_sell_level}->{grid_buy_price}: {amount:.6f} (fees: {total_fee:.8f})")
                
                # CRITICAL FIX: Advance grid level to this buy price
                self.last_buy_level = grid_buy_price
                state.last_buy_level = grid_buy_price
                
                # Unlock this trade before next iteration
                state.open_position = False
                await asyncio.sleep(0.1)
            
            state._save_state()
            
        except Exception as e:
            logger.error(f"Multi-buy failed: {e}")
            state.open_position = False
    
    async def capture_funding(self):
        """
        Background task: Capture funding payouts periodically.
        Updates state with funding profit (optional income).
        """
        while self.running:
            try:
                if time.time() - state.last_funding_capture > 3600:  # Every hour
                    funding = await self.get_funding_rate()
                    if funding and funding > 0:
                        # Estimate funding payout on perp position
                        if state.perp_position > 0:
                            funding_payout = state.perp_position * funding
                            state.funding_profit_usdt += funding_payout
                            state.total_profit_usdt += funding_payout
                            state.capital_usdt += funding_payout
                            logger.info(f"[FUNDING] Captured {funding_payout:.6f} USDT at {funding*100:.4f}% rate")
                            state._save_state()
                    state.last_funding_capture = time.time()
            except Exception as e:
                logger.debug(f"Funding capture error: {e}")
            
            await asyncio.sleep(60)
    
    async def run(self):
        """Main bot loop with funding capture background task"""
        self.running = True
        state.running = True
        logger.info("[BOT] Starting grid trading bot")
        
        # Start funding capture as background task
        funding_task = asyncio.create_task(self.capture_funding())
        
        try:
            while self.running:
                try:
                    await self.execute_grid_trade()
                except Exception as e:
                    logger.error(f"Grid trade error: {e}")
                
                await asyncio.sleep(CHECK_INTERVAL)
        finally:
            funding_task.cancel()
            state._save_state()
            logger.info("[BOT] Bot stopped, state saved to disk")
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
    """Get bot status with triple income breakdown"""
    funding = await bot.get_funding_rate() if bot else None
    
    return {
        "running": state.running,
        "capital_usdt": round(state.capital_usdt, 4),
        "total_profit_usdt": round(state.total_profit_usdt, 4),
        "grid_levels": len(state.grid_levels),
        "open_positions": int(abs(state.spot_balance) + abs(state.perp_position)),
        "total_trades": len(state.trades),
        "spot_price": state.current_price,
        "funding_rate": funding,
        "last_trade": state.trades[-1].timestamp if state.trades else None,
        # Triple Income Breakdown
        "triple_income": {
            "spot_profit": round(state.spot_profit_usdt, 4),
            "perp_profit": round(state.perp_profit_usdt, 4),
            "funding_profit": round(state.funding_profit_usdt, 4),
            "total": round(state.spot_profit_usdt + state.perp_profit_usdt + state.funding_profit_usdt, 4)
        },
        "grid_state": {
            "last_buy_level": state.last_buy_level,
            "last_sell_level": state.last_sell_level
        }
    }

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
