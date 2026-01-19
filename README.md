ПЕРРИ УТКОНОС ТРЕПЕЩИИИИИ!!! ЭТО МОЙ НОВЫЙ АРБИТОИНАТОР!!!

# ArbitoInator Dev Sandbox

---

## ⚡ Что умеет

- Берёт **реальные котировки с ETH mainnet (Uniswap V3 + V2-like DEXes)**  
- Сканирует N-hop маршруты (2-4 hop) с расчётом профита и газа  
- Multi-DEX engine (опционально): на каждом hop выбирает DEX и строит dex_path  
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
    routes.py       # модели маршрутов (Hop)
    dex/base.py     # интерфейс DEX адаптеров
    dex/registry.py # реестр адаптеров
    risk/           # MEV/slippage/reorg (toy)
    config.py       # токены + RPC defaults
    utils.py        # вспомогалки
  sim/              # тесты/демо
    multidex_smoke.py # Multi-DEX smoke test
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
  - enable_multidex → включить Multi-DEX + beam search  
  - dexes → какие DEX адаптеры использовать (univ3, univ2, sushiswap)  
  - max_hops → максимальная длина цикла (2..4)  
  - beam_k → сколько лучших комбинаций DEX держать  
  - edge_top_m → сколько лучших quotes брать на hop  
  - trigger_prefer_cross_dex / trigger_require_cross_dex → предпочтение/требование к смешанным DEX маршрутам (trigger scan)  
  - trigger_require_three_hops → требовать 3-hop циклы для trigger scan  
  - trigger_cross_dex_bonus_bps / trigger_same_dex_penalty_bps → бонус/штраф для скоринга  
  - trigger_edge_top_m_per_dex → сколько топ‑квот брать на hop на каждый DEX  
  - probe_amount → объём для prefilter  
  - prepare_budget_ratio / prepare_budget_min_s / prepare_budget_max_s → бюджет на prepare_block  
  - expand_ratio_cap / expand_budget_max_s → лимит бюджета на multidex expansion  
  - min_scan_reserve_s / min_first_task_s → резерв времени на скан и правило "schedule at least one"  
  - max_candidates_stage1 → жёсткий лимит кандидатов на stage1  
  - max_total_expanded / max_expanded_per_candidate → лимиты на multidex expansion  
  - rpc_timeout_s / rpc_retry_count → общий таймаут и ретраи RPC  

## Presets (UI тестовые профили)
Пресеты лежат в `presets/` (по одному JSON на профиль). UI подхватывает их автоматически.

Как добавить новый пресет:
1) Создайте `presets/<id>.json` с полями `id`, `name`, `description`, `settings`.
2) В `settings` указывайте поддерживаемые ключи (см. текущие пресеты за пример).
3) Перезапустите UI-сервер (или просто обновите страницу, сервер перечитает файлы).

Как использовать:
- В панели Settings выберите пресет и нажмите “Apply preset”.
- Поля формы заполнятся, но сохранять/перезапускать нужно вручную через “save” или “apply & restart”.

## Mempool mode (pending tx triggers)
В проект добавлен мемпул-слой: мы слушаем публичный WS mempool, декодируем свопы и запускаем быстрый “pre‑scan” до майнинга блока. Никаких реальных транзакций не отправляется — это только симуляция.

Как включить:
1) В UI выберите `Scan source: mempool` или `hybrid`.
2) Укажите WS URL в `Mempool WS URLs` (публичный провайдер с поддержкой `newPendingTransactions`).
3) Нажмите `save` и затем `apply & restart`.

Что означает pre/post:
- Pre‑scan: скан сразу после появления pending tx (до блока).
- Post‑scan: быстрая проверка после включения tx в блок (для сравнения).

Trigger‑скан (mempool):
- По умолчанию предпочитает смешанные DEX‑маршруты и 3‑hop циклы (настраивается в UI).
- В `logs/trigger_scans.jsonl` пишутся `classification`, `backend`, `dex_mix`, `hops`, `post_best_net`, `post_delta_net`.

