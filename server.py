import asyncio
import os
from typing import List, Dict

import ccxt
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# =====================================================
# ENV SETUP
# =====================================================

load_dotenv()

BYBIT_KEY = os.getenv("BYBIT_KEY")
BYBIT_SECRET = os.getenv("BYBIT_SECRET")

if not BYBIT_KEY or not BYBIT_SECRET:
    raise Exception("Missing BYBIT_KEY or BYBIT_SECRET in environment variables")

# =====================================================
# APP INIT
# =====================================================

app = FastAPI(title="Manual Arbitrage Executor")


# =====================================================
# MODELS
# =====================================================

class Step(BaseModel):
    symbol: str  # BTC/USDT
    side: str    # buy or sell


class BotConfig(BaseModel):
    amount: float
    loops: int
    min_profit_percent: float = 0.3
    steps: List[Step]


# =====================================================
# TRADER ENGINE
# =====================================================

class Trader:

    def __init__(self):

        self.exchange = ccxt.bybit({
            "apiKey": BYBIT_KEY,
            "secret": BYBIT_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })

        self.running = False
        self.config: BotConfig | None = None
        self.task = None

        self.total_profit = 0
        self.current_balance = 0

        self.fee = 0.001  # 0.1%

    # -------------------------
    # CONFIG
    # -------------------------

    def set_config(self, config: BotConfig):
        self.config = config
        self.current_balance = config.amount

    # -------------------------
    # STATUS
    # -------------------------

    def status(self):
        return {
            "running": self.running,
            "balance": round(self.current_balance, 6),
            "profit": round(self.total_profit, 6),
            "loops_left": self.config.loops if self.config else 0
        }

    # -------------------------
    # CONTROL
    # -------------------------

    def stop(self):
        self.running = False

    # -------------------------
    # SIMULATION
    # -------------------------

    async def simulate_cycle(self, amount: float):

        balance = amount

        for step in self.config.steps:
            ticker = self.exchange.fetch_ticker(step.symbol)
            price = ticker["last"]

            if step.side == "buy":
                balance = (balance / price) * (1 - self.fee)
            else:
                balance = (balance * price) * (1 - self.fee)

        return balance

    # -------------------------
    # EXECUTION
    # -------------------------

    async def execute_cycle(self, amount: float):

        balance = amount

        for step in self.config.steps:

            ticker = self.exchange.fetch_ticker(step.symbol)
            price = ticker["last"]

            qty = balance / price if step.side == "buy" else balance

            order = self.exchange.create_market_order(
                step.symbol,
                step.side,
                qty
            )

            # update balance
            if step.side == "buy":
                balance = qty
            else:
                balance = qty * price

            await asyncio.sleep(0.15)  # small delay for stability

        return balance

    # -------------------------
    # MAIN LOOP
    # -------------------------

    async def run(self):

        if not self.config:
            return

        self.running = True
        loops_left = self.config.loops

        while self.running and loops_left > 0:

            print(f"Cycle starting | balance={self.current_balance}")

            simulated = await self.simulate_cycle(self.current_balance)

            profit_percent = (
                (simulated - self.current_balance) /
                self.current_balance * 100
            )

            if profit_percent < self.config.min_profit_percent:
                print("Not profitable. Stopping.")
                break

            new_balance = await self.execute_cycle(self.current_balance)

            profit = new_balance - self.current_balance

            self.total_profit += profit
            self.current_balance = new_balance

            loops_left -= 1

            print(f"Cycle done | profit={profit}")

        self.running = False


# =====================================================
# INSTANCE
# =====================================================

bot = Trader()


# =====================================================
# API ROUTES
# =====================================================

@app.get("/")
def health():
    return {"msg": "Arbitrage bot running"}


@app.post("/config")
def set_config(config: BotConfig):
    bot.set_config(config)
    return {"msg": "config saved"}


@app.post("/start")
async def start():

    if bot.running:
        raise HTTPException(400, "Bot already running")

    bot.task = asyncio.create_task(bot.run())
    return {"msg": "bot started"}


@app.post("/stop")
def stop():
    bot.stop()
    return {"msg": "bot stopped"}


@app.get("/status")
def status():
    return bot.status()
