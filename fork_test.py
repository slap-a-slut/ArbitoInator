# sim/fork_test.py
import asyncio
from datetime import datetime
from web3 import Web3
from bot import scanner, strategies, executor, config

# ----------------------------
# Настройка Web3 / RPC
from infra.rpc import get_provider
w3 = get_provider()

# Хардкодим аккаунт для теста (можно через keys.py)
account = w3.eth.account.from_key(config.TEST_PRIVATE_KEY)

# ----------------------------
# Ждём новый блок
async def wait_for_new_block(last_block):
    while True:
        current_block = w3.eth.block_number
        if current_block > last_block:
            return current_block
        await asyncio.sleep(0.1)

# ----------------------------
# Этапы цикла
async def scan_markets():
    """Ищем кандидатов арбитража через сканер"""
    ps = scanner.PriceScanner()
    return await ps.find_arbitrage_opportunities(w3)

async def get_prices(candidate):
    """
    Берём реальные котировки через методы сканера
    """
    route = candidate["route"]
    amount_in = candidate["amount_in"]

    ps = scanner.PriceScanner()
    out_1 = await ps.get_univ3_quote(route[0], route[1], amount_in)
    out_2 = await ps.get_univ3_quote(route[1], route[2], out_1)

    # Имитация gas cost
    gas_cost = 21000

    profit = out_2 - amount_in - gas_cost

    return {
        "route": route,
        "amount_in": amount_in,
        "profit": profit,
        "to": "0x0000000000000000000000000000000000000000",
    }

def find_opportunity(candidates):
    """
    Фильтруем кандидатов с положительным профитом и risk layer
    """
    profitable = []
    for c in candidates:
        payload = asyncio.run(get_prices(c))
        if payload["profit"] > 0 and strategies.risk_check(payload):
            profitable.append(payload)
    return profitable

async def simulate(payload):
    """
    Симуляция выполнения через executor
    """
    tx = executor.prepare_transaction(payload, account.address)
    print(f"[Executor] Simulating tx from {tx['from']} to {tx['to']} | Profit: {payload['profit']:.6f}")
    await asyncio.sleep(0.01)  # имитация async

def log(iteration, payloads, block_number):
    now = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{now}] Block {block_number} | Iteration {iteration} | Profitable payloads: {len(payloads)}")
    for p in payloads:
        print(f"  Route: {p['route']} | Profit: {p['profit']:.6f}")

# ----------------------------
# Основной бесконечный loop
async def main():
    iteration = 0
    print("🚀 Fork simulation started...")
    last_block = w3.eth.block_number

    while True:
        iteration += 1

        # Ждём новый блок
        block_number = await wait_for_new_block(last_block)
        last_block = block_number

        # 1️⃣ Сканируем рынки
        candidates = await scan_markets()
        if not candidates:
            print(f"[Block {block_number}] ⚠️ No candidates found.")
            continue

        # 2️⃣ Фильтруем профитные
        profitable = find_opportunity(candidates)
        if not profitable:
            print(f"[Block {block_number}] ⚠️ No profitable opportunities this block.")
            continue

        # 3️⃣ Симуляция выполнения
        for payload in profitable:
            await simulate(payload)

        # 4️⃣ Лог
        log(iteration, profitable, block_number)

# ----------------------------
# Запуск
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Fork simulation stopped by user")
