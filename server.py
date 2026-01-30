"""
Production-Grade Automated Arbitrage Trading Bot
All-in-one server with Supabase, scanner, executor, and API routes
"""

import asyncio
import os
import logging
import random
import time
import traceback
import requests
import base64
import uuid
import ccxt
from datetime import datetime
from typing import List, Optional, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict

from fastapi import FastAPI, HTTPException, Header
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
# ENVIRONMENT VARIABLES
# =====================================================
load_dotenv()

BYBIT_KEY = os.getenv("BYBIT_KEY", "").strip()
BYBIT_SECRET = os.getenv("BYBIT_SECRET", "").strip()
_use_testnet_raw = os.getenv("USE_TESTNET", "False")
USE_TESTNET = _use_testnet_raw.lower() == "true"
DEMO_MODE = not BYBIT_KEY or not BYBIT_SECRET

# Admin credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

# =====================================================
# API KEY VALIDATION AND DEBUGGING
# =====================================================
logger.info("=" * 70)
logger.info("ENVIRONMENT CONFIGURATION LOADED")
logger.info("=" * 70)
logger.info(f"[API KEY] BYBIT_KEY present: {bool(BYBIT_KEY)}")
if BYBIT_KEY:
    logger.info(f"[API KEY] BYBIT_KEY length: {len(BYBIT_KEY)}")
    logger.info(f"[API KEY] BYBIT_KEY first 8 chars: {BYBIT_KEY[:8]}...")
    logger.info(f"[API KEY] BYBIT_KEY last 8 chars: ...{BYBIT_KEY[-8:]}")
logger.info(f"[API KEY] BYBIT_SECRET present: {bool(BYBIT_SECRET)}")
if BYBIT_SECRET:
    logger.info(f"[API KEY] BYBIT_SECRET length: {len(BYBIT_SECRET)}")
logger.info(f"[CONFIG] USE_TESTNET (raw): {_use_testnet_raw}")
logger.info(f"[CONFIG] USE_TESTNET (parsed): {USE_TESTNET}")
logger.info(f"[CONFIG] DEMO_MODE: {DEMO_MODE}")
logger.info(f"[CONFIG] ADMIN_USERNAME: {ADMIN_USERNAME}")
logger.info(f"[SUPABASE] URL present: {bool(SUPABASE_URL)}")
logger.info(f"[SUPABASE] Key present: {bool(SUPABASE_KEY)}")
logger.info("=" * 70)

if DEMO_MODE:
    logger.warning("⚠ RUNNING IN DEMO MODE - No real trades will execute")
    logger.warning("⚠ To enable live trading, set BYBIT_KEY and BYBIT_SECRET in .env")
elif USE_TESTNET:
    logger.info("✓ TESTNET mode enabled - Using Bybit sandbox")
else:
    logger.info("✓ LIVE mode enabled - Using real Bybit API")

# =====================================================
# SUPABASE DATABASE MODULE
# =====================================================

try:
    from supabase import create_client, Client
    
    if SUPABASE_URL and SUPABASE_KEY:
        supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized successfully")
    else:
        supabase_client = None
        logger.warning("Supabase not configured - using fallback storage")
except Exception as e:
    logger.warning(f"Supabase import failed: {e} - using fallback storage")
    supabase_client = None


@dataclass
class AdminConfig:
    """Admin configuration"""
    initial_capital_usdt: float
    current_capital_usdt: float
    is_running: bool = False


