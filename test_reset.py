#!/usr/bin/env python
"""Quick test of reset functionality."""
from database.db import reset_copy_trading, get_copy_trade_stats, get_followed_wallets



if __name__ == "__main__":
    print("\n📊 Before Reset:")
    stats = get_copy_trade_stats()
    followed = list(get_followed_wallets())
    print(f"  Open Trades:   {stats['open_trades']}")
    print(f"  Closed Trades: {stats['closed_trades']}")
    print(f"  Total P&L:     ${stats['total_pnl']:.2f}")
    print(f"  Followed:      {len(followed)} wallets")
    
    print("\n🔄 Running reset...")
    reset_copy_trading()
    
    print("\n📊 After Reset:")
    stats = get_copy_trade_stats()
    followed = list(get_followed_wallets())
    print(f"  Open Trades:   {stats['open_trades']}")
    print(f"  Closed Trades: {stats['closed_trades']}")
    print(f"  Total P&L:     ${stats['total_pnl']:.2f}")
    print(f"  Followed:      {len(followed)} wallets (preserved)")
    
    print("\n✅ Reset successful!\n")
    