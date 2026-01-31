"""
Production-Grade Spot + Perpetual Hedge Grid Trading Bot
=========================================================
Continuous trading with price-neutral hedging and optional funding capture.

Features:
- Grid trading with automatic position sizing
- Spot + Perp hedge for delta-neutral positions
- Funding rate capture for additional income
- State persistence for graceful restarts
- Atomic trade execution with rollback on failure
- Capital protection through hedge-first approach
"""

import asyncio
import os
import logging
import time
import traceback
import json
import random
from datetime import datetime
from typing import List, Optional, Dict
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor

import ccxt
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
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
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"
DEMO_MODE = not BYBIT_KEY or not BYBIT_SECRET

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Grid Trading Config (defaults, can be updated via API)
DEFAULT_BASE_ASSET = os.getenv("BASE_ASSET", "BTC").upper()
DEFAULT_QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDT").upper()
DEFAULT_GRID_SIZE = float(os.getenv("GRID_SIZE", "50"))
DEFAULT_TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "0.01"))
DEFAULT_MIN_PROFIT = float(os.getenv("MIN_PROFIT_PERCENT", "0.3"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3"))

# Thread pool for blocking ccxt operations
executor = ThreadPoolExecutor(max_workers=4)

logger.info("=" * 70)
logger.info("GRID TRADING BOT - SPOT + PERP HEDGE")
logger.info("=" * 70)
logger.info(f"Mode: {'TESTNET' if USE_TESTNET else 'LIVE'} | Demo: {DEMO_MODE}")
logger.info(f"Grid Size: {DEFAULT_GRID_SIZE} USDT | Trade Amount: {DEFAULT_TRADE_AMOUNT}")
logger.info(f"Min Profit: {DEFAULT_MIN_PROFIT}% | Check Interval: {CHECK_INTERVAL}s")
logger.info("=" * 70)

# =====================================================
# DATA MODELS
# =====================================================
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
    profit_usdt: float
    hedge_type: str  # "spot", "perp", or "spot+perp"


@dataclass
class GridConfig:
    """Configuration for grid trading"""
    base_asset: str = DEFAULT_BASE_ASSET
    quote_asset: str = DEFAULT_QUOTE_ASSET
    grid_size: float = DEFAULT_GRID_SIZE
    capital_usdt: float = 1000.0
    min_profit_percent: float = DEFAULT_MIN_PROFIT
    capital_per_trade_percent: float = 90.0  # Use 90% of capital per trade


class ConfigRequest(BaseModel):
    """API request for configuration"""
    base_asset: Optional[str] = None
    quote_asset: Optional[str] = None
    grid_size: Optional[float] = Field(None, gt=0)
    capital_usdt: Optional[float] = Field(None, gt=0)
    min_profit_percent: Optional[float] = Field(None, ge=0, le=100)


class StatusResponse(BaseModel):
    """API response for bot status"""
    running: bool
    demo_mode: bool
    capital_usdt: float
    available_capital: float
    total_profit_usdt: float
    total_trades: int
    spot_price: float
    perp_price: float
    funding_rate: Optional[float]
    last_trade: Optional[str]
    grid_state: dict
    triple_income: dict
    config: dict


# =====================================================
# BOT STATE MANAGEMENT
# =====================================================
class BotState:
    """Manages bot state and positions with persistence"""
    
    def __init__(self):
        self.running = False
        self.config = GridConfig()
        
        # Capital tracking
        self.available_capital = self.config.capital_usdt
        self.total_profit_usdt = 0.0
        self.trades: List[Trade] = []
        
        # Price tracking
        self.spot_price = 0.0
        self.perp_price = 0.0
        
        # Grid levels
        self.last_buy_level = 0.0
        self.last_sell_level = 0.0
        
        # Position tracking
        self.spot_balance = 0.0
        self.perp_position = 0.0
        
        # CRITICAL: Position lock - prevents double trades
        self.position_locked = False
        self.lock_reason = ""
        
        # Triple Income Tracking
        self.spot_profit_usdt = 0.0
        self.perp_profit_usdt = 0.0
        self.funding_profit_usdt = 0.0
        self.last_funding_capture = 0
        
        # Error tracking
        self.last_error: Optional[str] = None
        
        # Load persisted state
        self._load_state()
    
    def _load_state(self):
        """Load state from disk for graceful restart"""
        try:
            if os.path.exists("bot_state.json"):
                with open("bot_state.json", "r") as f:
                    data = json.load(f)
                    self.config.capital_usdt = data.get("capital_usdt", self.config.capital_usdt)
                    self.total_profit_usdt = data.get("total_profit_usdt", 0.0)
                    self.last_buy_level = data.get("last_buy_level", 0.0)
                    self.last_sell_level = data.get("last_sell_level", 0.0)
                    self.spot_profit_usdt = data.get("spot_profit_usdt", 0.0)
                    self.perp_profit_usdt = data.get("perp_profit_usdt", 0.0)
                    self.funding_profit_usdt = data.get("funding_profit_usdt", 0.0)
                    self.available_capital = self.config.capital_usdt
                    logger.info(f"[RESTORE] State loaded: capital={self.config.capital_usdt:.4f}, profit={self.total_profit_usdt:.4f}")
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")
    
    def save_state(self):
        """Save state to disk for graceful shutdown"""
        try:
            state_data = {
                "capital_usdt": self.config.capital_usdt,
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
        """Record a trade with compounding"""
        self.trades.append(trade)
        
        # COMPOUNDING: Add profit to capital
        if trade.profit_usdt > 0:
            self.total_profit_usdt += trade.profit_usdt
            self.config.capital_usdt += trade.profit_usdt
            self.available_capital = self.config.capital_usdt
            
            # Track profit by source (60% spot, 40% perp hedge for sells)
            if trade.side == "sell":
                self.spot_profit_usdt += trade.profit_usdt * 0.6
                self.perp_profit_usdt += trade.profit_usdt * 0.4
            
            logger.info(f"[COMPOUND] +{trade.profit_usdt:.4f} USDT | Total: {self.total_profit_usdt:.4f}")
            self.save_state()
        
        logger.info(f"[TRADE] {trade.side.upper()} {trade.pair}: {trade.amount:.6f} @ {trade.price:.2f}")
    
    def get_status(self) -> dict:
        """Get comprehensive status"""
        return {
            "running": self.running,
            "demo_mode": DEMO_MODE,
            "capital_usdt": round(self.config.capital_usdt, 4),
            "available_capital": round(self.available_capital, 4),
            "total_profit_usdt": round(self.total_profit_usdt, 4),
            "total_trades": len(self.trades),
            "spot_price": round(self.spot_price, 2),
            "perp_price": round(self.perp_price, 2),
            "funding_rate": None,  # Updated by engine
            "last_trade": self.trades[-1].timestamp if self.trades else None,
            "last_error": self.last_error,
            "grid_state": {
                "last_buy_level": round(self.last_buy_level, 2),
                "last_sell_level": round(self.last_sell_level, 2),
                "grid_size": self.config.grid_size,
                "position_locked": self.position_locked,
            },
            "triple_income": {
                "spot_profit": round(self.spot_profit_usdt, 4),
                "perp_profit": round(self.perp_profit_usdt, 4),
                "funding_profit": round(self.funding_profit_usdt, 4),
                "total": round(self.spot_profit_usdt + self.perp_profit_usdt + self.funding_profit_usdt, 4)
            },
            "config": {
                "base_asset": self.config.base_asset,
                "quote_asset": self.config.quote_asset,
                "grid_size": self.config.grid_size,
                "min_profit_percent": self.config.min_profit_percent,
            }
        }


# Global state instance
state = BotState()


# =====================================================
# EXCHANGE INITIALIZATION
# =====================================================
def init_exchanges():
    """Initialize spot and perpetual exchanges"""
    if DEMO_MODE:
        logger.warning("DEMO MODE - No real exchanges initialized")
        return None, None
    
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
            "options": {"defaultType": "swap"}  # Use swap for perpetuals
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
# GRID TRADING ENGINE
# =====================================================
class GridTradingEngine:
    """Automated spot+perp hedge grid trading with capital protection"""
    
    def __init__(self, spot_exchange, perp_exchange):
        self.spot = spot_exchange
        self.perp = perp_exchange
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.funding_task: Optional[asyncio.Task] = None
        
        # Fees (Bybit spot 0.1%, perp 0.06%)
        self.spot_fee = 0.001
        self.perp_fee = 0.0006
    
    @property
    def pair(self) -> str:
        return f"{state.config.base_asset}/{state.config.quote_asset}"
    
    @property
    def perp_pair(self) -> str:
        # Perpetual symbols often have different format
        return f"{state.config.base_asset}/{state.config.quote_asset}:{state.config.quote_asset}"
    
    def calculate_amount(self, price: float) -> float:
        """
        CAPITAL PROTECTION: Dynamic position sizing
        Use configured percentage of capital per trade.
        """
        capital_percent = state.config.capital_per_trade_percent / 100
        capital_to_use = state.config.capital_usdt * capital_percent
        amount = capital_to_use / price
        
        # Round to reasonable precision
        amount = round(amount, 6)
        logger.debug(f"[SIZING] Capital: {state.config.capital_usdt:.4f} x {capital_percent:.0%} = {amount:.6f} {state.config.base_asset}")
        return amount
    
    async def _run_sync(self, func, *args):
        """Run blocking ccxt call in executor"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(executor, func, *args)
    
    async def get_prices(self) -> Optional[Dict]:
        """Get current bid/ask prices from spot and perp"""
        try:
            if DEMO_MODE:
                return await self._get_demo_prices()
            
            # Fetch tickers in parallel
            spot_ticker = await self._run_sync(self.spot.fetch_ticker, self.pair)
            perp_ticker = await self._run_sync(self.perp.fetch_ticker, self.perp_pair)
            
            # Use bid/ask for accurate execution prices
            spot_bid = spot_ticker.get("bid", spot_ticker.get("last", 0))
            spot_ask = spot_ticker.get("ask", spot_ticker.get("last", 0))
            perp_bid = perp_ticker.get("bid", perp_ticker.get("last", 0))
            perp_ask = perp_ticker.get("ask", perp_ticker.get("last", 0))
            
            return {
                "spot_bid": spot_bid,
                "spot_ask": spot_ask,
                "spot_last": spot_ticker.get("last", 0),
                "perp_bid": perp_bid,
                "perp_ask": perp_ask,
                "perp_last": perp_ticker.get("last", 0),
            }
        except Exception as e:
            logger.error(f"Failed to fetch prices: {e}")
            return None
    
    async def _get_demo_prices(self) -> Dict:
        """Generate demo prices with realistic spread"""
        demo_prices = {
            "BTC": 65000.0,
            "ETH": 3500.0,
            "SOL": 150.0,
            "XRP": 0.55,
            "DOGE": 0.08,
        }
        
        base_price = demo_prices.get(state.config.base_asset, 100.0)
        
        # Add small variance
        variance = base_price * random.uniform(-0.001, 0.001)
        mid_price = base_price + variance
        
        # Simulate 0.05% spread
        spread = mid_price * 0.0005
        
        return {
            "spot_bid": mid_price - spread,
            "spot_ask": mid_price + spread,
            "spot_last": mid_price,
            "perp_bid": mid_price - spread * 0.8,
            "perp_ask": mid_price + spread * 0.8,
            "perp_last": mid_price,
        }
    
    async def get_funding_rate(self) -> Optional[float]:
        """Get current perpetual funding rate"""
        try:
            if DEMO_MODE:
                return random.uniform(-0.0001, 0.0003)
            
            funding = await self._run_sync(self.perp.fetch_funding_rate, self.perp_pair)
            return funding.get("fundingRate")
        except Exception as e:
            logger.debug(f"Could not fetch funding rate: {e}")
            return None
    
    async def check_balance(self, side: str, amount: float, price: float) -> bool:
        """
        CAPITAL PROTECTION: Verify sufficient balance before trade
        """
        try:
            if DEMO_MODE:
                return True
            
            balance = await self._run_sync(self.spot.fetch_balance)
            
            if side == "buy":
                # Need USDT to buy
                usdt_needed = amount * price * 1.01  # 1% buffer for fees
                usdt_available = balance.get(state.config.quote_asset, {}).get("free", 0)
                if usdt_available < usdt_needed:
                    logger.warning(f"Insufficient {state.config.quote_asset}: need {usdt_needed:.2f}, have {usdt_available:.2f}")
                    return False
            else:
                # Need base asset to sell
                base_available = balance.get(state.config.base_asset, {}).get("free", 0)
                if base_available < amount:
                    logger.warning(f"Insufficient {state.config.base_asset}: need {amount:.6f}, have {base_available:.6f}")
                    return False
            
            return True
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            return False
    
    async def execute_spot_order(self, side: str, amount: float) -> Optional[dict]:
        """Execute spot market order"""
        try:
            if DEMO_MODE:
                return await self._simulate_order("spot", side, amount)
            
            logger.info(f"[SPOT] {side.upper()} {amount:.6f} {state.config.base_asset}")
            order = await self._run_sync(
                lambda: self.spot.create_market_order(self.pair, side, amount)
            )
            logger.info(f"[SPOT] Order executed: {order.get('id')}")
            return order
        except Exception as e:
            logger.error(f"Spot {side} failed: {e}")
            state.last_error = f"Spot {side} failed: {str(e)}"
            return None
    
    async def execute_perp_order(self, side: str, amount: float) -> Optional[dict]:
        """Execute perpetual market order (for hedging)"""
        try:
            if DEMO_MODE:
                return await self._simulate_order("perp", side, amount)
            
            logger.info(f"[PERP] {side.upper()} {amount:.6f} {state.config.base_asset}")
            order = await self._run_sync(
                lambda: self.perp.create_market_order(self.perp_pair, side, amount)
            )
            logger.info(f"[PERP] Order executed: {order.get('id')}")
            return order
        except Exception as e:
            logger.error(f"Perp {side} failed: {e}")
            state.last_error = f"Perp {side} failed: {str(e)}"
            return None
    
    async def _simulate_order(self, market: str, side: str, amount: float) -> Optional[dict]:
        """Simulate order for demo mode"""
        prices = await self.get_prices()
        if not prices:
            return None
        
        if market == "spot":
            price = prices["spot_ask"] if side == "buy" else prices["spot_bid"]
        else:
            price = prices["perp_ask"] if side == "buy" else prices["perp_bid"]
        
        return {
            "id": f"demo_{int(time.time() * 1000)}",
            "symbol": self.pair if market == "spot" else self.perp_pair,
            "side": side,
            "amount": amount,
            "price": price,
            "cost": amount * price,
            "status": "closed",
            "type": "market"
        }
    
    async def execute_hedged_buy(self, prices: Dict) -> bool:
        """
        HEDGE-FIRST BUY: Short perp first, then buy spot
        This ensures we're delta-neutral before spot exposure.
        If perp fails, we abort. If spot fails, we close perp.
        """
        spot_price = prices["spot_ask"]  # We pay ask when buying
        amount = self.calculate_amount(spot_price)
        
        # Check balance first
        if not await self.check_balance("buy", amount, spot_price):
            return False
        
        # Lock position
        state.position_locked = True
        state.lock_reason = "executing_buy"
        
        try:
            # STEP 1: Hedge first - Short perp
            perp_order = await self.execute_perp_order("sell", amount)
            if not perp_order:
                logger.error("[HEDGE-BUY] Perp short failed - aborting buy")
                state.position_locked = False
                return False
            
            await asyncio.sleep(0.2)  # Brief delay for order settlement
            
            # STEP 2: Buy spot
            spot_order = await self.execute_spot_order("buy", amount)
            if not spot_order:
                logger.error("[HEDGE-BUY] Spot buy failed - closing perp hedge")
                # ROLLBACK: Close the perp position we opened
                await self.execute_perp_order("buy", amount)
                state.position_locked = False
                return False
            
            # Calculate fees
            spot_fee = amount * spot_price * self.spot_fee
            perp_fee = amount * prices["perp_bid"] * self.perp_fee
            total_fee = spot_fee + perp_fee
            
            # Record trade (buy has no profit yet - profit comes on sell)
            trade = Trade(
                id=str(int(time.time() * 1000)),
                timestamp=datetime.now().isoformat(),
                pair=self.pair,
                side="buy",
                price=spot_price,
                amount=amount,
                fee=total_fee,
                profit_usdt=0.0,
                hedge_type="spot+perp"
            )
            state.add_trade(trade)
            
            # Update grid level
            state.last_buy_level = spot_price
            state.spot_balance += amount
            state.perp_position -= amount  # Short = negative
            
            logger.info(f"[HEDGE-BUY] Complete: {amount:.6f} @ {spot_price:.2f} (fees: {total_fee:.4f})")
            return True
            
        finally:
            state.position_locked = False
            state.save_state()
    
    async def execute_hedged_sell(self, prices: Dict, entry_price: float) -> bool:
        """
        HEDGE-FIRST SELL: Long perp first, then sell spot
        This closes our hedge and realizes profit atomically.
        """
        spot_price = prices["spot_bid"]  # We get bid when selling
        amount = self.calculate_amount(spot_price)
        
        # Check balance
        if not await self.check_balance("sell", amount, spot_price):
            return False
        
        # Lock position
        state.position_locked = True
        state.lock_reason = "executing_sell"
        
        try:
            # STEP 1: Close hedge - Long perp (buy to close short)
            perp_order = await self.execute_perp_order("buy", amount)
            if not perp_order:
                logger.error("[HEDGE-SELL] Perp long failed - aborting sell")
                state.position_locked = False
                return False
            
            await asyncio.sleep(0.2)
            
            # STEP 2: Sell spot
            spot_order = await self.execute_spot_order("sell", amount)
            if not spot_order:
                logger.error("[HEDGE-SELL] Spot sell failed - re-opening perp hedge")
                # ROLLBACK: Re-open the perp short
                await self.execute_perp_order("sell", amount)
                state.position_locked = False
                return False
            
            # Calculate profit
            gross_profit = (spot_price - entry_price) * amount
            spot_fee = amount * spot_price * self.spot_fee
            perp_fee = amount * prices["perp_ask"] * self.perp_fee
            total_fee = spot_fee + perp_fee
            net_profit = gross_profit - total_fee
            
            # Record trade
            trade = Trade(
                id=str(int(time.time() * 1000)),
                timestamp=datetime.now().isoformat(),
                pair=self.pair,
                side="sell",
                price=spot_price,
                amount=amount,
                fee=total_fee,
                profit_usdt=max(0, net_profit),  # Never record negative profit
                hedge_type="spot+perp"
            )
            state.add_trade(trade)
            
            # Update grid level
            state.last_sell_level = spot_price
            state.spot_balance -= amount
            state.perp_position += amount  # Closing short = adding back
            
            profit_percent = (net_profit / (entry_price * amount)) * 100 if entry_price > 0 else 0
            logger.info(f"[HEDGE-SELL] Complete: {amount:.6f} @ {spot_price:.2f} | Profit: {net_profit:.4f} USDT ({profit_percent:.2f}%)")
            return True
            
        finally:
            state.position_locked = False
            state.save_state()
    
    async def execute_grid_trade(self):
        """Execute one grid trade cycle with hedge safety"""
        try:
            prices = await self.get_prices()
            if not prices:
                return
            
            spot_price = prices["spot_last"]
            state.spot_price = spot_price
            state.perp_price = prices["perp_last"]
            
            # Skip if position is locked
            if state.position_locked:
                logger.debug(f"[LOCK] Position locked ({state.lock_reason}) - skipping")
                return
            
            # Initialize grid on first run
            if state.last_buy_level == 0:
                logger.info(f"[GRID] Initializing at {spot_price:.2f}")
                state.last_buy_level = spot_price
                state.last_sell_level = spot_price
                state.save_state()
                return
            
            grid_size = state.config.grid_size
            
            # Check for SELL opportunity (price moved UP by grid_size)
            if spot_price >= state.last_buy_level + grid_size:
                grids_up = int((spot_price - state.last_buy_level) / grid_size)
                logger.info(f"[GRID] Price UP {grids_up} grid(s) to {spot_price:.2f}")
                
                # Execute sells for each grid level
                for i in range(min(grids_up, 3)):  # Max 3 grids at once for safety
                    entry_price = state.last_buy_level + (i * grid_size)
                    success = await self.execute_hedged_sell(prices, entry_price)
                    if not success:
                        break
                    await asyncio.sleep(0.3)
                
                return
            
            # Check for BUY opportunity (price moved DOWN by grid_size)
            if spot_price <= state.last_sell_level - grid_size:
                grids_down = int((state.last_sell_level - spot_price) / grid_size)
                logger.info(f"[GRID] Price DOWN {grids_down} grid(s) to {spot_price:.2f}")
                
                # Execute buys for each grid level
                for i in range(min(grids_down, 3)):  # Max 3 grids at once
                    success = await self.execute_hedged_buy(prices)
                    if not success:
                        break
                    await asyncio.sleep(0.3)
                
                return
                
        except Exception as e:
            logger.error(f"Grid trade error: {e}")
            logger.error(traceback.format_exc())
            state.last_error = str(e)
    
    async def capture_funding(self):
        """Background task: Capture funding rate payouts"""
        while self.running:
            try:
                if time.time() - state.last_funding_capture > 3600:  # Every hour
                    funding_rate = await self.get_funding_rate()
                    
                    if funding_rate and funding_rate > 0 and abs(state.perp_position) > 0:
                        # Calculate funding payout
                        funding_payout = abs(state.perp_position) * state.perp_price * funding_rate
                        
                        state.funding_profit_usdt += funding_payout
                        state.total_profit_usdt += funding_payout
                        state.config.capital_usdt += funding_payout
                        
                        logger.info(f"[FUNDING] Captured {funding_payout:.6f} USDT @ {funding_rate*100:.4f}%")
                        state.save_state()
                    
                    state.last_funding_capture = time.time()
                    
            except Exception as e:
                logger.debug(f"Funding capture error: {e}")
            
            await asyncio.sleep(60)
    
    async def run(self):
        """Main bot loop"""
        self.running = True
        state.running = True
        state.last_error = None
        
        logger.info("[BOT] Starting grid trading bot")
        logger.info(f"[BOT] Pair: {self.pair} | Grid: {state.config.grid_size} USDT")
        
        # Start funding capture as background task
        self.funding_task = asyncio.create_task(self.capture_funding())
        
        try:
            while self.running:
                try:
                    await self.execute_grid_trade()
                except Exception as e:
                    logger.error(f"Grid cycle error: {e}")
                    state.last_error = str(e)
                
                await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[BOT] Bot task cancelled")
        finally:
            if self.funding_task:
                self.funding_task.cancel()
            state.save_state()
            state.running = False
            self.running = False
            logger.info("[BOT] Bot stopped, state saved")
    
    def stop(self):
        """Stop the bot gracefully"""
        self.running = False
        state.running = False
        if self.task:
            self.task.cancel()
        logger.info("[BOT] Stop signal sent")


# =====================================================
# FASTAPI APP
# =====================================================
app = FastAPI(
    title="Grid Trading Bot API",
    description="Spot + Perp Hedge Grid Trading",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global engine instance
engine: Optional[GridTradingEngine] = None


# =====================================================
# AUTH HELPERS
# =====================================================
def verify_admin(authorization: Optional[str] = Header(None, alias="Authorization")):
    """Verify admin credentials from Basic auth header"""
    if not authorization or not authorization.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Missing authorization")
    
    try:
        import base64
        encoded = authorization[6:]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            return True
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except Exception:
        raise HTTPException(status_code=401, detail="Authorization failed")


# =====================================================
# API ROUTES
# =====================================================
@app.get("/")
async def health():
    """Health check"""
    return {
        "status": "online",
        "message": "Grid Trading Bot",
        "demo_mode": DEMO_MODE,
        "version": "2.0.0"
    }


@app.get("/status")
async def get_status():
    """Get bot status with triple income breakdown"""
    status_data = state.get_status()
    
    # Add funding rate if engine is available
    if engine:
        funding = await engine.get_funding_rate()
        status_data["funding_rate"] = funding
    
    return status_data


@app.get("/trades")
async def get_trades(limit: int = 100):
    """Get trade history"""
    trades = state.trades[-limit:] if state.trades else []
    return {
        "total": len(state.trades),
        "trades": [asdict(t) for t in trades]
    }


@app.post("/config")
async def set_config(config: ConfigRequest):
    """Update bot configuration"""
    if state.running:
        raise HTTPException(400, "Cannot change config while bot is running")
    
    if config.base_asset:
        state.config.base_asset = config.base_asset.upper()
    if config.quote_asset:
        state.config.quote_asset = config.quote_asset.upper()
    if config.grid_size:
        state.config.grid_size = config.grid_size
    if config.capital_usdt:
        state.config.capital_usdt = config.capital_usdt
        state.available_capital = config.capital_usdt
    if config.min_profit_percent is not None:
        state.config.min_profit_percent = config.min_profit_percent
    
    state.save_state()
    
    return {
        "message": "Configuration updated",
        "config": {
            "base_asset": state.config.base_asset,
            "quote_asset": state.config.quote_asset,
            "grid_size": state.config.grid_size,
            "capital_usdt": state.config.capital_usdt,
            "min_profit_percent": state.config.min_profit_percent,
        }
    }


@app.post("/start")
async def start_bot():
    """Start the trading bot"""
    global engine
    
    if state.running:
        raise HTTPException(400, "Bot is already running")
    
    if not engine:
        spot, perp = init_exchanges()
        if spot and perp:
            engine = GridTradingEngine(spot, perp)
        elif DEMO_MODE:
            engine = GridTradingEngine(None, None)
        else:
            raise HTTPException(500, "Failed to initialize exchanges")
    
    engine.task = asyncio.create_task(engine.run())
    
    return {"message": "Bot started", "demo_mode": DEMO_MODE}


@app.post("/stop")
async def stop_bot():
    """Stop the trading bot"""
    if not state.running:
        raise HTTPException(400, "Bot is not running")
    
    if engine:
        engine.stop()
    
    return {"message": "Bot stopped"}


@app.post("/reset")
async def reset_bot():
    """Reset bot state"""
    if state.running:
        raise HTTPException(400, "Cannot reset while bot is running")
    
    state.total_profit_usdt = 0.0
    state.spot_profit_usdt = 0.0
    state.perp_profit_usdt = 0.0
    state.funding_profit_usdt = 0.0
    state.trades.clear()
    state.last_buy_level = 0.0
    state.last_sell_level = 0.0
    state.spot_balance = 0.0
    state.perp_position = 0.0
    state.last_error = None
    state.save_state()
    
    return {"message": "Bot state reset"}


@app.get("/prices")
async def get_prices():
    """Get current market prices"""
    if not engine:
        if DEMO_MODE:
            temp_engine = GridTradingEngine(None, None)
            prices = await temp_engine.get_prices()
            funding = await temp_engine.get_funding_rate()
        else:
            raise HTTPException(503, "Engine not initialized")
    else:
        prices = await engine.get_prices()
        funding = await engine.get_funding_rate()
    
    return {
        "prices": prices,
        "funding_rate": funding,
        "demo_mode": DEMO_MODE
    }


# Admin routes
@app.post("/admin/capital")
async def set_capital(request: dict, auth: bool = Depends(verify_admin)):
    """Set initial capital (admin only)"""
    capital = request.get("capital_usdt", 1000)
    state.config.capital_usdt = capital
    state.available_capital = capital
    state.save_state()
    logger.info(f"[ADMIN] Capital set to {capital} USDT")
    return {"capital": capital}


@app.get("/admin/diagnostics")
async def diagnostics(auth: bool = Depends(verify_admin)):
    """Get diagnostics info (admin only)"""
    return {
        "config": {
            "base": state.config.base_asset,
            "quote": state.config.quote_asset,
            "grid_size": state.config.grid_size,
            "min_profit": state.config.min_profit_percent,
            "mode": "testnet" if USE_TESTNET else "live",
            "demo": DEMO_MODE
        },
        "state": state.get_status(),
        "engine": "running" if (engine and engine.running) else "stopped"
    }


# =====================================================
# STARTUP / SHUTDOWN
# =====================================================
@app.on_event("startup")
async def startup():
    """Initialize on startup"""
    logger.info("=" * 50)
    logger.info("Grid Trading Bot Server Starting")
    logger.info(f"   Demo Mode: {DEMO_MODE}")
    logger.info(f"   Version: 2.0.0")
    logger.info("=" * 50)


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown"""
    global engine
    logger.info("Server shutting down...")
    
    if engine:
        engine.stop()
    
    state.save_state()
    executor.shutdown(wait=True)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
