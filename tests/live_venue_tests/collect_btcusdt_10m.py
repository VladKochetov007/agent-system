"""
Collect 10 minutes of BTCUSDT-PERP trades and L2 order book deltas from Binance Futures.
Saves to ParquetDataCatalog at data/btcusdt_10m/ for backtesting.

Usage:
    python tests/live_venue_tests/collect_btcusdt_10m.py
"""

import os
import signal
import shutil
import time
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveDataEngineConfig,
    LoggingConfig,
    StrategyConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import OrderBookDeltas, TradeTick
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.trading.strategy import Strategy

INSTRUMENT = "BTCUSDT-PERP.BINANCE"
COLLECTION_SECONDS = 600  # 10 minutes
CATALOG_PATH = PROJECT_ROOT / "data" / "btcusdt_10m"


class CollectorConfig(StrategyConfig, frozen=True):
    instrument_str: str = INSTRUMENT


class CollectorStrategy(Strategy):
    def __init__(self, config: CollectorConfig) -> None:
        super().__init__(config)
        self.inst_id = InstrumentId.from_str(config.instrument_str)
        self.trades: list[TradeTick] = []
        self.deltas: list = []
        self.start_time = None
        self._stopped = False

    def on_start(self) -> None:
        inst = self.cache.instrument(self.inst_id)
        if inst is None:
            self.log.error(f"Instrument not found: {self.inst_id}")
            return

        self.start_time = time.time()
        self.subscribe_trade_ticks(self.inst_id)
        self.subscribe_order_book_deltas(self.inst_id, book_type=BookType.L2_MBP, depth=10)

        self.clock.set_timer(
            "shutdown",
            interval=timedelta(seconds=COLLECTION_SECONDS),
            callback=self._on_shutdown,
        )
        self.clock.set_timer(
            "status",
            interval=timedelta(seconds=30),
            callback=self._on_status,
        )
        self.log.info(f"Collecting {INSTRUMENT} for {COLLECTION_SECONDS}s...")

    def on_trade_tick(self, tick: TradeTick) -> None:
        self.trades.append(tick)

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        self.deltas.append(deltas)

    def _on_status(self, event) -> None:
        elapsed = time.time() - self.start_time
        remaining = COLLECTION_SECONDS - elapsed
        self.log.info(
            f"[{elapsed:.0f}s] trades={len(self.trades):,} "
            f"delta_batches={len(self.deltas):,} "
            f"remaining={remaining:.0f}s"
        )

    def _on_shutdown(self, event) -> None:
        if self._stopped:
            return
        self._stopped = True
        elapsed = time.time() - self.start_time
        print(f"\n{'='*60}")
        print(f"COLLECTION COMPLETE ({elapsed:.1f}s)")
        print(f"  Trades:        {len(self.trades):,}")
        print(f"  Delta batches: {len(self.deltas):,}")

        book = self.cache.order_book(self.inst_id)
        if book and book.best_bid_price():
            spread_bps = (float(book.best_ask_price()) - float(book.best_bid_price())) / float(book.midpoint()) * 10000
            print(f"  Last book:     bid={book.best_bid_price()} ask={book.best_ask_price()} spread={spread_bps:.2f}bps")

        if self.trades:
            t_rate = len(self.trades) / elapsed
            print(f"  Trade rate:    {t_rate:.1f}/s")
        if self.deltas:
            d_rate = len(self.deltas) / elapsed
            print(f"  Delta rate:    {d_rate:.1f}/s")
        print(f"{'='*60}\n")

        os.kill(os.getpid(), signal.SIGINT)


def save_to_catalog(node: TradingNode, strategy: CollectorStrategy):
    print("--- SAVING TO CATALOG ---")
    if CATALOG_PATH.exists():
        shutil.rmtree(CATALOG_PATH)

    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    # Instruments
    instruments = node.cache.instruments()
    if instruments:
        catalog.write_data(instruments)
        print(f"  Instruments: {len(instruments)}")

    # Trade ticks
    trades = strategy.trades
    if trades:
        trades.sort(key=lambda x: x.ts_init)
        catalog.write_data(trades)
        print(f"  Trade ticks: {len(trades):,}")

    # Order book deltas — write the batch objects directly
    deltas = strategy.deltas
    if deltas:
        deltas.sort(key=lambda x: x.ts_init)
        catalog.write_data(deltas)
        print(f"  Delta batches: {len(deltas):,}")

    # Verify
    data_types = catalog.list_data_types()
    print(f"  Data types: {data_types}")

    trades_back = catalog.trade_ticks(instrument_ids=[INSTRUMENT])
    print(f"  Verified trade ticks: {len(trades_back):,}")

    print(f"  Saved to: {CATALOG_PATH}")


def main():
    api_key = os.environ.get("BINANCE_LINEAR_API_KEY", "")
    api_secret = os.environ.get("BINANCE_LINEAR_API_SECRET", "")
    if not api_key or api_key == "***":
        print("BINANCE_LINEAR_API_KEY not set")
        return

    config = TradingNodeConfig(
        timeout_connection=20,
        timeout_reconciliation=5,
        timeout_portfolio=5,
        timeout_disconnection=5,
        logging=LoggingConfig(log_level="INFO"),
        data_engine=LiveDataEngineConfig(
            time_bars_interval_type="left-open",
        ),
        data_clients={
            "BINANCE": BinanceDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                account_type=BinanceAccountType.USDT_FUTURES,
                instrument_provider=InstrumentProviderConfig(
                    load_all=False,
                    load_ids=frozenset({INSTRUMENT}),
                ),
            ),
        },
    )

    node = TradingNode(config=config)
    node.add_data_client_factory("BINANCE", BinanceLiveDataClientFactory)

    strategy = CollectorStrategy(CollectorConfig())
    node.trader.add_strategy(strategy)
    node.build()

    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        node.dispose()

    save_to_catalog(node, strategy)


if __name__ == "__main__":
    main()
