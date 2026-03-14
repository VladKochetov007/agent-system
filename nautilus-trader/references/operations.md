# Operations

Clock, timers, graceful shutdown, error recovery, circuit breakers, reconnection handling, logging, and monitoring for NautilusTrader.

## Clock API

Every `Strategy` and `Actor` has `self.clock` — `BacktestClock` in backtest, `LiveClock` in live. Same interface, different time sources.

| Method | Returns | Notes |
|--------|---------|-------|
| `utc_now()` | pandas Timestamp (tz-aware UTC) | |
| `timestamp_ns()` | int | Nanosecond epoch |
| `timestamp_ms()` | int | Millisecond epoch |
| `timestamp_us()` | int | Microsecond epoch |
| `timestamp()` | float | Seconds epoch |
| `local_now(tz)` | pandas Timestamp | Local timezone |

## Recurring Timer

Fires repeatedly at fixed interval until canceled.

```python
from datetime import timedelta

def on_start(self) -> None:
    self.clock.set_timer(
        "rebalance",                               # unique name
        interval=timedelta(seconds=10),            # firing interval
        callback=self._on_rebalance,               # handler
    )

def _on_rebalance(self, event) -> None:
    # event is a TimeEvent with: name, ts_event, ts_init
    positions = self.cache.positions_open(instrument_id=self.instrument_id)
```

**Full signature**:
```python
clock.set_timer(
    name,                    # str — unique timer name
    interval,                # timedelta — firing interval
    start_time=None,         # Optional[datetime] — when to start (default: now)
    stop_time=None,          # Optional[datetime] — when to stop (default: never)
    callback=None,           # Callable[[TimeEvent], None] — REQUIRED in practice
    allow_past=True,         # bool — allow start_time in the past
    fire_immediately=False,  # bool — fire once immediately on creation
)
```

## One-Shot Time Alert

Fires once at a specific time, then auto-removes.

```python
self.clock.set_time_alert(
    "exit_funding",
    alert_time=self.clock.utc_now() + timedelta(seconds=30),
    callback=self._on_exit_funding,
)
```

**Full signature**:
```python
clock.set_time_alert(
    name,                    # str — unique alert name
    alert_time,              # datetime — when to fire
    callback=None,           # Callable[[TimeEvent], None]
    override=False,          # bool — overwrite existing alert with same name
    allow_past=True,         # bool — allow alert_time in the past
)
```

## Cancel Timer

```python
self.clock.cancel_timer("rebalance")   # cancel specific timer
self.clock.cancel_timers()             # cancel ALL timers
```

## Timer Anti-Patterns

| Wrong | Right |
|-------|-------|
| `def on_timer(self, event)` | Doesn't exist. Use `clock.set_timer(callback=handler)` |
| `time.sleep(5)` in callback | Blocks the entire event loop. Use `set_time_alert` instead |
| Timer without callback | Callback required — without it, timer fires but nothing happens |
| Multiple timers with same name | Raises error. Cancel first or use `override=True` on alerts |

## Timer Patterns

### Periodic Position Check

```python
def on_start(self) -> None:
    self.clock.set_timer(
        "position_check", interval=timedelta(seconds=5),
        callback=self._check_positions,
    )

def _check_positions(self, event) -> None:
    positions = self.cache.positions_open(instrument_id=self.config.instrument_id)
    if not positions:
        return
    pos = positions[0]
    pnl = pos.unrealized_pnl(self.cache.quote_tick(self.config.instrument_id).bid_price)
    if float(pnl) < -100:
        self.close_position(pos)
```

### Stale Order Cleanup

```python
def on_start(self) -> None:
    self.clock.set_timer(
        "stale_cleanup", interval=timedelta(seconds=30),
        callback=self._cleanup_stale_orders,
    )

def _cleanup_stale_orders(self, event) -> None:
    now_ns = self.clock.timestamp_ns()
    for order in self.cache.orders_open(instrument_id=self.config.instrument_id):
        age_secs = (now_ns - order.ts_init) / 1_000_000_000
        if age_secs > 60:
            self.cancel_order(order)
```

### Funding Rate Window

```python
def on_start(self) -> None:
    self.clock.set_timer(
        "funding_check", interval=timedelta(minutes=1),
        callback=self._check_funding_window,
    )

def _check_funding_window(self, event) -> None:
    now = self.clock.utc_now()
    hour, minute = now.hour, now.minute
    for fh in (0, 8, 16):
        minutes_until = ((fh - hour) % 8) * 60 - minute
        if 0 < minutes_until <= 5:
            self._enter_funding_trade()
            break
```

### Data Collection with Status Reporting

```python
def on_start(self) -> None:
    self.start_time = self.clock.timestamp()
    self.clock.set_timer(
        "status", interval=timedelta(seconds=5),
        callback=self._report_status,
    )
    self.clock.set_timer(
        "shutdown", interval=timedelta(seconds=60),
        callback=self._shutdown,
    )

def _report_status(self, event) -> None:
    elapsed = self.clock.timestamp() - self.start_time
    self.log.info(f"[{elapsed:.0f}s] Events: {self.event_count}")
```

### Programmatic Shutdown

Stop the node from within a strategy (for timed runs, error conditions):

```python
import os
import signal as sig

def _initiate_shutdown(self) -> None:
    self.cancel_all_orders(self.config.instrument_id)
    self.close_all_positions(self.config.instrument_id)
    self.clock.set_time_alert(
        "kill",
        alert_time=self.clock.utc_now() + timedelta(seconds=2),
        callback=lambda _: os.kill(os.getpid(), sig.SIGINT),
    )
```

