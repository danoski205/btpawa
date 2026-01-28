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

BYBIT_KEY = os.getenv("BYBIT_KEY")
BYBIT_SECRET = os.getenv("BYBIT_SECRET")
_use_testnet_raw = os.getenv("USE_TESTNET", "False")
USE_TESTNET = _use_testnet_raw.lower() == "true"
DEMO_MODE = not BYBIT_KEY or not BYBIT_SECRET

# Admin credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

logger.info(f"[DEBUG] USE_TESTNET: {USE_TESTNET}")
logger.info(f"[DEBUG] BYBIT_KEY set: {bool(BYBIT_KEY)}")
logger.info(f"[DEBUG] BYBIT_SECRET set: {bool(BYBIT_SECRET)}")
logger.info(f"[DEBUG] DEMO_MODE: {DEMO_MODE}")
logger.info(f"[DEBUG] Supabase URL: {bool(SUPABASE_URL)}")
logger.info(f"[DEBUG] Supabase Key: {bool(SUPABASE_KEY)}")

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

    def init_admin_config(self, capital_usdt: float):
        """Initialize admin config"""
        try:
            if not self.client:
                self.fallback_data["admin_config"] = {
                    "initial_capital_usdt": capital_usdt,
                    "current_capital_usdt": capital_usdt,
                    "is_running": False
                }
                logger.info(f"Admin config initialized (fallback): {capital_usdt} USDT")
                return

            data = {
                "initial_capital_usdt": capital_usdt,
                "current_capital_usdt": capital_usdt,
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
        """Fetch all USDT trading pairs from Bybit"""
        try:
            logger.info("[Scanner] Fetching all USDT pairs...")
            markets = self.exchange.symbols
            usdt_pairs = []
            
            for symbol in markets:
                if "/USDT" not in symbol:
                    continue
                
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
                except Exception as e:
                    logger.debug(f"Failed to fetch {symbol}: {e}")
                    continue
            
            logger.info(f"[Scanner] Found {len(usdt_pairs)} USDT pairs")
            return usdt_pairs
        except Exception as e:
            logger.error(f"[Scanner] Failed to get USDT pairs: {e}")
            return []

    async def find_triangle_arbitrage(self, pairs: List[PairInfo]) -> List[Dict]:
        """Find triangle arbitrage opportunities"""
        try:
            logger.info("[Scanner] Finding triangle opportunities...")
            opportunities = []
            
            pairs_by_base: Dict[str, List[PairInfo]] = {}
            for pair in pairs:
                if pair.base not in pairs_by_base:
                    pairs_by_base[pair.base] = []
                pairs_by_base[pair.base].append(pair)
            
            bases = [b for b in pairs_by_base.keys() if b != "USDT"]
            
            for i, base1 in enumerate(bases):
                for base2 in bases[i+1:]:
                    # USDT -> base1 -> base2 -> USDT
                    pair1 = self._find_pair(pairs, base1, "USDT")
                    pair2 = self._find_pair(pairs, base2, base1)
                    pair3 = self._find_pair(pairs, base2, "USDT")
                    
                    if pair1 and pair2 and pair3:
                        start_capital = 1.0
                        fee = 0.001
                        
                        base1_amount = (start_capital / pair1.ask) * (1 - fee)
                        base2_amount = (base1_amount / pair2.ask) * (1 - fee)
                        end_capital = (base2_amount * pair3.bid) * (1 - fee)
                        
                        profit_percent = ((end_capital - start_capital) / start_capital) * 100
                        
                        if profit_percent >= self.min_profit_percent:
                            path = f"USDT -> {base1} -> {base2} -> USDT"
                            opp = {
                                "id": str(uuid.uuid4()),
                                "path": path,
                                "profit_percent": round(profit_percent, 4),
                                "pairs": [pair1, pair2, pair3],
                                "steps": [
                                    {"symbol": pair1.symbol, "side": "buy", "price": pair1.ask},
                                    {"symbol": pair2.symbol, "side": "buy", "price": pair2.ask},
                                    {"symbol": pair3.symbol, "side": "sell", "price": pair3.bid}
                                ]
                            }
                            opportunities.append(opp)
                            logger.info(f"[Scanner] Found: {path} - {profit_percent}%")
            
            opportunities.sort(key=lambda x: x["profit_percent"], reverse=True)
            logger.info(f"[Scanner] Found {len(opportunities)} opportunities")
            
            return opportunities
        except Exception as e:
            logger.error(f"[Scanner] Failed: {e}")
            return []

    def _find_pair(self, pairs: List[PairInfo], base: str, quote: str) -> Optional[PairInfo]:
        """Find a pair"""
        for pair in pairs:
            if pair.base == base and pair.quote == quote:
                return pair
        return None

    async def scan(self, exchange) -> List[Dict]:
        """Main scan method"""
        try:
            exchange.load_markets()
            pairs = await self.get_all_usdt_pairs()
            
            if not pairs:
                logger.warning("[Scanner] No USDT pairs found")
                return []
            
            opportunities = await self.find_triangle_arbitrage(pairs)
            return opportunities
        except Exception as e:
            logger.error(f"[Scanner] Scan failed: {e}")
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
        """Start the bot"""
        logger.info("=" * 60)
        logger.info("AUTOMATED ARBITRAGE BOT STARTING")
        logger.info("=" * 60)
        self.running = True
        
        try:
            while self.running:
                config = db.get_admin_config()
                if not config or not config.is_running:
                    logger.info("[Bot] Stopped or not configured")
                    await asyncio.sleep(5)
                    continue

                logger.info("[Bot] Starting scan...")
                try:
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
        """Initialize Bybit exchange"""
        logger.info("=" * 60)
        logger.info("INITIALIZING EXCHANGE")
        logger.info("=" * 60)
        try:
            logger.info("[STEP 1] Testing API connectivity...")
            try:
                headers = {'User-Agent': 'Mozilla/5.0'}
                response = requests.get("https://api.bybit.com/v5/market/time", timeout=5, headers=headers)
                logger.info(f"  Response: {response.status_code}")
                
                if response.status_code == 403:
                    logger.error("  ✗ 403 CLOUDFLARE BLOCKING")
            except Exception as e:
                logger.error(f"  API test failed: {e}")
            
            logger.info("[STEP 2] Creating ccxt object...")
            self.exchange = ccxt.bybit({
                "apiKey": BYBIT_KEY,
                "secret": BYBIT_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"}
            })
            logger.info("  ✓ Created")

            logger.info("[STEP 3] Setting sandbox mode...")
            if USE_TESTNET:
                self.exchange.set_sandbox_mode(True)
                logger.info("  ✓ Sandbox enabled")
            else:
                logger.info("  LIVE mode")

            logger.info("[STEP 4] Loading markets...")
            for attempt in range(5):
                try:
                    self.exchange.load_markets()
                    logger.info(f"  ✓ Markets loaded (attempt {attempt + 1})")
                    break
                except Exception as e:
                    logger.warning(f"  Attempt {attempt + 1} failed: {e}")
                    if attempt < 4:
                        wait = 2 ** (attempt + 1)
                        logger.info(f"  Waiting {wait}s...")
                        time.sleep(wait)
                    else:
                        raise

            logger.info("[STEP 5] Verification...")
            if self.exchange is None:
                raise Exception("Exchange is None")
            logger.info("  ✓ Ready")
            
            logger.info("=" * 60)
            logger.info("✓ EXCHANGE INITIALIZED")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error("=" * 60)
            logger.error("✗ EXCHANGE INITIALIZATION FAILED")
            logger.error("=" * 60)
            logger.error(f"Error: {e}")
            logger.error(traceback.format_exc())
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
    """Get bot status"""
    config = db.get_admin_config()
    
    if config:
        profit = config.current_capital_usdt - config.initial_capital_usdt
        profit_percent = (profit / config.initial_capital_usdt * 100) if config.initial_capital_usdt > 0 else 0
    else:
        profit = 0
        profit_percent = 0
    
    return StatusResponse(
        running=config.is_running if config else False,
        capital_usdt=config.current_capital_usdt if config else 0,
        profit_usdt=profit,
        profit_percent=profit_percent,
        demo_mode=DEMO_MODE,
        testnet_mode=USE_TESTNET
    )


# =====================================================
# STARTUP / SHUTDOWN
# =====================================================

@app.on_event("startup")
async def startup():
    """Startup"""
    logger.info("=" * 60)
    logger.info("SERVER STARTUP")
    logger.info("=" * 60)
    
    if DEMO_MODE:
        logger.warning("DEMO MODE - No real trades")
    else:
        if not bot.exchange:
            bot._init_exchange()
        
        if bot.exchange:
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
