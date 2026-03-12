"""
Live Binance Spot limit order test: place → modify → cancel.
Validates order lifecycle on SPOT account.
No fills expected — limit price placed 2% below market.
"""

import os
import signal
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

from nautilus_trader.adapters.binance import (
    BINANCE, BinanceAccountType, BinanceDataClientConfig,
    BinanceExecClientConfig, BinanceLiveDataClientFactory, BinanceLiveExecClientFactory,
)
from nautilus_trader.adapters.binance.common.enums import BinanceKeyType
from nautilus_trader.config import (
    InstrumentProviderConfig, LiveExecEngineConfig,
    LoggingConfig, StrategyConfig, TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from datetime import timedelta

INSTRUMENT = InstrumentId.from_str("ETHUSDT.BINANCE")
TRADE_QTY = Decimal("0.0026")  # ~$5.10 notional at 2% below market, fits within ~$5.25 spot balance


class SpotLimitConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    trade_qty: Decimal = TRADE_QTY


class SpotLimitStrategy(Strategy):
    def __init__(self, config: SpotLimitConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self.limit_order = None
        self.accepted = False
        self.modified = False
        self.canceled = False

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if not self.instrument:
            self.log.error(f"Instrument {self.config.instrument_id} not found in cache")
            return

        self.subscribe_quote_ticks(self.config.instrument_id)
        self.log.info(f"Subscribed to {self.config.instrument_id}, waiting 5s before limit order...")

        self.clock.set_time_alert(
            name="submit_limit",
            alert_time=self.clock.utc_now() + timedelta(seconds=5),
            callback=self._submit_limit,
        )
        self.clock.set_time_alert(
            name="kill_switch",
            alert_time=self.clock.utc_now() + timedelta(seconds=60),
            callback=self._kill,
        )

    def _submit_limit(self, event) -> None:
        quotes = self.cache.quote_ticks(self.config.instrument_id)
        if quotes:
            best_ask = float(quotes[0].ask_price)
        else:
            book = self.cache.order_book(self.config.instrument_id)
            if book and book.best_ask_price():
                best_ask = float(book.best_ask_price())
            else:
                self.log.error("No market data to determine price")
                return

        # 2% below market — far enough to not fill
        limit_price = self.instrument.make_price(Decimal(str(best_ask * 0.98)))
        qty = self.instrument.make_qty(self.config.trade_qty)

        notional = float(limit_price) * float(qty)
        self.log.info(f"Notional check: {notional:.2f} USDT (min=5, balance=~5.25)")

        self.limit_order = self.order_factory.limit(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
            price=limit_price,
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        self.submit_order(self.limit_order)
        self.log.info(f"LIMIT BUY submitted: {qty} @ {limit_price} (market ~{best_ask:.2f})")

    def on_order_accepted(self, event) -> None:
        self.log.info(f"ORDER ACCEPTED: {event.client_order_id}")
        if not self.accepted:
            self.accepted = True
            self.clock.set_time_alert(
                name="modify_order",
                alert_time=self.clock.utc_now() + timedelta(seconds=3),
                callback=self._modify_order,
            )

    def _modify_order(self, event) -> None:
        if self.limit_order is None or not self.limit_order.is_open:
            self.log.warning("No open order to modify")
            return

        old_price = float(self.limit_order.price)
        new_price = self.instrument.make_price(Decimal(str(old_price * 0.99)))
        self.modify_order(self.limit_order, price=new_price)
        self.log.info(f"MODIFY submitted: {old_price:.2f} → {new_price}")

    def on_order_updated(self, event) -> None:
        self.log.info(f"ORDER UPDATED: price={event.price}, qty={event.quantity}")
        if not self.modified:
            self.modified = True
            self.clock.set_time_alert(
                name="cancel_order",
                alert_time=self.clock.utc_now() + timedelta(seconds=3),
                callback=self._cancel_order,
            )

    def _cancel_order(self, event) -> None:
        if self.limit_order is None or not self.limit_order.is_open:
            self.log.warning("No open order to cancel")
            return
        self.cancel_order(self.limit_order)
        self.log.info("CANCEL submitted")

    def on_order_canceled(self, event) -> None:
        self.log.info(f"ORDER CANCELED: {event.client_order_id}")
        self.canceled = True
        self.clock.set_time_alert(
            name="shutdown",
            alert_time=self.clock.utc_now() + timedelta(seconds=2),
            callback=self._kill,
        )

    def on_order_denied(self, event) -> None:
        self.log.error(f"ORDER DENIED: {event.reason}")
        self.clock.set_time_alert(
            name="shutdown_denied",
            alert_time=self.clock.utc_now() + timedelta(seconds=2),
            callback=self._kill,
        )

    def on_order_modify_rejected(self, event) -> None:
        self.log.error(f"MODIFY REJECTED: {event.reason}")
        self.clock.set_time_alert(
            name="cancel_after_reject",
            alert_time=self.clock.utc_now() + timedelta(seconds=2),
            callback=self._cancel_order,
        )

    def on_order_cancel_rejected(self, event) -> None:
        self.log.error(f"CANCEL REJECTED: {event.reason}")

    def on_order_filled(self, event) -> None:
        self.log.warning(f"UNEXPECTED FILL: {event.order_side} {event.last_qty} @ {event.last_px}")

    def on_order_rejected(self, event) -> None:
        self.log.error(f"ORDER REJECTED: {event.reason}")

    def _kill(self, event) -> None:
        parts = []
        if self.accepted:
            parts.append("ACCEPTED")
        if self.modified:
            parts.append("MODIFIED")
        if self.canceled:
            parts.append("CANCELED")
        status = " → ".join(parts) if parts else "NO EVENTS"
        self.log.info(f"Shutting down. Lifecycle: {status}")
        os.kill(os.getpid(), signal.SIGINT)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)


def main():
    api_key = os.environ.get("BINANCE_SPOT_ED25519")
    api_secret = os.environ.get("BINANCE_SPOT_ED25519_SECRET")
    if not api_key or not api_secret:
        print("ERROR: BINANCE_SPOT_ED25519 / BINANCE_SPOT_ED25519_SECRET not set")
        return

    provider = InstrumentProviderConfig(load_all=False, load_ids=frozenset({INSTRUMENT}))

    config = TradingNodeConfig(
        trader_id="SPOT-LIMIT-001",
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=10),
        data_clients={
            BINANCE: BinanceDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                key_type=BinanceKeyType.ED25519,
                account_type=BinanceAccountType.SPOT,
                instrument_provider=provider,
            ),
        },
        exec_clients={
            BINANCE: BinanceExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                key_type=BinanceKeyType.ED25519,
                account_type=BinanceAccountType.SPOT,
                instrument_provider=provider,
            ),
        },
        timeout_connection=45.0,
        timeout_reconciliation=10.0,
        timeout_portfolio=10.0,
        timeout_disconnection=5.0,
        timeout_post_stop=3.0,
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
    node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)

    strategy = SpotLimitStrategy(SpotLimitConfig(instrument_id=INSTRUMENT, trade_qty=TRADE_QTY))
    node.trader.add_strategy(strategy)
    node.build()

    print(f"Starting Spot limit order test: place → modify → cancel")
    print(f"Qty: {TRADE_QTY} ETHUSDT, price 2% below market (should NOT fill)")
    print(f"Spot balance: ~5.25 USDT")
    print("=" * 60)

    try:
        node.run()
    except KeyboardInterrupt:
        node.stop()

    print("=" * 60)
    print(f"Accepted: {strategy.accepted}")
    print(f"Modified: {strategy.modified}")
    print(f"Canceled: {strategy.canceled}")
    success = strategy.accepted and strategy.modified and strategy.canceled
    print(f"Result: {'SUCCESS — full lifecycle' if success else 'INCOMPLETE'}")


if __name__ == "__main__":
    main()
