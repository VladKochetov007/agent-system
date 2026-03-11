# Live Trading

TradingNode setup, state persistence, reconciliation, memory management, deployment for NautilusTrader.

## TradingNode Setup

```python
import os
from nautilus_trader.config import (
    CacheConfig, DatabaseConfig, LiveDataEngineConfig,
    LiveExecEngineConfig, LoggingConfig, MessageBusConfig, TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.adapters.binance import (
    BINANCE, BinanceAccountType, BinanceDataClientConfig, BinanceExecClientConfig,
    BinanceLiveDataClientFactory, BinanceLiveExecClientFactory,
)
from nautilus_trader.adapters.binance.common.enums import BinanceKeyType

config = TradingNodeConfig(
    trader_id="CRYPTO-HFT-001",
    logging=LoggingConfig(log_level="INFO"),
    cache=CacheConfig(database=DatabaseConfig(type="redis", host="localhost", port=6379)),
    message_bus=MessageBusConfig(database=DatabaseConfig(type="redis", host="localhost", port=6379)),
    data_engine=LiveDataEngineConfig(),
    exec_engine=LiveExecEngineConfig(
        reconciliation=True,
        reconciliation_lookback_mins=1440,
    ),
    data_clients={
        BINANCE: BinanceDataClientConfig(
            api_key=os.environ["BINANCE_API_KEY"],
            api_secret=os.environ["BINANCE_API_SECRET"],
            key_type=BinanceKeyType.ED25519,  # REQUIRED — HMAC rejected by WS API session.logon
            account_type=BinanceAccountType.USDT_FUTURES,
        ),
    },
    exec_clients={
        BINANCE: BinanceExecClientConfig(
            api_key=os.environ["BINANCE_API_KEY"],
            api_secret=os.environ["BINANCE_API_SECRET"],
            key_type=BinanceKeyType.ED25519,
            account_type=BinanceAccountType.USDT_FUTURES,
        ),
    },
    timeout_connection=30.0,
    timeout_reconciliation=10.0,
    timeout_portfolio=10.0,
    timeout_disconnection=10.0,
    timeout_post_stop=5.0,
)

node = TradingNode(config=config)
node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
node.trader.add_strategy(MyStrategy(my_config))
node.build()
node.run()  # blocks until shutdown signal
```

## TradingNode vs BacktestNode

| Aspect | TradingNode | BacktestNode |
|--------|-------------|--------------|
| Clock | `LiveClock` (real time) | `TestClock` (simulated) |
| Data | Real venue streams | Historical data |
| Execution | Real venue APIs | `SimulatedExchange` |
| Event loop | Async (single loop) | Synchronous |
| Constraint | One per process | Sequential runs OK |

Same `Strategy` class works in both — adapters abstract the difference.

## State Persistence

### Redis (State Recovery)

```python
CacheConfig(database=DatabaseConfig(type="redis", host="localhost", port=6379))
```

### PostgreSQL (Audit Trail)

```python
CacheConfig(database=DatabaseConfig(
    type="postgres", host="localhost", port=5432,
    username="nautilus", password="...", database="nautilus_trading",
))
```

**Persisted**: orders (full lifecycle), positions (open/closed), accounts (balances), custom state via `on_save()`/`on_load()`.

## Reconciliation

### Startup Reconciliation

1. TradingNode connects to venue
2. `LiveExecutionEngine` calls `generate_mass_status()` on each ExecutionClient
3. Engine compares venue state vs cache state
4. Discrepancies resolved: missing fills applied, status mismatches corrected

### Continuous Reconciliation

```python
LiveExecEngineConfig(
    reconciliation=True,
    reconciliation_lookback_mins=1440,           # 24h lookback on startup
    inflight_check_interval_ms=2000,             # check in-flight orders every 2s
    open_check_interval_secs=10,                 # poll open orders every 10s (recommended 5-10s)
    open_check_lookback_mins=60,                 # never reduce below 60
    position_check_interval_secs=30,             # position discrepancy checks
)
```

### Order Resolution (Max Retries Exceeded)

