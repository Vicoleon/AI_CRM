# Logging Conventions

To maintain a clean and observable production environment, follow these logging conventions:

## 1. Structured Logging over `print()`
Never use `print()` for runtime logging. Use the standard Python `logging` library.
This ensures logs have timestamps, levels, and can be easily parsed by log aggregators (e.g., Cloud Logging).

```python
import logging

logger = logging.getLogger(__name__)

# Correct
logger.info("Processing asset: %s", asset_id)
# Incorrect
print(f"Processing asset: {asset_id}")
```

## 2. Choosing Log Levels
- **DEBUG**: Verbose information for developers (e.g., "Entering function X", "SQL query executed").
- **INFO**: General application milestones (e.g., "Starting backend", "Asset ingested successfully").
- **WARNING**: Potential issues that don't stop the app (e.g., "Optional dependency missing", "Slow response detected").
- **ERROR**: Serious issues that require attention but don't crash the app (e.g., "Ingestion failed for file X").
- **CRITICAL**: System-wide failures (e.g., "Database connection lost").

## 3. Security: No Sensitive Data
**NEVER** log the following:
- Bearer tokens or Session IDs
- Passwords or Secret Keys
- PII (Personally Identifiable Information) unless explicitly required and anonymized

## 4. Configuration
The log level is controlled by the `LOG_LEVEL` environment variable. Defaults to `INFO` in production.
In `scripts/start.sh`, Uvicorn's log level is also synchronized with this setting.