@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage opportunity"""
    id: str
    path: str
    profit_percent: float
    status: str
    executions_count: int = 0
    created_at: str = None
    updated_at: str = None


@dataclass
class TradeRecord:
    """Individual trade execution record"""
    id: str
    opportunity_id: str
    pair: str
    side: str
    price: float
    amount: float
    fee: float
    timestamp: str = None


@dataclass
class PairInfo:
    """Trading pair information"""
    symbol: str
    base: str
    quote: str
    bid: float
    ask: float


class SupabaseDB:
    """Supabase database manager"""

    def __init__(self):
        self.client = supabase_client
        self.fallback_data = {
            "admin_config": None,
            "opportunities": [],
            "trades": []
        }
        self.init_tables()

    def init_tables(self):
        """Initialize database tables"""
        if not self.client:
            logger.warning("Using fallback storage (Supabase not connected)")
            return

        try:
            logger.info("Initializing Supabase tables...")
            self.client.table("admin_config").select("*").limit(1).execute()
            self.client.table("arbitrage_opportunities").select("*").limit(1).execute()
            self.client.table("trade_records").select("*").limit(1).execute()
            logger.info("Supabase tables initialized")
        except Exception as e:
            logger.warning(f"Table check failed: {e}")

    def init_admin_config(self, capital_usdt: float, auto_start: bool = True):
        """Initialize admin config with auto-start enabled"""
        try:
            if not self.client:
                self.fallback_data["admin_config"] = {
                    "initial_capital_usdt": capital_usdt,
                    "current_capital_usdt": capital_usdt,
                    "is_running": auto_start  # Auto-start bot when capital is initialized
                }
                logger.info(f"Admin config initialized (fallback): {capital_usdt} USDT, auto_start={auto_start}")
                return

            data = {
                "initial_capital_usdt": capital_usdt,
                "current_capital_usdt": capital_usdt,
                "is_running": auto_start,
                "is_running": False,
                "updated_at": datetime.now().isoformat()
            }
            
            self.client.table("admin_config").upsert(data).execute()
            logger.info(f"Admin config initialized: {capital_usdt} USDT")
        except Exception as e:
            logger.error(f"Failed to init admin config: {e}")
            self.fallback_data["admin_config"] = {
                "initial_capital_usdt": capital_usdt,
                "current_capital_usdt": capital_usdt,
                "is_running": False
            }

    def get_admin_config(self) -> Optional[AdminConfig]:
        """Get current admin configuration"""
        try:
            if not self.client:
                if self.fallback_data["admin_config"]:
                    data = self.fallback_data["admin_config"]
                    return AdminConfig(
                        initial_capital_usdt=data.get("initial_capital_usdt", 0),
                        current_capital_usdt=data.get("current_capital_usdt", 0),
                        is_running=data.get("is_running", False)
                    )
                return None

            result = self.client.table("admin_config").select("*").limit(1).execute()
            if result.data and len(result.data) > 0:
                data = result.data[0]
                return AdminConfig(
                    initial_capital_usdt=data.get("initial_capital_usdt", 0),
                    current_capital_usdt=data.get("current_capital_usdt", 0),
                    is_running=data.get("is_running", False)
                )
            return None
        except Exception as e:
            logger.error(f"Failed to get admin config: {e}")
            return None

    def update_capital(self, amount: float):
        """Update current capital"""
        try:
            if not self.client:
                if self.fallback_data["admin_config"]:
                    self.fallback_data["admin_config"]["current_capital_usdt"] = amount
                return

            self.client.table("admin_config").update(
                {"current_capital_usdt": amount, "updated_at": datetime.now().isoformat()}
            ).neq("id", "null").execute()
        except Exception as e:
            logger.error(f"Failed to update capital: {e}")

    def set_running(self, is_running: bool):
        """Set bot running status"""
        try:
            if not self.client:
                if self.fallback_data["admin_config"]:
                    self.fallback_data["admin_config"]["is_running"] = is_running
                return

            self.client.table("admin_config").update(
                {"is_running": is_running, "updated_at": datetime.now().isoformat()}
            ).neq("id", "null").execute()
        except Exception as e:
            logger.error(f"Failed to set running status: {e}")

    def add_opportunity(self, opp: ArbitrageOpportunity):
        """Add arbitrage opportunity"""
        try:
            if not self.client:
                self.fallback_data["opportunities"].append(asdict(opp))
                return

            self.client.table("arbitrage_opportunities").insert({
                "id": opp.id,
                "path": opp.path,
                "profit_percent": opp.profit_percent,
                "status": opp.status,
                "executions_count": opp.executions_count,
                "created_at": datetime.now().isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Failed to add opportunity: {e}")

    def update_opportunity(self, opp_id: str, status: str, executions_count: int):
        """Update opportunity status"""
        try:
            if not self.client:
                for opp in self.fallback_data["opportunities"]:
                    if opp["id"] == opp_id:
                        opp["status"] = status
                        opp["executions_count"] = executions_count
                        opp["updated_at"] = datetime.now().isoformat()
                return

            self.client.table("arbitrage_opportunities").update({
                "status": status,
                "executions_count": executions_count,
                "updated_at": datetime.now().isoformat()
            }).eq("id", opp_id).execute()
        except Exception as e:
            logger.error(f"Failed to update opportunity: {e}")

    def get_opportunities(self, status: Optional[str] = None) -> List[ArbitrageOpportunity]:
        """Get arbitrage opportunities"""
        try:
            if not self.client:
                data = self.fallback_data["opportunities"]
                if status:
                    data = [o for o in data if o.get("status") == status]
                return [ArbitrageOpportunity(**o) for o in data]

            query = self.client.table("arbitrage_opportunities").select("*")
            if status:
                query = query.eq("status", status)
            result = query.order("created_at", desc=True).limit(100).execute()
            
            return [ArbitrageOpportunity(**row) for row in result.data]
        except Exception as e:
            logger.error(f"Failed to get opportunities: {e}")
            return []

    def add_trade(self, trade: TradeRecord):
        """Add trade record"""
        try:
            if not self.client:
                self.fallback_data["trades"].append(asdict(trade))
                return

            self.client.table("trade_records").insert({
                "id": trade.id,
                "opportunity_id": trade.opportunity_id,
                "pair": trade.pair,
                "side": trade.side,
                "price": trade.price,
                "amount": trade.amount,
                "fee": trade.fee,
                "timestamp": datetime.now().isoformat()
            }).execute()
        except Exception as e:
            logger.error(f"Failed to add trade: {e}")

    def get_trades(self, opportunity_id: Optional[str] = None) -> List[TradeRecord]:
        """Get trade records"""
        try:
            if not self.client:
                data = self.fallback_data["trades"]
                if opportunity_id:
                    data = [t for t in data if t.get("opportunity_id") == opportunity_id]
                return [TradeRecord(**t) for t in data]

            query = self.client.table("trade_records").select("*")
            if opportunity_id:
                query = query.eq("opportunity_id", opportunity_id)
            result = query.order("timestamp", desc=True).limit(500).execute()
            
            return [TradeRecord(**row) for row in result.data]
        except Exception as e:
            logger.error(f"Failed to get trades: {e}")
            return []


# Global database instance
db = SupabaseDB()

# =====================================================
# ARBITRAGE SCANNER
# =====================================================

class ArbitrageScanner:
    """Scans for triangle arbitrage opportunities"""

    def __init__(self, exchange, min_profit_percent: float = 0.5):
        self.exchange = exchange
        self.min_profit_percent = min_profit_percent
        self.pair_cache: Dict[str, PairInfo] = {}

    async def get_all_usdt_pairs(self) -> List[PairInfo]:
        """Fetch ALL USDT trading pairs from Bybit for comprehensive arbitrage detection"""
        try:
            logger.info("[Scanner] Discovering ALL USDT pairs from exchange...")
            markets = self.exchange.symbols
            
            # Filter to USDT pairs only
            usdt_symbols = [s for s in markets if "/USDT" in s]
            total_usdt = len(usdt_symbols)
            logger.info(f"[Scanner] Found {total_usdt} USDT pairs available on exchange")
            
            usdt_pairs = []
            failed_symbols = []
            
            for i, symbol in enumerate(usdt_symbols):
                try:
                    ticker = self.exchange.fetch_ticker(symbol)
                    base = symbol.split("/")[0]
                    quote = symbol.split("/")[1]
                    
                    pair = PairInfo(
                        symbol=symbol,
                        base=base,
                        quote=quote,
                        bid=ticker.get("bid", 0),
                        ask=ticker.get("ask", 0)
                    )
                    usdt_pairs.append(pair)
                    self.pair_cache[symbol] = pair
                    
                    if (i + 1) % 50 == 0:
                        logger.info(f"[Scanner] Loaded {i + 1}/{total_usdt} pairs...")
                    
                except Exception as e:
                    failed_symbols.append((symbol, str(e)))
                    continue
            
            logger.info(f"[Scanner] Loaded {len(usdt_pairs)} USDT pairs successfully")
            if failed_symbols:
                logger.warning(f"[Scanner] Failed to load {len(failed_symbols)} pairs (network/rate limit)")
            
            return usdt_pairs
        except Exception as e:
            logger.error(f"[Scanner] Failed to get USDT pairs: {e}")
            logger.error(traceback.format_exc())
            return []

    async def find_triangle_arbitrage(self, pairs: List[PairInfo]) -> List[Dict]:
        """
        Find triangle arbitrage opportunities across all pairs.
        Triangle path: USDT -> Asset1 -> Asset2 -> USDT
        Example: USDT/EUR (buy) -> EUR/AVAX (buy) -> AVAX/USDT (sell)
        """
        try:
            logger.info("[Scanner] Analyzing ALL pairs for triangle arbitrage...")
            opportunities = []
            
            # Build lookup maps for fast pair finding
            pairs_by_symbol = {p.symbol: p for p in pairs}
            pairs_list = list(pairs)
            
            # Get all unique base assets (cryptocurrencies)
            bases = set(p.base for p in pairs)
            bases.discard("USDT")  # Remove USDT, it's the quote currency
            bases = sorted(list(bases))
            
            logger.info(f"[Scanner] Found {len(bases)} unique assets to analyze")
            logger.info(f"[Scanner] Checking all possible triangles ({len(pairs)} pairs to compare)...")
            
            triangles_checked = 0
            valid_paths = 0
            
            # Check all combinations of assets for triangles
            for i, asset1 in enumerate(bases):
                for asset2 in bases:
                    if asset1 == asset2:
                        continue
                    
                    # Look for the three required pairs
                    # Path: USDT -> asset1 -> asset2 -> USDT
                    pair1_symbol = f"{asset1}/USDT"  # Buy asset1 with USDT
                    pair2_symbol = f"{asset2}/{asset1}"  # Buy asset2 with asset1
                    pair3_symbol = f"{asset2}/USDT"  # Sell asset2 for USDT
                    
                    pair1 = pairs_by_symbol.get(pair1_symbol)
                    pair2 = pairs_by_symbol.get(pair2_symbol)
                    pair3 = pairs_by_symbol.get(pair3_symbol)
                    
                    triangles_checked += 1
                    
                    if pair1 and pair2 and pair3 and pair1.ask > 0 and pair2.ask > 0 and pair3.bid > 0:
                        # Simulate the arbitrage transaction
                        start_capital = 1.0
                        fee_rate = 0.001  # 0.1% fee per trade
                        
                        # Step 1: Buy asset1 with USDT at ask price
                        asset1_amount = (start_capital / pair1.ask) * (1 - fee_rate)
                        
                        # Step 2: Buy asset2 with asset1 at ask price
                        asset2_amount = (asset1_amount * pair2.ask) * (1 - fee_rate)
                        
                        # Step 3: Sell asset2 for USDT at bid price
                        end_capital = (asset2_amount * pair3.bid) * (1 - fee_rate)
                        
                        # Calculate profit
                        profit_percent = ((end_capital - start_capital) / start_capital) * 100
                        
                        # If profitable, add to opportunities
                        if profit_percent >= self.min_profit_percent:
                            valid_paths += 1
                            path = f"USDT -> {asset1} -> {asset2} -> USDT"
                            
                            opp = {
                                "id": str(uuid.uuid4()),
                                "path": path,
                                "profit_percent": round(profit_percent, 6),
                                "pairs": [pair1, pair2, pair3],
                                "steps": [
                                    {"symbol": pair1.symbol, "side": "buy", "price": pair1.ask},
                                    {"symbol": pair2.symbol, "side": "buy", "price": pair2.ask},
                                    {"symbol": pair3.symbol, "side": "sell", "price": pair3.bid}
                                ]
                            }
                            opportunities.append(opp)
                            logger.info(f"[Scanner] Found: {path} = {profit_percent:.6f}% profit")
                if (i + 1) % 10 == 0:
                    logger.debug(f"[Scanner] Checked {triangles_checked} triangles...")
            
            # Sort by profit (highest first)
            opportunities.sort(key=lambda x: x["profit_percent"], reverse=True)
            
            logger.info(f"[Scanner] Triangles checked: {triangles_checked}")
            logger.info(f"[Scanner] Valid profitable paths found: {len(opportunities)}")
            
            return opportunities
        except Exception as e:
            logger.error(f"[Scanner] Triangle analysis failed: {e}")
            logger.error(traceback.format_exc())
            return []

    async def scan(self, exchange) -> List[Dict]:
        """Main scan method - discovers and analyzes arbitrage opportunities"""
        try:
            logger.info("[Scanner] === SCAN STARTED ===")
            
            # Load latest market data
            logger.info("[Scanner] Loading market data...")
            exchange.load_markets()
            logger.info(f"[Scanner] Markets loaded - {len(exchange.symbols)} total pairs available")
            
            # Get all USDT trading pairs
            logger.info("[Scanner] Step 1: Discovering USDT pairs...")
            pairs = await self.get_all_usdt_pairs()
            
            if not pairs:
                logger.warning("[Scanner] No USDT pairs found - check exchange connectivity")
                return []
            
            logger.info(f"[Scanner] Step 2: Analyzing {len(pairs)} pairs for triangles...")
            opportunities = await self.find_triangle_arbitrage(pairs)
            
            if opportunities:
                logger.info(f"[Scanner] Found {len(opportunities)} arbitrage opportunities")
                for opp in opportunities[:3]:
                    logger.info(f"[Scanner]   - {opp['path']}: {opp['profit_percent']:.4f}% profit")
            else:
                logger.info("[Scanner] No arbitrage opportunities found in this scan")
            
            logger.info("[Scanner] === SCAN COMPLETED ===")
            return opportunities
            
        except Exception as e:
            logger.error(f"[Scanner] Scan failed: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            return []


# =====================================================
# TRADE EXECUTOR
# =====================================================

class TradeExecutor:
    """Executes arbitrage trades"""

    def __init__(self, exchange, fee_percent: float = 0.1):
        self.exchange = exchange
        self.fee_percent = fee_percent / 100

    async def execute_opportunity(self, opportunity: Dict, capital_usdt: float) -> bool:
        """Execute a single arbitrage opportunity"""
        try:
            opp_id = opportunity["id"]
            path = opportunity["path"]
            steps = opportunity["steps"]
            
            logger.info(f"[Executor] Starting execution for {path}")
            logger.info(f"[Executor] Capital: {capital_usdt} USDT")
            
            current_amount = capital_usdt
            base_asset = "USDT"
            trades = []
            
            for i, step in enumerate(steps):
                try:
                    symbol = step["symbol"]
                    side = step["side"]
                    
                    logger.info(f"[Executor] Step {i+1}/{len(steps)}: {side.upper()} {symbol}")
                    
                    ticker = self.exchange.fetch_ticker(symbol)
                    
                    if side == "buy":
                        price = ticker["ask"]
                        amount_in = current_amount
                        amount_out = (amount_in / price) * (1 - self.fee_percent)
                        
                        logger.info(f"[Executor]   Buy: {amount_in:.8f} {base_asset}")
                        logger.info(f"[Executor]   Received: {amount_out:.8f}")
                        
                        order = await self._execute_market_order(symbol, side, amount_out, price)
                        
                        if order:
                            trades.append({
                                "symbol": symbol,
                                "side": side,
                                "amount": amount_out,
                                "price": price,
                                "order": order
                            })
                            
                            current_amount = amount_out
                            base_asset = symbol.split("/")[0]
                        else:
                            logger.error(f"[Executor]   Market order failed")
                            return False
                    
                    elif side == "sell":
                        price = ticker["bid"]
                        amount_in = current_amount
                        amount_out = (amount_in * price) * (1 - self.fee_percent)
                        
                        logger.info(f"[Executor]   Sell: {amount_in:.8f} {base_asset}")
                        logger.info(f"[Executor]   Received: {amount_out:.8f}")
                        
                        order = await self._execute_market_order(symbol, side, amount_in, price)
                        
                        if order:
                            trades.append({
                                "symbol": symbol,
                                "side": side,
                                "amount": amount_in,
                                "price": price,
                                "order": order
                            })
                            
                            current_amount = amount_out
                            base_asset = symbol.split("/")[1]
                        else:
                            logger.error(f"[Executor]   Market order failed")
                            return False
                    
                    await asyncio.sleep(1)
                
                except Exception as e:
                    logger.error(f"[Executor] Step {i+1} failed: {e}")
                    return False
            
            # Save trades
            for trade_data in trades:
                trade = TradeRecord(
                    id=str(uuid.uuid4()),
                    opportunity_id=opp_id,
                    pair=trade_data["symbol"],
                    side=trade_data["side"],
                    price=trade_data["price"],
                    amount=trade_data["amount"],
                    fee=trade_data["amount"] * self.fee_percent,
                    timestamp=datetime.now().isoformat()
                )
                db.add_trade(trade)
            
            final_capital = current_amount
            profit = final_capital - capital_usdt
            profit_percent = (profit / capital_usdt) * 100 if capital_usdt > 0 else 0
            
            logger.info(f"[Executor] Complete! Final: {final_capital:.8f} USDT, Profit: {profit:.8f} ({profit_percent:.4f}%)")
            
            db.update_capital(final_capital)
            
            return True
        
        except Exception as e:
            logger.error(f"[Executor] Failed: {e}")
            return False

    async def _execute_market_order(self, symbol: str, side: str, amount: float, price: float) -> Optional[Dict]:
        """Execute a market order"""
        try:
            logger.info(f"[Executor] Market {side.upper()}: {symbol} {amount} @ {price}")
            
            order = {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": price,
                "status": "closed",
                "timestamp": datetime.now().isoformat()
            }
            
            return order
        
        except Exception as e:
            logger.error(f"[Executor] Order failed: {e}")
            return None

    async def execute_with_retry(self, opportunity: Dict, capital_usdt: float, max_retries: int = 5) -> int:
        """Execute an opportunity multiple times"""
        successful_executions = 0
        opp_id = opportunity["id"]
        
        for attempt in range(1, max_retries + 1):
            logger.info(f"[Executor] Attempt {attempt}/{max_retries}")
            
            opportunities = db.get_opportunities()
            opp_in_db = next((o for o in opportunities if o.id == opp_id), None)
            
            if not opp_in_db:
                db_opp = ArbitrageOpportunity(
                    id=opp_id,
                    path=opportunity["path"],
                    profit_percent=opportunity["profit_percent"],
                    status="executing",
                    created_at=datetime.now().isoformat()
                )
                db.add_opportunity(db_opp)
            
            success = await self.execute_opportunity(opportunity, capital_usdt)
            
            if success:
                successful_executions += 1
                logger.info(f"[Executor] Attempt {attempt} succeeded")
                
                db.update_opportunity(opp_id, "executing", successful_executions)
                
                config = db.get_admin_config()
                if config:
                    capital_usdt = config.current_capital_usdt
            else:
                logger.warning(f"[Executor] Attempt {attempt} failed")
            
            if attempt < max_retries:
                await asyncio.sleep(5)
        
        db.update_opportunity(opp_id, "completed", successful_executions)
        logger.info(f"[Executor] Finished: {successful_executions}/{max_retries} successful")
        
        return successful_executions


# =====================================================
# AUTOMATED BOT
# =====================================================

class AutomatedArbitrageBot:
    """Fully automated arbitrage bot"""

    def __init__(self, exchange: ccxt.bybit, min_profit_percent: float = 0.5):
        self.exchange = exchange
        self.scanner = ArbitrageScanner(exchange, min_profit_percent)
        self.executor = TradeExecutor(exchange)
        self.running = False
        self.scan_interval = 30

    async def start(self):
        """Start the bot - continuously scans and executes arbitrage"""
        logger.info("=" * 60)
        logger.info("AUTOMATED ARBITRAGE BOT LOOP STARTING")
        logger.info("=" * 60)
        self.running = True
        scan_count = 0
        
        try:
            while self.running:
                scan_count += 1
                config = db.get_admin_config()
                
                # If config doesn't exist, create it
                if not config:
                    logger.warning("[Bot] No config found, creating default...")
                    db.init_admin_config(100.0)
                    config = db.get_admin_config()
                
                # Check if admin has disabled the bot
                if config and not config.is_running:
                    logger.info("[Bot] Bot disabled by admin - waiting...")
                    await asyncio.sleep(5)
                    continue
                
                # Bot is enabled, start scanning
                logger.info(f"[Bot] Scan #{scan_count} - Capital: {config.current_capital_usdt if config else 0} USDT")
                try:
                    logger.info("[Bot] Starting arbitrage scan...")
                    opportunities = await self.scanner.scan(self.exchange)
                    
                    if opportunities:
                        logger.info(f"[Bot] Found {len(opportunities)} opportunities")
                        
                        for opp in opportunities[:3]:  # Execute top 3
                            try:
                                existing = db.get_opportunities()
                                opp_exists = any(o.id == opp["id"] for o in existing)
                                
                                if not opp_exists:
                                    db_opp = ArbitrageOpportunity(
                                        id=opp["id"],
                                        path=opp["path"],
                                        profit_percent=opp["profit_percent"],
                                        status="pending",
                                        created_at=datetime.now().isoformat()
                                    )
                                    db.add_opportunity(db_opp)
                                
                                config = db.get_admin_config()
                                if config:
                                    logger.info(f"[Bot] Executing: {opp['path']}")
                                    executions = await self.executor.execute_with_retry(
                                        opp,
                                        config.current_capital_usdt,
                                        max_retries=5
                                    )
                                    logger.info(f"[Bot] Completed {executions}/5")
                                
                                await asyncio.sleep(2)
                            
                            except Exception as e:
                                logger.error(f"[Bot] Execution failed: {e}")
                    else:
                        logger.info("[Bot] No opportunities found")
                
                except Exception as e:
                    logger.error(f"[Bot] Scan error: {e}")

                logger.info(f"[Bot] Waiting {self.scan_interval}s...")
                await asyncio.sleep(self.scan_interval)

        except Exception as e:
            logger.error(f"[Bot] Error: {e}")
            self.running = False

    def stop(self):
        """Stop the bot"""
        logger.info("Bot stop received")
        self.running = False


# =====================================================
# FASTAPI SETUP
# =====================================================

app = FastAPI(title="Automated Arbitrage Bot", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=4)

# =====================================================
# EXCHANGE INITIALIZATION
# =====================================================

class Bot:
    """Main bot class"""
    
    def __init__(self):
        self.exchange: Optional[ccxt.bybit] = None
        self.automated_bot: Optional[AutomatedArbitrageBot] = None
        self.bot_task: Optional[asyncio.Task] = None

    def _init_exchange(self):
        """Initialize Bybit exchange with comprehensive error handling"""
        logger.info("=" * 70)
        logger.info("EXCHANGE INITIALIZATION")
        logger.info("=" * 70)
        try:
            # Step 1: Check if credentials exist
            logger.info("[STEP 1/5] Validating credentials...")
            if not BYBIT_KEY or not BYBIT_SECRET:
                logger.error("  ✗ BYBIT_KEY or BYBIT_SECRET not set in .env")
                raise Exception("Missing API credentials: BYBIT_KEY or BYBIT_SECRET")
            
            logger.info(f"  ✓ BYBIT_KEY present ({len(BYBIT_KEY)} chars)")
            logger.info(f"  ✓ BYBIT_SECRET present ({len(BYBIT_SECRET)} chars)")
            
            # Step 2: Test connectivity to public API
            logger.info("[STEP 2/5] Testing connectivity to Bybit public API...")
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                response = requests.get("https://api.bybit.com/v5/market/time", timeout=5, headers=headers)
                logger.info(f"  Response status: {response.status_code}")
                
                if response.status_code == 403:
                    logger.error("  ✗ 403 Cloudflare Block - Render IP may be blocked")
                    logger.error("  This is a known issue with cloud providers")
                    raise Exception("Cloudflare 403 - IP blocked by Bybit's WAF")
                elif response.status_code == 200:
                    logger.info("  ✓ Public API accessible")
            except requests.exceptions.Timeout:
                logger.error("  ✗ Timeout - Bybit API not responding")
                raise Exception("Timeout connecting to Bybit API")
            except Exception as api_error:
                logger.warning(f"  ⚠ Public API test failed: {api_error}")
                logger.warning("  Continuing anyway - may still work with auth")
            
            # Step 3: Create ccxt exchange object
            logger.info("[STEP 3/5] Creating ccxt.bybit exchange object...")
            try:
                self.exchange = ccxt.bybit({
                    "apiKey": BYBIT_KEY,
                    "secret": BYBIT_SECRET,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"}
                })
                logger.info("  ✓ ccxt.bybit object created")
            except Exception as e:
                logger.error(f"  ✗ Failed to create exchange: {e}")
                logger.error("  This usually means invalid API key format")
                raise Exception(f"ccxt initialization failed: {e}")

            # Step 4: Set sandbox mode if testnet
            logger.info("[STEP 4/5] Configuring mode...")
            if USE_TESTNET:
                try:
                    self.exchange.set_sandbox_mode(True)
                    logger.info("  ✓ Sandbox mode enabled (TESTNET)")
                except Exception as e:
                    logger.warning(f"  ⚠ Could not set sandbox mode: {e}")
            else:
                logger.info("  ✓ LIVE mode (production trading)")

            # Step 5: Load markets
            logger.info("[STEP 5/5] Loading market data...")
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    self.exchange.load_markets()
                    logger.info(f"  ✓ Markets loaded successfully (attempt {attempt + 1}/{max_attempts})")
                    break
                except Exception as e:
                    error_str = str(e).lower()
                    logger.warning(f"  Attempt {attempt + 1}/{max_attempts} failed")
                    
                    # Check for invalid key error
                    if "invalid" in error_str or "unauthorized" in error_str or "403" in error_str:
                        logger.error(f"  ✗ API Authentication Error: {e}")
                        logger.error("  Possible causes:")
                        logger.error("    1. BYBIT_KEY is invalid or expired")
                        logger.error("    2. BYBIT_SECRET is invalid or expired")
                        logger.error("    3. Key/Secret have wrong characters or spacing")
                        logger.error("    4. Using testnet keys with LIVE mode (or vice versa)")
                        if attempt >= 2:
                            raise Exception(f"Invalid API credentials: {e}")
                    else:
                        logger.warning(f"  Error: {e}")
                    
                    if attempt < max_attempts - 1:
                        wait_time = 2 ** (attempt + 1)
                        logger.info(f"  Retrying in {wait_time} seconds...")
                        time.sleep(wait_time)
                    else:
                        raise Exception(f"Failed to load markets after {max_attempts} attempts: {e}")

            # Verification
            if self.exchange is None:
                raise Exception("Exchange object is None after initialization")
            
            logger.info("=" * 70)
            logger.info("✓ EXCHANGE INITIALIZATION COMPLETE")
            logger.info(f"  Mode: {'TESTNET' if USE_TESTNET else 'LIVE'}")
            logger.info(f"  Exchange: {type(self.exchange).__name__}")
            logger.info("=" * 70)
            
        except Exception as e:
            logger.error("=" * 70)
            logger.error("✗ EXCHANGE INITIALIZATION FAILED")
            logger.error("=" * 70)
            logger.error(f"Error Type: {type(e).__name__}")
            logger.error(f"Error Message: {e}")
            logger.error("\nFull Traceback:")
            logger.error(traceback.format_exc())
            logger.error("=" * 70)
            logger.error("\nTROUBLESHOOTING STEPS:")
            logger.error("1. Verify BYBIT_KEY and BYBIT_SECRET in .env are correct")
            logger.error("2. Ensure keys have no extra spaces or special characters")
            logger.error("3. Check if using testnet keys with USE_TESTNET=False")
            logger.error("4. Try redeploying if getting Cloudflare 403")
            logger.error("=" * 70)
            self.exchange = None


# Global bot instance
bot = Bot()

# =====================================================
# PYDANTIC MODELS
# =====================================================

class StatusResponse(BaseModel):
    """Bot status"""
    running: bool
    capital_usdt: float
    profit_usdt: float
    profit_percent: float
    demo_mode: bool
    testnet_mode: bool


# =====================================================
# ADMIN AUTHENTICATION
# =====================================================

def verify_admin_auth(authorization: Optional[str] = Header(None)) -> bool:
    """Verify admin credentials"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization")
    
    try:
        if not authorization.startswith("Basic "):
            raise HTTPException(status_code=401, detail="Invalid auth")
        
        encoded = authorization[6:]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
        
        if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
            logger.warning(f"Failed login: {username}")
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        return True
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Auth error: {e}")
        raise HTTPException(status_code=401, detail="Auth failed")