| Current Status | Resolution | Rationale |
|----------------|------------|-----------|
| `SUBMITTED` | → `REJECTED` | No venue confirmation received |
| `PENDING_UPDATE` | → `CANCELED` | Modification unacknowledged |
| `PENDING_CANCEL` | → `CANCELED` | Cancel never confirmed |

Safety: "Not found" resolutions only in full-history mode. Recent order protection: 5-second buffer to prevent race condition false positives.

### Required ExecutionClient Methods

```python
async def generate_order_status_report(self, command) -> OrderStatusReport | None
async def generate_order_status_reports(self, command) -> list[OrderStatusReport]
async def generate_fill_reports(self, command) -> list[FillReport]
async def generate_position_status_reports(self, command) -> list[PositionStatusReport]
async def generate_mass_status(self, lookback_mins=None) -> ExecutionMassStatus | None
```

## Memory Management

Long-running sessions need periodic purging:

```python
# purge params are in MINUTES (not seconds) in v1.224.0
LiveExecEngineConfig(
    purge_closed_orders_interval_mins=10,
    purge_closed_orders_buffer_mins=60,         # retention before purge
    purge_closed_positions_interval_mins=10,
    purge_closed_positions_buffer_mins=60,
    purge_account_events_interval_mins=15,
    purge_account_events_lookback_mins=60,
    purge_from_database=False,                  # True to also purge from DB
)
```

## External Order Claims

Declare instruments whose external orders (placed outside Nautilus) should be tracked:

```python
class MyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    external_order_claims: list[str] = ["BTCUSDT-PERP.BINANCE"]
```

External orders appear via `on_order_accepted`, `on_order_filled`, etc.

## Multi-Venue Trading

```python
config = TradingNodeConfig(
    data_clients={
        "BINANCE": BinanceDataClientConfig(...),
        "BYBIT": BybitDataClientConfig(...),
    },
    exec_clients={
        "BINANCE": BinanceExecClientConfig(...),
        "BYBIT": BybitExecClientConfig(...),
    },
)
```

Strategies subscribe to data from any venue and submit orders to any venue via `instrument_id` routing.

## Error Handling

### WebSocket Reconnection

1. Detect disconnect (read error or ping timeout)
2. Exponential backoff: 1s → 2s → 4s → 8s → max 60s
3. Re-authenticate
4. Re-subscribe all active subscriptions
5. Request snapshot for order books

### Strategy-Level

```python
def on_order_rejected(self, event) -> None:
    self.log.warning(f"Order rejected: {event.reason}")

def on_order_modify_rejected(self, event) -> None:
    self.log.warning(f"Modify rejected: {event.reason}")
    # Fall back to cancel + re-submit

def on_order_cancel_rejected(self, event) -> None:
    self.log.error(f"Cancel rejected: {event.reason}")
    # Query order status
```

## Deployment

### Standalone Script (Recommended)

```python
def main():
    config = TradingNodeConfig(...)
    node = TradingNode(config)
    node.add_data_client_factory(...)
    node.add_exec_client_factory(...)
    node.trader.add_strategy(MyStrategy(my_config))
    node.build()
    try:
        node.run()
    except KeyboardInterrupt:
        node.stop()

if __name__ == "__main__":
    main()
```

### Docker

```dockerfile
FROM python:3.12-slim
RUN pip install nautilus_trader
COPY strategies/ /app/strategies/
COPY run_trading.py /app/
WORKDIR /app
CMD ["python", "run_trading.py"]
```

**NO Jupyter notebooks** for live trading: event loop conflicts, no signal handling, risk of accidental re-execution.

## Logging

```python
LoggingConfig(
    log_level="INFO",
    log_level_file="DEBUG",
    log_directory="logs/",
)
```

Implemented in Rust for performance. Nanosecond timestamps, structured key-value pairs.

## MessageBus External Streaming

```python
MessageBusConfig(
    database=DatabaseConfig(type="redis", host="localhost", port=6379),
    use_instance_id=True,
    streams_prefix="trader",
)
```

External systems consume Nautilus events via Redis streams.

