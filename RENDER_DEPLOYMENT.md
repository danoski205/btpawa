# Betpawa Monitor - Render Deployment Guide

## Requirements
- Python 3.9+
- Render account
- GitHub repository with the code

## Files Needed
- `betpawa_monitor.py` - Main application
- `requirements.txt` - Python dependencies
- `Procfile` - Render process configuration
- `.gitignore` (optional but recommended)

## Deployment Steps

### 1. Prepare GitHub Repository
Push these files to your GitHub repository:
```
betpawa_monitor.py
requirements.txt
Procfile
```

### 2. Create Background Worker on Render
1. Go to [Render Dashboard](https://dashboard.render.com)
2. Click **"New +"** → **"Background Worker"**
3. Connect your GitHub repository
4. Fill in the details:
   - **Name**: `betpawa-monitor` (or your preferred name)
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python betpawa_monitor.py`

### 3. Set Environment Variables (Optional)
In the Render dashboard, go to **Environment** and add:
- `BETPAWA_BASE_URL` = `https://www.betpawa.ng/api/sportsbook/virtual/v1` (default)
- `TIMEZONE` = `Africa/Lagos` (default, change if needed)
- `API_TIMEOUT` = `10` (default, in seconds)
- `MAX_RETRIES` = `3` (default)

### 4. Deploy
Click **"Create Background Worker"** to deploy.

## Monitoring

### View Logs
1. Go to your Background Worker in Render dashboard
2. Click **"Logs"** to see real-time output
3. Logs are also saved to `betpawa_monitor.log`

### Common Issues

**Issue**: Worker keeps restarting
- Check logs for errors
- Verify API endpoint is accessible
- Check network connectivity

**Issue**: No alerts appearing
- Verify the API is returning data
- Check timezone setting
- Monitor logs for "No teams retrieved"

**Issue**: Timeout errors
- Increase `API_TIMEOUT` env variable
- Check Betpawa API status

## Improvements Made

✅ **Robust Error Handling**: Comprehensive try-except blocks with logging
✅ **Retry Logic**: Automatic retries with exponential backoff
✅ **Logging**: File and console logging for debugging
✅ **Environment Variables**: Configurable settings without code changes
✅ **Session Management**: HTTP session with connection pooling
✅ **Data Validation**: Checks for missing/unexpected data structures
✅ **Graceful Shutdown**: Handles KeyboardInterrupt cleanly
✅ **Recovery Logic**: Auto-restarts after too many consecutive errors

## Testing Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run the monitor
python betpawa_monitor.py

# Stop with Ctrl+C
```

## Support
If the worker crashes or behaves unexpectedly, check:
1. The logs in Render dashboard
2. The `betpawa_monitor.log` file
3. API endpoint availability
4. Environment variable settings
