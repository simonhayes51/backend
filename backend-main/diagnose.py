#!/usr/bin/env python3
"""
Diagnostic script to check if new endpoints are properly loaded
Run this in your server environment where FastAPI is installed
"""

import sys
import os

# Add backend directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def check_imports():
    """Check if all routers can be imported"""
    print("🔍 Checking Router Imports...\n")

    routers_to_check = [
        ('portfolio', '/api/ai/optimize-portfolio'),
        ('trades', '/api/trades/bulk'),
        ('ai_engine', '/api/ai/copilot'),
        ('market', '/api/market/sentiment'),
    ]

    for router_name, expected_path in routers_to_check:
        try:
            module = __import__(f'app.routers.{router_name}', fromlist=['router'])
            router = module.router

            # Get all routes
            routes = [f"{route.methods} {route.path}" for route in router.routes if hasattr(route, 'path')]

            print(f"✅ {router_name}")
            print(f"   Prefix: {router.prefix}")
            print(f"   Routes: {len(routes)}")
            for route in routes[:5]:  # Show first 5 routes
                print(f"      - {route}")
            print()

        except Exception as e:
            print(f"❌ {router_name}: {str(e)}\n")

def check_main():
    """Check if main.py imports and registers the routers"""
    print("\n🔍 Checking main.py Router Registrations...\n")

    try:
        # Check if routers are imported
        with open('main.py', 'r') as f:
            content = f.read()

        imports_to_check = [
            'from app.routers.portfolio import router as portfolio_router',
            'from app.routers.trades import router as trades_router',
            'app.include_router(portfolio_router)',
            'app.include_router(trades_router)',
        ]

        for import_line in imports_to_check:
            if import_line in content:
                print(f"✅ Found: {import_line}")
            else:
                print(f"❌ Missing: {import_line}")

    except Exception as e:
        print(f"❌ Error reading main.py: {e}")

def check_files():
    """Check if router files exist"""
    print("\n🔍 Checking Router Files...\n")

    files_to_check = [
        'app/routers/portfolio.py',
        'app/routers/trades.py',
        'app/routers/leaderboard.py',
        'app/routers/referrals.py',
    ]

    for file_path in files_to_check:
        if os.path.exists(file_path):
            size = os.path.getsize(file_path)
            print(f"✅ {file_path} ({size} bytes)")
        else:
            print(f"❌ {file_path} NOT FOUND")

if __name__ == "__main__":
    print("=" * 60)
    print("Backend Endpoint Diagnostic Tool")
    print("=" * 60)
    print()

    check_files()
    check_main()

    try:
        check_imports()
    except ImportError as e:
        print(f"\n⚠️  Cannot import routers: {e}")
        print("This is expected if FastAPI is not installed in this environment.")
        print("Run this script on your actual server where the app runs.")

    print("\n" + "=" * 60)
    print("✅ Diagnostic Complete")
    print("=" * 60)