The 2-second delay gives fills time to arrive before the node shuts down.

## Backtest vs Live Clock

| Aspect | Backtest | Live |
|--------|----------|------|
| Time source | Data timestamps (simulated) | System clock (real) |
| Timer precision | Exact — fires at scheduled data time | Approximate — async event loop |
| `utc_now()` | Advances with data replay | Real UTC time |
| `fire_immediately` | Fires at current data time | Fires immediately on creation |
| `set_time_alert` | Fires when data time passes alert time | Fires at real wall-clock time |

## Graceful Shutdown

`on_stop()` is called when the node receives SIGINT or `node.stop()` is called.

```python
def on_stop(self) -> None:
    self.cancel_all_orders(self.config.instrument_id)
    self.close_all_positions(self.config.instrument_id)
```

For multi-instrument strategies:
```python
def on_stop(self) -> None:
    for inst_id in self.instrument_ids:
        try:
            self.cancel_all_orders(inst_id)
            self.close_all_positions(inst_id)
        except Exception as e:
            self.log.error(f"Cleanup error for {inst_id}: {e}")
```

`close_all_positions` submits market orders with `reduce_only=True`. Fills may arrive after `on_stop` returns.

## Error Recovery

### Order Rejection

```python
def on_order_rejected(self, event) -> None:
    self.log.warning(f"Order rejected: {event.reason}")
    if event.client_order_id == self._bid_id:
        self._bid_id = None
    elif event.client_order_id == self._ask_id:
        self._ask_id = None
```

### Modify Rejection

```python
def on_order_modify_rejected(self, event) -> None:
    self.log.warning(f"Modify rejected: {event.reason}")
    order = self.cache.order(event.client_order_id)
    if order and order.is_open:
        self.cancel_order(order)
```

### Cancel Rejection

```python
def on_order_cancel_rejected(self, event) -> None:
    self.log.warning(f"Cancel rejected: {event.reason}")
    order = self.cache.order(event.client_order_id)
    if order and order.is_closed:
        self.log.info("Order already closed, nothing to do")
```

### Denial (Pre-Trade)

```python
def on_order_denied(self, event) -> None:
    self.log.error(f"Order denied by RiskEngine: {event.reason}")
    # Likely: precision error, notional limit, rate limit
    # Do NOT retry immediately — fix the root cause
```

## Drawdown Circuit Breaker

```python
class CircuitBreakerMixin:
    def _init_circuit_breaker(self, max_drawdown_usdt: float = 100.0):
        self._max_drawdown = max_drawdown_usdt
        self._halted = False
        self.clock.set_timer(
            "pnl_check", interval=timedelta(seconds=5),
            callback=self._check_pnl,
        )

    def _check_pnl(self, event) -> None:
        if self._halted:
            return
        pnls = self.portfolio.unrealized_pnls(self.config.instrument_id.venue)
        if pnls:
            total = sum(float(v) for v in pnls.values())
            if total < -self._max_drawdown:
                self._halted = True
                self.log.error(f"CIRCUIT BREAKER: PnL={total:.2f}, halting")
                self.cancel_all_orders(self.config.instrument_id)
                self.close_all_positions(self.config.instrument_id)
```

## Stale Book Detection

After WebSocket reconnection, the order book may be stale until a fresh snapshot arrives:

```python
def on_order_book_deltas(self, deltas) -> None:
    book = self.cache.order_book(self.config.instrument_id)
    if not book.best_bid_price() or not book.best_ask_price():
        return

    spread = float(book.spread())
    mid = float(book.midpoint())
    spread_bps = (spread / mid) * 10_000 if mid > 0 else float('inf')

    if spread_bps > 50:
        self.log.warning(f"Wide spread {spread_bps:.1f}bps — possible stale book")
        return
```

## Reconnection Handling

WebSocket reconnection is handled automatically by adapters. Strategy-level considerations:

1. **Order book**: Adapter requests fresh snapshot on reconnect. Book rebuilds transparently.
2. **Open orders**: Reconciliation queries venue for open orders on reconnect.
3. **Positions**: Reconciliation verifies position state matches venue.
4. **Data gaps**: Ticks/deltas during disconnect are lost. No backfill.

```python
def on_order_book_deltas(self, deltas) -> None:
    book = self.cache.order_book(self.config.instrument_id)
    if book.update_count == 0:
        return  # book empty — awaiting snapshot after reconnect
```

## Logging

```python
from nautilus_trader.config import LoggingConfig

logging = LoggingConfig(
    log_level="INFO",          # DEBUG is extremely verbose
    log_level_file="DEBUG",    # full debug to file only
    log_directory="/var/log/nautilus/",
    log_file_format=None,      # default format
    log_colors=False,          # no ANSI in files
    bypass_logging=False,      # never bypass in production
)
```

Implemented in Rust for performance. Nanosecond timestamps, structured key-value pairs.

Strategy logging:
```python
self.log.info("Normal operation")
self.log.warning("Recoverable issue")
self.log.error("Needs attention")
self.log.debug("Verbose detail")  # only visible at DEBUG level
```

## Health Monitoring via External Streaming

Stream MessageBus events to Redis/Kafka for external monitoring:

```python
from nautilus_trader.config import DatabaseConfig, MessageBusConfig

msgbus_config = MessageBusConfig(
    database=DatabaseConfig(type="redis", host="localhost", port=6379),
    external_streams=["data.*", "events.order.*", "events.position.*"],
)
```
