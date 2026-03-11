"""
Live Binance USDT-M Futures trade: market LONG 0.005 ETHUSDT-PERP → wait → market CLOSE.
Validates full perp order lifecycle: submit → accept → fill → position open → close.
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

INSTRUMENT = InstrumentId.from_str("ETHUSDT-PERP.BINANCE")
TRADE_QTY = Decimal("0.010")


class FuturesTradeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    trade_qty: Decimal = TRADE_QTY


class FuturesTradeStrategy(Strategy):
    def __init__(self, config: FuturesTradeConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self.long_filled = False
        self.close_filled = False
        self.entry_price = None
        self.exit_price = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if not self.instrument:
            self.log.error(f"Instrument {self.config.instrument_id} not found in cache")
            return

        self.subscribe_trade_ticks(self.config.instrument_id)
        self.log.info(f"Subscribed to {self.config.instrument_id}, waiting 5s before long entry...")

        self.clock.set_time_alert(
            name="submit_long",
            alert_time=self.clock.utc_now() + timedelta(seconds=5),
            callback=self._submit_long,
        )
        self.clock.set_time_alert(
            name="kill_switch",
            alert_time=self.clock.utc_now() + timedelta(seconds=60),
            callback=self._kill,
        )

    def _submit_long(self, event) -> None:
        qty = self.instrument.make_qty(self.config.trade_qty)
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info(f"LONG order submitted: {qty} {self.config.instrument_id}")

    def _submit_close(self, event) -> None:
        qty = self.instrument.make_qty(self.config.trade_qty)
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=qty,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info(f"CLOSE order submitted: {qty} {self.config.instrument_id}")

    def on_order_filled(self, event) -> None:
        side = event.order_side
        price = event.last_px
        qty = event.last_qty
        self.log.info(f"FILLED: {side} {qty} @ {price}")

        if side == OrderSide.BUY and not self.long_filled:
            self.long_filled = True
            self.entry_price = float(price)
            self.log.info("LONG filled, waiting 3s before closing...")
            self.clock.set_time_alert(
                name="submit_close",
                alert_time=self.clock.utc_now() + timedelta(seconds=3),
                callback=self._submit_close,
            )

        elif side == OrderSide.SELL and not self.close_filled:
            self.close_filled = True
            self.exit_price = float(price)
            pnl = (self.exit_price - self.entry_price) * float(qty)
            self.log.info(f"ROUND TRIP COMPLETE: entry={self.entry_price} exit={self.exit_price} pnl={pnl:.4f} USDT")
            self.clock.set_time_alert(
                name="shutdown",
                alert_time=self.clock.utc_now() + timedelta(seconds=2),
                callback=self._kill,
            )

    def on_order_rejected(self, event) -> None:
        self.log.error(f"ORDER REJECTED: {event.reason}")

    def on_order_canceled(self, event) -> None:
        self.log.warning(f"ORDER CANCELED: {event}")

    def _kill(self, event) -> None:
        status = "SUCCESS" if self.close_filled else ("PARTIAL" if self.long_filled else "NO FILLS")
        self.log.info(f"Shutting down. Status: {status}")
        os.kill(os.getpid(), signal.SIGINT)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)


def main():
    # Ed25519 keys required for WS API session.logon
    api_key = os.environ.get("BINANCE_SPOT_ED25519")
    api_secret = os.environ.get("BINANCE_SPOT_ED25519_SECRET")
    if not api_key or not api_secret:
        print("ERROR: BINANCE_SPOT_ED25519 / BINANCE_SPOT_ED25519_SECRET not set")
        return

    key_type = BinanceKeyType.ED25519

    provider = InstrumentProviderConfig(load_all=False, load_ids=frozenset({INSTRUMENT}))

    config = TradingNodeConfig(
        trader_id="FUTURES-TRADE-001",
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=10),
        data_clients={
            BINANCE: BinanceDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                key_type=key_type,
                account_type=BinanceAccountType.USDT_FUTURES,
                instrument_provider=provider,
            ),
        },
        exec_clients={
            BINANCE: BinanceExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                key_type=key_type,
                account_type=BinanceAccountType.USDT_FUTURES,
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

    strategy = FuturesTradeStrategy(FuturesTradeConfig(instrument_id=INSTRUMENT, trade_qty=TRADE_QTY))
    node.trader.add_strategy(strategy)
    node.build()

    print(f"Starting live futures trade: LONG {TRADE_QTY} ETHUSDT-PERP -> CLOSE")
    print(f"Risk: ~${float(TRADE_QTY) * 2075:.0f} notional, margin ~$1 at 20x")
    print("=" * 60)

    try:
        node.run()
    except KeyboardInterrupt:
        node.stop()

    print("=" * 60)
    print(f"Long filled: {strategy.long_filled} (price: {strategy.entry_price})")
    print(f"Close filled: {strategy.close_filled} (price: {strategy.exit_price})")
    if strategy.entry_price and strategy.exit_price:
        pnl = (strategy.exit_price - strategy.entry_price) * float(TRADE_QTY)
        print(f"Net P&L: {pnl:.4f} USDT (before fees)")


if __name__ == "__main__":
    main()
