# Arbitrage Bot Backend

A production-ready FastAPI server for automated triangular arbitrage trading on Bybit.

## ✨ Features

- **CORS Enabled** - Works seamlessly with the frontend
- **Demo Mode** - Test without real API keys
- **Async-Safe** - Non-blocking exchange API calls
- **Error Handling** - Graceful error recovery
- **Trade History** - Track all executed trades
- **Input Validation** - Pydantic models with strict validation
- **Logging** - Comprehensive logging for monitoring

## 🚀 Quick Start

### 1. Install Dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy the example env file
cp .env.example .env

# Edit .env with your Bybit API credentials (optional for demo mode)
```

### 3. Run the Server

```bash
# Development mode with auto-reload
uvicorn server:app --reload --port 8000

# Production mode
uvicorn server:app --host 0.0.0.0 --port 8000
```

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check & server info |
| `GET` | `/status` | Get bot status (balance, profit, loops) |
| `GET` | `/trades` | Get trade history |
| `GET` | `/prices?symbols=BTC/USDT,ETH/USDT` | Get current prices |
| `POST` | `/config` | Set bot configuration |
| `POST` | `/start` | Start the arbitrage bot |
| `POST` | `/stop` | Stop the bot |
| `POST` | `/reset` | Reset bot state |

## 📋 Configuration

Send a POST request to `/config`:

```json
{
  "amount": 100,
  "loops": 10,
  "min_profit_percent": 0.3,
  "steps": [
    { "symbol": "BTC/USDT", "side": "buy" },
    { "symbol": "ETH/BTC", "side": "buy" },
    { "symbol": "ETH/USDT", "side": "sell" }
  ]
}
```

### Parameters

| Field | Type | Description |
|-------|------|-------------|
| `amount` | float | Initial trading amount (USDT) |
| `loops` | int | Number of cycles (1-1000) |
| `min_profit_percent` | float | Minimum profit % to execute (default: 0.3) |
| `steps` | array | Arbitrage path (2-10 steps) |

## 🧪 Demo Mode

If you don't provide `BYBIT_KEY` and `BYBIT_SECRET`, the server runs in **demo mode**:

- No real trades are executed
- Simulated prices for common trading pairs
- Perfect for testing the frontend integration

## 📊 Status Response

```json
{
  "running": true,
  "balance": 100.12345678,
  "profit": 0.12345678,
  "profit_percent": 0.1234,
  "loops_left": 8,
  "loops_completed": 2,
  "last_error": null,
  "demo_mode": false
}
```

## ⚠️ Warnings

> **IMPORTANT**: This bot executes **real trades** when configured with valid API keys.

- Start with small amounts to test
- Monitor the bot closely
- Ensure sufficient balance in your Bybit account
- Understand trading fees and slippage
- Triangular arbitrage opportunities are rare and brief

## 🔒 Security Notes

- Never commit your `.env` file
- Use read-only API keys if you just want to monitor prices
- Consider IP whitelisting on your Bybit API keys
- Run behind a reverse proxy (nginx) in production

## 📝 Logs

The server outputs detailed logs:

```
2024-01-15 10:30:00 | INFO | 🚀 Bot starting...
2024-01-15 10:30:00 | INFO |    Initial balance: 100.0
2024-01-15 10:30:01 | INFO | 📊 Cycle 1 | Balance: 100.00000000
2024-01-15 10:30:01 | INFO |    Simulated result: 100.15 (+0.15%)
2024-01-15 10:30:02 | INFO |   Step 1: BUY BTC/USDT | qty=0.00238095 @ 42000.00000000
2024-01-15 10:30:02 | INFO | ✅ Cycle 1 complete | Profit: +0.15 | Total: +0.15
```