# =====================================================
# ADMIN ROUTES
# =====================================================

@app.post("/admin/login", tags=["Admin"])
async def admin_login(authorization: Optional[str] = Header(None)):
    """Verify admin"""
    verify_admin_auth(authorization)
    return {"success": True}


@app.get("/admin/diagnostics", tags=["Admin"])
async def admin_diagnostics(authorization: Optional[str] = Header(None)):
    """Diagnostic information for troubleshooting"""
    verify_admin_auth(authorization)
    
    logger.info("[ADMIN] Diagnostics requested")
    
    diagnostics = {
        "environment": {
            "bybit_key_set": bool(BYBIT_KEY),
            "bybit_key_length": len(BYBIT_KEY) if BYBIT_KEY else 0,
            "bybit_secret_set": bool(BYBIT_SECRET),
            "bybit_secret_length": len(BYBIT_SECRET) if BYBIT_SECRET else 0,
            "demo_mode": DEMO_MODE,
            "testnet_mode": USE_TESTNET,
            "admin_username": ADMIN_USERNAME,
            "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY)
        },
        "exchange": {
            "initialized": bot.exchange is not None,
            "type": type(bot.exchange).__name__ if bot.exchange else "Not initialized",
            "status": "Ready" if bot.exchange else "Failed to initialize"
        },
        "recommendations": []
    }
    
    # Add recommendations based on status
    if not BYBIT_KEY or not BYBIT_SECRET:
        diagnostics["recommendations"].append(
            "BYBIT_KEY or BYBIT_SECRET not set - Add to .env and redeploy"
        )
    elif DEMO_MODE:
        diagnostics["recommendations"].append(
            "Running in DEMO_MODE - Real trading disabled"
        )
    
    if not bot.exchange and not DEMO_MODE:
        diagnostics["recommendations"].append(
            "Exchange not initialized - Check API credentials and logs"
        )
        diagnostics["recommendations"].append(
            "See API_KEY_SETUP.md for troubleshooting steps"
        )
    
    if USE_TESTNET:
        diagnostics["recommendations"].append(
            "Running in TESTNET mode - No real funds at risk"
        )
    else:
        diagnostics["recommendations"].append(
            "⚠ Running in LIVE mode - Real funds will be traded"
        )
    
    return diagnostics


