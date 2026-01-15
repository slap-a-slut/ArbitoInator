ПЕРРИ УТКОНОС ТРЕПЕЩИИИИИ!!! ЭТО МОЙ НОВЫЙ АРБИТОИНАТОР!!!

# ArbitoInator Dev Sandbox

---

## ⚡ Что умеет

- Берёт **реальные цены с ETH mainnet (Uniswap V3)**  
- Симулирует арбитражные маршруты (USDC → WETH → USDC)  
- Проверяет **MEV угрозы** (sandwich/frontrun)  
- Проверяет **slippage**  
- Проверяет **mini-reorg stability**  
- Симулирует **pending mempool tx**  
- Собирает **bundle и считает суммарный профит**  

> Полностью безопасно, деньги не тратятся.

---

## 🧩 Структура проекта

```
arb-bot/
  contracts/        # контракты (ArbExecutor/Interfaces)
  bot/
    scanner.py      # fetch live prices
    strategies.py   # маршруты и profit
    executor.py     # симулятор swap/profit
    mempool.py      # pending tx генератор
    bundler.py      # bundle simulator
    dex/            # UniV3/Curve/Balancer adapters
    risk/           # MEV, slippage, reorg
    config.py       # токены и RPC
    utils.py        # вспомогалки
  sim/              # тесты
  infra/
    rpc.py          # async RPC client
  deploy/           # build/deploy скрипты
  README.md
  requirements.txt
```

---

## ⚙️ Установка

1. Клонируем репо

```bash
git clone <your-repo>
cd arb-bot
```

2. Создаем виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. Устанавливаем зависимости

```bash
pip install -r requirements.txt
```

---

## 🚀 Быстрый запуск демо

```bash
python bot/run_bundle_demo.py
```

Вывод:

```
[Mempool] new tx ...
[Demo] Simulated profit: 0.000425 USDC
[BundleSimulator] Total TXs: 1
[BundleSimulator] Simulated Total Profit: 0.000425 USDC
```

---

## 🔧 Настройки

- `bot/config.py`  
  - RPC_URL → ETH mainnet публичный RPC  
  - TOKENS → поддерживаемые токены (USDC, WETH)  
  - UNISWAP_V3_QUOTER → Quoter адрес  

- `bot/risk` → настройки slippage и reorg

---

## 📈 Дальнейшие шаги

- Добавить **triangular routes**  
- Подключить **Curve и Balancer adapters**  
- Расширить **MEV heuristics**  
- Добавить **UI / визуализацию профита**