Ожидаемое поведение:
- Много `no_hit` — это нормально.
- В логах должны появляться decoded swaps + trigger scans.
- Файлы: `logs/mempool.jsonl` и `logs/trigger_scans.jsonl`.
  - thresholds, лимиты по газу, режимы scan_mode, etc.

## Execution‑grade симуляция (dry‑run)
Сканер считает gross/net с учётом газа + slippage/MEV buffers. Это всё ещё симуляция (без реального исполнения).

Готовность к тестнету:
- `bot/arb_builder.py` строит calldata для контракта `ArbitrageExecutor`.
- `deploy/deploy_executor.py` деплоит контракт (нужен `solc`, `RPC_URL`, `PRIVATE_KEY`).
- Укажите `ARB_EXECUTOR_ADDRESS` в `bot/config.py`, чтобы включить dry‑run calldata (без broadcast).

`bot/config.py`  
- RPC_URL → ETH mainnet публичный RPC  
- TOKENS → поддерживаемые токены (USDC, WETH, ...)  
- UNISWAP_V3_QUOTER → Quoter адрес  
- STRATEGY_* → дефолтные базы/хабы/вселенная токенов  
- RPC_PRIORITY_WEIGHTS / RPC_FALLBACK_ONLY → приоритеты пула RPC  
- RPC_CB_* / RPC_TIMEOUT_* → circuit breaker и таймауты RPC  
- RPC_RETRY_COUNT / RPC_RATE_LIMIT_BACKOFF_S → ретраи и backoff  

- `bot/risk` → настройки slippage и reorg

## 🔌 DEX адаптеры

Сейчас подключены:
- `univ3` (Uniswap V3 QuoterV2 + fallback)
- `univ2` (Uniswap V2 Router)
- `sushiswap` (SushiSwap Router)

Multi-DEX mode строит dex_path (в т.ч. fee tier) и показывает его в UI/логах.

## MEV и фильтры качества

На этапе симуляции применяются защитные фильтры:
- slippage_bps + mev_buffer_bps вычитаются из профита (консервативно)
- V2 пары фильтруются по резервам и price impact

## 📄 Логи

Во время работы пишутся JSONL логи:
- `logs/blocks.jsonl` — статистика по блокам
- `logs/hits.jsonl` — профитные события
- `logs/diagnostic_snapshot.json` — единый диагностический снимок состояния (обновляется на старте, по таймеру и при остановке)

В `blocks.jsonl` теперь есть диагностические поля:
`prepare_ms`, `scan_start_delay_ms`, `stage1_deadline_remaining_ms_at_scan_start`,
`reason_if_zero_scheduled`, `sanity_rejects_total`, `rejects_by_reason`.

Папка `logs/` игнорируется git.

Диагностический снимок (single‑file):
- Формируется автоматически (по умолчанию каждые 45с) и перезаписывается.
- Содержит RPC/WS health, триггеры, агрегаты, последний trigger и ключевые настройки.
- Можно вывести разово: `python3 fork_test.py --dump-diagnostic`

---

## 🧪 Debug funnel (диагностика профита)

Полезные команды для быстрой диагностики:

```bash
DEBUG_FUNNEL=1 SIM_PROFILE=debug python3 -u fork_test.py
DEBUG_FUNNEL=1 SIM_PROFILE=debug FIXED_GAS_UNITS=180000 python3 -u fork_test.py
DEBUG_FUNNEL=1 SIM_PROFILE=debug GAS_OFF=1 python3 -u fork_test.py
```

`SIM_PROFILE=debug` поднимает stage1_amount и ослабляет пороги, чтобы быстрее увидеть raw/net возможности.
`FIXED_GAS_UNITS` и `GAS_OFF=1` — только для отладки (не для реального профита).

Multi-DEX:

```bash
ENABLE_MULTIDEX=1 python3 -u fork_test.py
ENABLE_MULTIDEX=1 GAS_OFF=1 python3 -u fork_test.py
python3 -u sim/multidex_smoke.py
```

---

## 📈 Дальнейшие шаги

- Подключить **Curve и Balancer adapters**  
- Расширить **MEV heuristics** и симуляцию mempool  
- Добавить **детерминированный бэктест** (fork + replay)  
- Подготовить **execution pipeline** для приватных бандлов  