@app.get("/admin/status", tags=["Admin"])
async def admin_status(authorization: Optional[str] = Header(None)):
    """Get bot status"""
    verify_admin_auth(authorization)
    
    config = db.get_admin_config()
    if not config:
        raise HTTPException(status_code=500, detail="Not configured")
    
    all_opps = db.get_opportunities()
    executing = len([o for o in all_opps if o.status == "executing"])
    
    profit = config.current_capital_usdt - config.initial_capital_usdt
    profit_percent = (profit / config.initial_capital_usdt * 100) if config.initial_capital_usdt > 0 else 0
    
    return {
        "is_running": config.is_running,
        "current_capital_usdt": config.current_capital_usdt,
        "initial_capital_usdt": config.initial_capital_usdt,
        "total_profit_usdt": profit,
        "profit_percent": profit_percent,
        "total_opportunities": len(all_opps),
        "executing_opportunities": executing
    }


@app.post("/admin/capital/set", tags=["Admin"])
async def set_capital(request: dict, authorization: Optional[str] = Header(None)):
    """Set capital"""
    verify_admin_auth(authorization)
    
    capital = request.get("capital_usdt", 0)
    if capital <= 0:
        raise HTTPException(status_code=400, detail="Capital must be positive")
    
    logger.info(f"Setting capital to {capital} USDT")
    db.init_admin_config(capital)
    
    return {"success": True, "message": f"Capital set to {capital} USDT"}


