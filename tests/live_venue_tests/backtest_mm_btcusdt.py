"""
Backtest SimpleMarketMaker on collected BTCUSDT-PERP data with L2 matching.

Requires: data/btcusdt_10m/ catalog from collect_btcusdt_10m.py

Usage:
    python tests/live_venue_tests/backtest_mm_btcusdt.py
"""

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "nautilus-trader" / "examples"))

from market_maker_backtest import MMConfig, SimpleMarketMaker

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.enums import AccountType, BookType, OmsType, OrderSide
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Currency, Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

CATALOG_PATH = PROJECT_ROOT / "data" / "btcusdt_10m"
INSTRUMENT_STR = "BTCUSDT-PERP.BINANCE"


def main():
    if not CATALOG_PATH.exists():
        print(f"Catalog not found at {CATALOG_PATH}")
        print("Run collect_btcusdt_10m.py first")
        return

    catalog = ParquetDataCatalog(str(CATALOG_PATH))

    # Load data
    print("--- LOADING DATA ---")
    instruments = catalog.instruments()
    instrument = None
    for inst in instruments:
        if str(inst.id) == INSTRUMENT_STR:
            instrument = inst
            break

    if instrument is None:
        print(f"Instrument {INSTRUMENT_STR} not found in catalog")
        print(f"Available: {[str(i.id) for i in instruments]}")
        return

    print(f"  Instrument: {instrument.id} (price_prec={instrument.price_precision}, size_prec={instrument.size_precision})")

    trades = catalog.trade_ticks(instrument_ids=[INSTRUMENT_STR])
    print(f"  Trade ticks: {len(trades):,}")

    deltas = catalog.order_book_deltas(instrument_ids=[INSTRUMENT_STR])
    print(f"  Book deltas: {len(deltas):,}")

    if not trades:
        print("No trade data — nothing to backtest")
        return

    # Engine setup
    print("\n--- BACKTEST SETUP ---")
    USDT = Currency.from_str("USDT")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(log_level="INFO"),
        ),
    )

    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=None,
        starting_balances=[Money(1_000_000, USDT)],
        book_type=BookType.L2_MBP,
        fill_model=FillModel(
            prob_fill_on_limit=0.3,
            prob_slippage=0.1,
            random_seed=42,
        ),
    )

    engine.add_instrument(instrument)
    engine.add_data(trades)
    if deltas:
        engine.add_data(deltas)

    strategy = SimpleMarketMaker(
        MMConfig(
            instrument_id=instrument.id,
            trade_size=Decimal("0.001"),   # ~$80 per side
            max_size=Decimal("0.01"),      # max inventory ~$800
            half_spread=Decimal("0.0003"), # 3 bps — wider than BTC spread
            skew_factor=Decimal("0.5"),
        ),
    )
    engine.add_strategy(strategy)

    print(f"  Strategy: SimpleMarketMaker")
    print(f"  trade_size=0.001 BTC, half_spread=3bps, max_size=0.01 BTC")
    print(f"  Venue: BINANCE L2_MBP, NETTING, MARGIN")
    print(f"  Fill model: prob_limit=0.3, prob_slip=0.1")

    # Run
    print("\n--- RUNNING BACKTEST ---")
    engine.run()

    # Results
    print("\n--- RESULTS ---")
    orders = engine.cache.orders()
    orders_closed = engine.cache.orders_closed()
    orders_open = engine.cache.orders_open()
    positions = engine.cache.positions()
    positions_open = engine.cache.positions_open()
    positions_closed = engine.cache.positions_closed()

    print(f"  Orders total:   {len(orders)}")
    print(f"  Orders closed:  {len(orders_closed)}")
    print(f"  Orders open:    {len(orders_open)}")
    print(f"  Positions total: {len(positions)}")
    print(f"  Positions open:  {len(positions_open)}")
    print(f"  Positions closed: {len(positions_closed)}")

    # P&L
    for pos in positions:
        if pos.is_closed:
            print(f"  Closed P&L: {pos.realized_pnl} (entry={pos.avg_px_open:.2f}, exit={pos.avg_px_close:.2f})")
        else:
            last_trade = trades[-1] if trades else None
            if last_trade:
                upnl = pos.unrealized_pnl(last_trade.price)
                print(f"  Open P&L:   {upnl} (entry={pos.avg_px_open:.2f}, qty={pos.signed_qty})")

    # Account
    accounts = engine.cache.accounts()
    if accounts:
        account = accounts[0]
        balance = account.balance_total(USDT)
        print(f"  Final balance: {balance}")

    # Fill stats
    from nautilus_trader.model.enums import OrderStatus
    filled_orders = [o for o in orders_closed if o.status == OrderStatus.FILLED]
    canceled_orders = [o for o in orders_closed if o.status == OrderStatus.CANCELED]
    print(f"\n  Filled:    {len(filled_orders)}")
    print(f"  Canceled:  {len(canceled_orders)}")

    if filled_orders:
        buy_fills = [o for o in filled_orders if o.side == OrderSide.BUY]
        sell_fills = [o for o in filled_orders if o.side == OrderSide.SELL]
        print(f"  Buy fills: {len(buy_fills)}, Sell fills: {len(sell_fills)}")

    engine.dispose()
    print("\nDone.")


if __name__ == "__main__":
    main()
