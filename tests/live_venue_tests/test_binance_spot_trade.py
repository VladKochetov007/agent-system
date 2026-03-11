"""
Live Binance Spot trade: market BUY 0.005 ETHUSDT → wait → market SELL back.
Validates full order lifecycle: submit → accept → fill.
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
    ActorConfig, InstrumentProviderConfig, LiveExecEngineConfig,
    LoggingConfig, StrategyConfig, TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from datetime import timedelta

INSTRUMENT = InstrumentId.from_str("ETHUSDT.BINANCE")
TRADE_QTY = Decimal("0.005")


class SpotTradeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    trade_qty: Decimal = TRADE_QTY


class SpotTradeStrategy(Strategy):
    def __init__(self, config: SpotTradeConfig) -> None:
        super().__init__(config)
        self.instrument = None
        self.buy_filled = False
        self.sell_filled = False
        self.buy_price = None
        self.sell_price = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if not self.instrument:
            self.log.error(f"Instrument {self.config.instrument_id} not found in cache")
            return

        self.subscribe_trade_ticks(self.config.instrument_id)
        self.log.info(f"Subscribed to {self.config.instrument_id}, waiting 5s before buy...")

        self.clock.set_time_alert(
            name="submit_buy",
            alert_time=self.clock.utc_now() + timedelta(seconds=5),
            callback=self._submit_buy,
        )
        self.clock.set_time_alert(
            name="kill_switch",
            alert_time=self.clock.utc_now() + timedelta(seconds=60),
            callback=self._kill,
        )

    def _submit_buy(self, event) -> None:
        qty = self.instrument.make_qty(self.config.trade_qty)
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        self.log.info(f"BUY order submitted: {qty} {self.config.instrument_id}")

    def _submit_sell(self, event) -> None:
        qty = self.instrument.make_qty(self.config.trade_qty)
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=qty,
            time_in_force=TimeInForce.IOC,
        )
        self.submit_order(order)
        self.log.info(f"SELL order submitted: {qty} {self.config.instrument_id}")

    def on_order_filled(self, event) -> None:
        side = event.order_side
        price = event.last_px
        qty = event.last_qty
        self.log.info(f"FILLED: {side} {qty} @ {price}")

        if side == OrderSide.BUY and not self.buy_filled:
            self.buy_filled = True
            self.buy_price = float(price)
            self.log.info("BUY filled, waiting 3s before sell-back...")
            self.clock.set_time_alert(
                name="submit_sell",
                alert_time=self.clock.utc_now() + timedelta(seconds=3),
                callback=self._submit_sell,
            )

        elif side == OrderSide.SELL and not self.sell_filled:
            self.sell_filled = True
            self.sell_price = float(price)
            pnl = (self.sell_price - self.buy_price) * float(qty)
            self.log.info(f"ROUND TRIP COMPLETE: buy={self.buy_price} sell={self.sell_price} pnl={pnl:.4f} USDT")
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
        status = "SUCCESS" if self.sell_filled else ("PARTIAL" if self.buy_filled else "NO FILLS")
        self.log.info(f"Shutting down. Status: {status}")
        os.kill(os.getpid(), signal.SIGINT)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)


def main():
    # Ed25519 keys required — Binance WS API session.logon rejects HMAC
    api_key = os.environ.get("BINANCE_SPOT_ED25519")
    api_secret = os.environ.get("BINANCE_SPOT_ED25519_SECRET")
    if not api_key or not api_secret:
        print("ERROR: BINANCE_SPOT_ED25519 / BINANCE_SPOT_ED25519_SECRET not set")
        return

    key_type = BinanceKeyType.ED25519

    provider = InstrumentProviderConfig(load_all=False, load_ids=frozenset({INSTRUMENT}))

    config = TradingNodeConfig(
        trader_id="SPOT-TRADE-001",
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=10),
        data_clients={
            BINANCE: BinanceDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                key_type=key_type,
                account_type=BinanceAccountType.SPOT,
                instrument_provider=provider,
            ),
        },
        exec_clients={
            BINANCE: BinanceExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                key_type=key_type,
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

    strategy = SpotTradeStrategy(SpotTradeConfig(instrument_id=INSTRUMENT, trade_qty=TRADE_QTY))
    node.trader.add_strategy(strategy)
    node.build()

    print(f"Starting live trade: BUY {TRADE_QTY} ETHUSDT → SELL back")
    print(f"Risk: ~${float(TRADE_QTY) * 2068:.0f} notional, ~$0.02 fees")
    print("=" * 60)

    try:
        node.run()
    except KeyboardInterrupt:
        node.stop()

    print("=" * 60)
    print(f"Buy filled: {strategy.buy_filled} (price: {strategy.buy_price})")
    print(f"Sell filled: {strategy.sell_filled} (price: {strategy.sell_price})")
    if strategy.buy_price and strategy.sell_price:
        pnl = (strategy.sell_price - strategy.buy_price) * float(TRADE_QTY)
        print(f"Net P&L: {pnl:.4f} USDT (before fees)")


if __name__ == "__main__":
    main()