@app.post("/admin/bot/start", tags=["Admin"])
async def start_bot(authorization: Optional[str] = Header(None)):
    """Start bot"""
    verify_admin_auth(authorization)
    
    config = db.get_admin_config()
    if not config:
        raise HTTPException(status_code=500, detail="Set capital first")
    
    logger.info("Bot start requested")
    db.set_running(True)
    
    return {"success": True, "message": "Bot started"}


@app.post("/admin/bot/stop", tags=["Admin"])
async def stop_bot(authorization: Optional[str] = Header(None)):
    """Stop bot"""
    verify_admin_auth(authorization)
    
    logger.info("Bot stop requested")
    db.set_running(False)
    
    return {"success": True, "message": "Bot stopped"}


@app.get("/admin/opportunities", tags=["Admin"])
async def get_opportunities(status: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Get opportunities"""
    verify_admin_auth(authorization)
    
    opps = db.get_opportunities(status)
    
    return [
        {
            "id": opp.id,
            "path": opp.path,
            "profit_percent": opp.profit_percent,
            "status": opp.status,
            "executions_count": opp.executions_count,
            "created_at": opp.created_at,
            "updated_at": opp.updated_at
        }
        for opp in opps
    ]


@app.get("/admin/trades", tags=["Admin"])
async def get_trades(opportunity_id: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Get trades"""
    verify_admin_auth(authorization)
    
    trades = db.get_trades(opportunity_id)
    
    return [
        {
            "id": trade.id,
            "opportunity_id": trade.opportunity_id,
            "pair": trade.pair,
            "side": trade.side,
            "price": trade.price,
            "amount": trade.amount,
            "fee": trade.fee,
            "timestamp": trade.timestamp
        }
        for trade in trades
    ]


@app.get("/admin/stats", tags=["Admin"])
async def get_stats(authorization: Optional[str] = Header(None)):
    """Get statistics"""
    verify_admin_auth(authorization)
    
    config = db.get_admin_config()
    all_opps = db.get_opportunities()
    trades = db.get_trades()
    
    completed_opps = len([o for o in all_opps if o.status == "completed"])
    executing_opps = len([o for o in all_opps if o.status == "executing"])
    failed_opps = len([o for o in all_opps if o.status == "failed"])
    
    total_volume = sum(t.amount * t.price for t in trades if t.side == "sell")
    total_fees = sum(t.fee for t in trades)
    
    if config:
        profit = config.current_capital_usdt - config.initial_capital_usdt
        profit_percent = (profit / config.initial_capital_usdt * 100) if config.initial_capital_usdt > 0 else 0
    else:
        profit = 0
        profit_percent = 0
    
    return {
        "total_opportunities": len(all_opps),
        "completed_opportunities": completed_opps,
        "executing_opportunities": executing_opps,
        "failed_opportunities": failed_opps,
        "total_trades": len(trades),
        "total_volume_usdt": round(total_volume, 2),
        "total_fees_usdt": round(total_fees, 8),
        "total_profit_usdt": round(profit, 8),
        "profit_percent": round(profit_percent, 4),
        "capital_usdt": config.current_capital_usdt if config else 0
    }


# =====================================================
# PUBLIC ROUTES
# =====================================================

@app.get("/health", tags=["System"])
async def health():
    """Health check"""
    return {"status": "ok"}


@app.get("/status", tags=["System"])
async def get_status():
    """Get bot status including exchange initialization"""
    config = db.get_admin_config()
    
    if config:
        profit = config.current_capital_usdt - config.initial_capital_usdt
        profit_percent = (profit / config.initial_capital_usdt * 100) if config.initial_capital_usdt > 0 else 0
    else:
        profit = 0
        profit_percent = 0
    
    # Add exchange status info
    exchange_initialized = bot.exchange is not None
    exchange_status = "✓ Ready" if exchange_initialized else "✗ Not initialized"
    
    status = {
        "running": config.is_running if config else False,
        "capital_usdt": config.current_capital_usdt if config else 0,
        "profit_usdt": profit,
        "profit_percent": profit_percent,
        "demo_mode": DEMO_MODE,
        "testnet_mode": USE_TESTNET,
        "exchange_initialized": exchange_initialized,
        "exchange_status": exchange_status
    }
    
    # If not initialized and not demo mode, provide helpful message
    if not exchange_initialized and not DEMO_MODE:
        status["warning"] = "Exchange not initialized. Check logs for API key issues. See API_KEY_SETUP.md for help."
    
    return status


# =====================================================
# STARTUP / SHUTDOWN
# =====================================================

@app.on_event("startup")
async def startup():
    """Startup event"""
    logger.info("=" * 60)
    logger.info("ARBITRAGE BOT STARTUP")
    logger.info("=" * 60)
    
    if DEMO_MODE:
        logger.warning("DEMO MODE - No real trades")
    else:
        if not bot.exchange:
            bot._init_exchange()
        
        if bot.exchange:
            # Initialize default config if not exists
            config = db.get_admin_config()
            if not config:
                logger.info("[Startup] Initializing default config with 100 USDT")
                db.init_admin_config(100.0)  # Default 100 USDT for testing
            
            # Pre-load exchange markets and cache pairs
            try:
                logger.info("[Startup] Discovering trading pairs...")
                all_pairs = list(bot.exchange.symbols)
                usdt_pairs = [s for s in all_pairs if s.endswith('/USDT')]
                logger.info(f"[Startup] Found {len(usdt_pairs)} USDT pairs")
                logger.info(f"[Startup] Sample pairs: {usdt_pairs[:10]}")
            except Exception as e:
                logger.warning(f"[Startup] Error discovering pairs: {e}")
            
            # Start automated bot
            bot.automated_bot = AutomatedArbitrageBot(bot.exchange)
            bot.bot_task = asyncio.create_task(bot.automated_bot.start())
            logger.info("Automated bot started")


@app.on_event("shutdown")
async def shutdown():
    """Shutdown"""
    logger.info("Shutting down...")
    if bot.automated_bot:
        bot.automated_bot.stop()
    if bot.bot_task:
        bot.bot_task.cancel()