## Operational Checklist

- [ ] State persistence configured (Redis or PostgreSQL)?
- [ ] Reconciliation enabled with lookback?
- [ ] Continuous reconciliation intervals set?
- [ ] Memory purge configured for long sessions?
- [ ] API keys in environment variables (not hardcoded)?
- [ ] Logging with file output?
- [ ] Running as standalone script (not notebook)?
- [ ] Signal handling for graceful shutdown?
- [ ] Separate testnet validation before mainnet?

For detailed error recovery, circuit breakers, reconnection handling, and production monitoring patterns, see [operational_patterns.md](operational_patterns.md).

## Binance Key Type Requirements (Verified with Real Trades)

**Ed25519 keys are REQUIRED** for the Binance exec client WS API (`/ws-api/v3`). The `session.logon` endpoint rejects HMAC-SHA-256 keys.

| Key Type | Data Client | Exec Client (Spot) | Exec Client (Futures) |
|----------|-------------|--------------------|-----------------------|
| HMAC | Works | **FAILS** — `session.logon` rejects | Works (REST listenKey fallback) |
| Ed25519 | Works | **Works** | **Works** |

```python
from nautilus_trader.adapters.binance.common.enums import BinanceKeyType
# BinanceKeyType.HMAC, BinanceKeyType.RSA, BinanceKeyType.ED25519

# MUST set key_type on BOTH data and exec configs:
BinanceDataClientConfig(api_key=key, api_secret=secret, key_type=BinanceKeyType.ED25519, ...)
BinanceExecClientConfig(api_key=key, api_secret=secret, key_type=BinanceKeyType.ED25519, ...)
```

**Ed25519 key format**: The private key MUST be unencrypted PKCS#8 (48 bytes base64, starts with `MC4CAQAw`). Encrypted (PBES2) keys fail with `-1022 invalid signature`. Decrypt with: `openssl pkey -in encrypted.pem -out decrypted.pem`

### HMAC Fallback Behavior

The adapter's `_use_rest_listen_key` property returns `True` for futures + non-Ed25519. This means:
- **Futures + HMAC**: adapter skips WS API entirely, uses REST `POST /fapi/v1/listenKey` — data works, but no WS order API
- **Spot + HMAC**: NO fallback — `session.logon` fails, exec client never connects, orders fail with "no execution client found"

## Min Notional Requirements (Verified)

| Pair | Market | Min Notional | Min Qty | Step | Example |
|------|--------|-------------|---------|------|---------|
| ETHUSDT | Spot | 5 USDT | 0.0001 ETH | 0.0001 | 0.005 ETH × $2075 = $10.38 ✓ |
| ETHUSDT | Futures | **20 USDT** | 0.001 ETH | 0.001 | 0.010 ETH × $2075 = $20.75 ✓ |

RiskEngine also checks `NOTIONAL_EXCEEDS_FREE_BALANCE` for spot — limit order notional must be ≤ free balance.

### Futures Leverage

New Binance Futures accounts default to **1x leverage**. Must set via API before trading:

```python
# POST /fapi/v1/leverage
# Returns: {"symbol": "ETHUSDT", "leverage": 10, "maxNotionalValue": "150000000"}
```

At 10x leverage, 0.010 ETH ($20.75) requires only ~$2.08 margin.

### Internal Transfers

```python
# Spot → USDT-M Futures: POST /sapi/v1/asset/transfer
# type=MAIN_UMFUTURE, asset=USDT, amount=15
```

## Order Lifecycle (Verified with Real Trades)

**Market order (Spot)**: submit → OrderSubmitted → OrderAccepted → OrderFilled
**Market order (Futures)**: submit → OrderSubmitted → OrderFilled (possibly multiple partial fills)
**Limit order (Futures)**: submit → Submitted → Accepted → (modify) → PendingUpdate → Updated → (cancel) → PendingCancel → Canceled

Partial fills are normal: a 0.010 ETH market order on futures produced 2 fills (0.003 + 0.007). Position events: PositionOpened → PositionChanged (on second fill) → PositionClosed (on close).
