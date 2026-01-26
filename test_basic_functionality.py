#!/usr/bin/env python3
"""
Basic smoke test for ArbitoInator project
Tests core functionality and fixes
"""

import sys
import traceback
from pathlib import Path

def test_python_imports():
    """Test Python module imports"""
    print("🔍 Testing Python imports...")
    try:
        import bot.scanner
        import bot.strategies  
        import bot.executor
        import bot.preflight
        import bot.config
        print("✅ Python imports successful")
    except Exception as e:
        print(f"❌ Python import failed: {e}")
        traceback.print_exc()
        raise

def test_contract_compilation():
    """Test contract compilation artifacts"""
    print("\n🔍 Testing contract compilation...")
    try:
        artifacts_dir = Path("artifacts")
        assert artifacts_dir.exists(), "No artifacts directory found"
            
        # Check for main contracts
        required_contracts = [
            "contracts/ArbitrageExecutor.sol/ArbExecutor.json",
            "contracts/AccessController.sol/AccessController.json", 
            "contracts/Treasury.sol/Treasury.json",
            "contracts/adapters/UniV2.sol/UniswapV2Adapter.json",
            "contracts/adapters/UniV3Adapter.sol/UniswapV3Adapter.json"
        ]
        
        missing = []
        for contract_path in required_contracts:
            full_path = artifacts_dir / contract_path
            if not full_path.exists():
                missing.append(contract_path)
        
        assert not missing, f"Missing contract artifacts: {missing}"
        print("✅ All contract artifacts found")
            
    except Exception as e:
        print(f"❌ Contract compilation check failed: {e}")
        raise

def test_config_files():
    """Test configuration files"""
    print("\n🔍 Testing configuration files...")
    try:
        import json
        
        # Test main config
        with open("bot_config.json", "r") as f:
            config = json.load(f)
        
        required_keys = ["rpc_urls", "dexes", "min_profit_pct"]
        for key in required_keys:
            assert key in config, f"Missing config key: {key}"
        
        # Test Sepolia config
        with open("configs/chains/sepolia.json", "r") as f:
            sepolia_config = json.load(f)
        
        assert sepolia_config["tokens"]["USDC"] is not None, "Sepolia USDC token not configured"
            
        print("✅ Configuration files valid")
        
    except Exception as e:
        print(f"❌ Config validation failed: {e}")
        raise

def test_dependencies():
    """Test Python dependencies"""
    print("\n🔍 Testing dependencies...")
    try:
        import warnings
        warnings.filterwarnings(
            "ignore",
            message=r"urllib3 v2 only supports OpenSSL.*",
            category=Warning,
        )
        import web3
        import aiohttp
        import pandas as pd
        import numpy as np
        from loguru import logger
        
        print(f"✅ Web3 version: {web3.__version__}")
        print(f"✅ Pandas version: {pd.__version__}")
        print(f"✅ NumPy version: {np.__version__}")
        
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        raise

def test_basic_functionality():
    """Test basic system functionality"""
    print("\n🔍 Testing basic functionality...")
    try:
        from bot.strategies import Strategy
        from bot.executor import Executor
        
        # Test strategy creation
        strategy = Strategy()
        print(f"✅ Strategy created with {len(strategy.bases)} base tokens")
        
        # Test executor creation
        executor = Executor()
        print("✅ Executor created successfully")
        
        # Test basic strategy calculation
        profit = strategy.calc_profit(1000, 1050, 20)
        assert profit == 30, f"Strategy profit calculation wrong: {profit}"
        print("✅ Strategy profit calculation correct")
        
    except Exception as e:
        print(f"❌ Basic functionality test failed: {e}")
        traceback.print_exc()
        raise

def main():
    """Run all tests"""
    print("🚀 ArbitoInator Smoke Test Started")
    print("=" * 50)
    
    tests = [
        test_python_imports,
        test_contract_compilation, 
        test_config_files,
        test_dependencies,
        test_basic_functionality
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception:
            pass
        print()
    
    print("=" * 50)
    print(f"📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! ArbitoInator is working correctly.")
        return 0
    else:
        print("⚠️  Some tests failed. Please check the issues above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
