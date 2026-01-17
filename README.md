ПЕРРИ УТКОНОС ТРЕПЕЩИИИИИ!!! ЭТО МОЙ НОВЫЙ АРБИТОИНАТОР!!!

# ArbitoInator Dev Sandbox

---

## ⚡ Что умеет

- Берёт **реальные котировки с ETH mainnet (Uniswap V3 + V2-like DEXes)**  
- Сканирует N-hop маршруты (2-3 hop) с расчётом профита и газа  
- Выбирает лучший DEX на каждом хопе и сохраняет путь (route_dex/fee)  
- Использует пул RPC + кэши на блок, чтобы не зависать  
- Пушит события в UI по WebSocket и пишет JSONL логи  
- Имеет игрушечные модули MEV/slippage/reorg/mempool/bundler (эвристики)  

> Полностью безопасно, деньги не тратятся.

---

## 🧩 Структура проекта

```
ArbitoInator/
  contracts/        # контракты (ArbExecutor/Interfaces)
  bot/
    scanner.py      # live quotes + profit calc
    strategies.py   # генерация маршрутов
    executor.py     # заглушки транзакций для fork_test
    mempool.py      # pending tx генератор (toy)
    bundler.py      # bundle simulator (toy)
    dex/            # UniV3 + UniV2-like adapters
    risk/           # MEV/slippage/reorg (toy)
    config.py       # токены + RPC defaults
    utils.py        # вспомогалки
  sim/              # тесты/демо
  infra/
    rpc.py          # async RPC client + pool + web3 helper
  deploy/           # build/deploy скрипты
  ui/
    server.js       # web UI + bot runner
    index.html
  fork_test.py      # основной симулятор (real quotes)
  ui_notify.py      # Python -> UI push bridge
  bot_config.json   # runtime config (UI/CLI)
  logs/             # runtime logs (ignored by git)
  README.md
  requirements.txt
```

---

## ⚙️ Установка

1. Клонируем репо

```bash
git clone <your-repo>
cd ArbitoInator
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

CLI (headless, только консоль):
```bash
python fork_test.py
```

UI (веб-панель + запуск бота из UI):
```bash
node ui/server.js
```
Открой `http://localhost:8080`.

UI заметки:
- В таблице Deals можно тянуть ширину колонок, двойной клик сбрасывает ширину.
- Длинные маршруты прокручиваются горизонтально внутри ячейки Route.

---

## 🔧 Настройки

- `bot_config.json`  
  - RPC_URLS / rpc_urls → список RPC для failover  
  - dexes → какие DEX адаптеры использовать (univ3, univ2, sushiswap)  
  - thresholds, лимиты по газу, режимы scan_mode, etc.  
  - report_currency → базовая валюта в UI (USDC/USDT)  

- `bot/config.py`  
  - RPC_URL → ETH mainnet публичный RPC  
  - TOKENS → поддерживаемые токены (USDC, WETH, ...)  
  - UNISWAP_V3_QUOTER → Quoter адрес  
  - STRATEGY_* → дефолтные базы/хабы/вселенная токенов  

- `bot/risk` → настройки slippage и reorg

## 🔌 DEX адаптеры

Сейчас подключены:
- `univ3` (Uniswap V3 QuoterV2 + fallback)
- `univ2` (Uniswap V2 пары)
- `sushiswap` (SushiSwap пары)

Лучший DEX выбирается на каждом хопе. Путь сохраняется в payload и виден в UI.

## 📄 Логи

Во время работы пишутся JSONL логи:
- `logs/blocks.jsonl` — статистика по блокам
- `logs/hits.jsonl` — профитные события

Папка `logs/` игнорируется git.

---

## 📈 Дальнейшие шаги

- Подключить **Curve и Balancer adapters**  
- Расширить **MEV heuristics** и симуляцию mempool  
- Добавить **детерминированный бэктест** (fork + replay)  
- Подготовить **execution pipeline** для приватных бандлов  
