# infra/rpc.py

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from contextvars import ContextVar, Token
from typing import Any, Optional, Sequence, List, Dict, Set, Tuple

import aiohttp
from web3 import Web3

from bot import config
from infra.metrics import METRICS

_PINNED_HTTP_ENDPOINT: ContextVar[Optional[str]] = ContextVar("rpc_pinned_http_endpoint", default=None)


def set_pinned_http_endpoint(url: Optional[str]) -> Token:
    return _PINNED_HTTP_ENDPOINT.set(_normalize_url(url) if url else None)


def reset_pinned_http_endpoint(token: Token) -> None:
    _PINNED_HTTP_ENDPOINT.reset(token)


def get_pinned_http_endpoint() -> Optional[str]:
    return _PINNED_HTTP_ENDPOINT.get()


def _normalize_url(url: str) -> str:
    u = str(url).strip()
    if not u:
        return u
    if "://" not in u:
        u = "https://" + u
    return u


def _url_host(url: str) -> str:
    u = _normalize_url(url).lower()
    if "://" in u:
        u = u.split("://", 1)[1]
    return u.split("/", 1)[0]


def _split_urls(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    # Accept comma or newline separated lists.
    parts: List[str] = []
    for chunk in str(raw).replace("\n", ",").split(","):
        u = _normalize_url(chunk)
        if u:
            parts.append(u)
    return parts


def get_rpc_urls() -> List[str]:
    """Return RPC URL candidates in priority order.

    Order:
      1) env RPC_HTTP_URLS (comma/newline list)
      2) bot.config.RPC_HTTP_URLS (if present)
      3) env RPC_URLS (comma/newline list)
      4) bot.config.RPC_URLS (if present)
      5) env RPC_URL
      6) bot.config.RPC_URL
    """

    urls: List[str] = []
    urls.extend(_split_urls(os.getenv("RPC_HTTP_URLS")))
    urls.extend([_normalize_url(u) for u in (getattr(config, "RPC_HTTP_URLS", []) or []) if str(u).strip()])
    urls.extend(_split_urls(os.getenv("RPC_URLS")))
    urls.extend([_normalize_url(u) for u in (getattr(config, "RPC_URLS", []) or []) if str(u).strip()])

    single = os.getenv("RPC_URL")
    if single:
        urls.append(_normalize_url(single))

    if not urls:
        urls = [_normalize_url(config.RPC_URL)]

    # De-dupe while preserving order
    out: List[str] = []
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _normalize_weights(weights: Optional[Sequence[float]], n: int) -> List[float]:
    if not weights:
        return [1.0 for _ in range(n)]
    out = [float(w) for w in weights if w is not None]
    if not out:
        return [1.0 for _ in range(n)]
    if len(out) < n:
        out.extend([out[-1]] * (n - len(out)))
    return out[:n]


def _normalize_rpc_error(msg: Optional[str]) -> str:
    text = str(msg or "").lower()
    if "timeout" in text:
        return "timeout"
    if "http_429" in text or "rate limit" in text:
        return "rate_limited"
    if "http_5" in text:
        return "http_5xx"
    if "decode" in text:
        return "decode_error"
    if "revert" in text:
        return "revert"
    if "rpc" in text or "http_" in text:
        return "rpc_error"
    return "internal_error"


def _percentile(vals: List[float], pct: float) -> Optional[float]:
    if not vals:
        return None
    v = sorted(vals)
    if len(v) == 1:
        return float(v[0])
    k = max(0, min(len(v) - 1, int(round((pct / 100.0) * (len(v) - 1)))))
    return float(v[k])


class EndpointHealth:
    def __init__(self, maxlen: int = 50) -> None:
        self._samples: deque[Tuple[float, bool, str]] = deque(maxlen=max(1, int(maxlen)))

    def record(self, ok: bool, latency_ms: float, reason: str) -> None:
        self._samples.append((float(latency_ms), bool(ok), str(reason)))

    def stats(self) -> Dict[str, Any]:
        if not self._samples:
            return {
                "count": 0,
                "success_rate": None,
                "timeout_rate": None,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
            }
        total = len(self._samples)
        oks = sum(1 for _, ok, _ in self._samples if ok)
        timeouts = sum(1 for _, _, reason in self._samples if reason == "timeout")
        lats = [float(lat) for lat, _, _ in self._samples]
        return {
            "count": total,
            "success_rate": float(oks) / float(total) if total > 0 else None,
            "timeout_rate": float(timeouts) / float(total) if total > 0 else None,
            "p50_latency_ms": _percentile(lats, 50.0),
            "p95_latency_ms": _percentile(lats, 95.0),
        }


class AsyncRPC:
    """Async JSON-RPC client with:
    - persistent aiohttp session (fast)
    - per-call timeouts (prevents "first block never ends")
    - retries + exponential backoff for transient errors / rate limits
    - Uniswap Quoter revert-data extraction for eth_call
    """

    def __init__(
        self,
        url: str,
        *,
        default_timeout_s: float = 3.0,
        max_retries: Optional[int] = None,
        backoff_base_s: Optional[float] = None,
    ):
        self.url = url
        self.default_timeout_s = float(default_timeout_s)
        if max_retries is None:
            max_retries = int(getattr(config, "RPC_RETRY_COUNT", 1))
        if backoff_base_s is None:
            backoff_base_s = float(getattr(config, "RPC_BACKOFF_BASE_S", 0.35))
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self._id = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None
        self.last_url: Optional[str] = None

        # Lightweight stats (useful even for single-RPC mode)
        self._stats_lock = asyncio.Lock()
        self._inflight = 0
        self._ok = 0
        self._fail = 0
        self._lat_ewma_ms = 350.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        # Limit total sockets to avoid exploding the public RPC.
        self._connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=self._connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._connector:
            await self._connector.close()
        self._connector = None

    async def call(
        self,
        method: str,
        params: list,
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
        allow_error_data: bool = False,
    ) -> Any:
        """Perform a JSON-RPC call.

        IMPORTANT: Some contracts (e.g., Uniswap V3 Quoter v1) intentionally revert
        to return data for eth_call. That revert-data pass-through must be *opt-in*
        via allow_revert_data=True; otherwise, downstream ABI decoding can turn
        Solidity revert payloads into absurd huge numbers.
        """

        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}

        session = await self._get_session()
        self.last_url = self.url
        to_s = float(timeout_s) if timeout_s is not None else self.default_timeout_s
        min_t = float(getattr(config, "RPC_TIMEOUT_MIN_S", 2.0))
        max_t = float(getattr(config, "RPC_TIMEOUT_MAX_S", 4.0))
        if max_t < min_t:
            max_t = min_t
        to_s = max(min_t, min(max_t, to_s))
        last_err: Optional[str] = None

        async with self._stats_lock:
            self._inflight += 1

        try:
            for attempt in range(self.max_retries + 1):
                t0 = time.perf_counter()
                host = _url_host(self.url)
                METRICS.inc("rpc_requests_total", 1)
                METRICS.inc_reason("rpc_requests_by_endpoint", host, 1)
                try:
                    async def _do():
                        async with session.post(self.url, json=payload) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                raise aiohttp.ClientResponseError(
                                    request_info=resp.request_info,
                                    history=resp.history,
                                    status=resp.status,
                                    message=text,
                                    headers=resp.headers,
                                )
                            return await resp.json()

                    data = await asyncio.wait_for(_do(), timeout=to_s)

                    # EWMA latency update (success path and allowed-revert path)
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                    METRICS.observe("rpc_latency_ms", float(dt_ms))
                    METRICS.observe(f"rpc_latency_ms:{host}", float(dt_ms))
                    async with self._stats_lock:
                        self._lat_ewma_ms = 0.8 * self._lat_ewma_ms + 0.2 * float(dt_ms)

                    if isinstance(data, dict) and "error" in data:
                        err = data["error"]
                        err_data = err.get("data") if isinstance(err, dict) else None

                        def _extract_revert_hex(ed: Any) -> Optional[str]:
                            if isinstance(ed, dict):
                                if isinstance(ed.get("data"), str):
                                    return ed["data"]
                                if isinstance(ed.get("result"), str):
                                    return ed["result"]
                                for _k, v in ed.items():
                                    if isinstance(v, dict):
                                        if isinstance(v.get("return"), str):
                                            return v["return"]
                                        if isinstance(v.get("data"), str):
                                            return v["data"]
                            if isinstance(ed, str):
                                return ed
                            return None

                        revert_hex = _extract_revert_hex(err_data)
                        if revert_hex and allow_revert_data and method == "eth_call":
                            hx = revert_hex if revert_hex.startswith("0x") else ("0x" + revert_hex)
                            # Guard against common Solidity error selectors unless explicitly allowed.
                            if not allow_error_data and (hx.startswith("0x08c379a0") or hx.startswith("0x4e487b71")):
                                last_err = f"revert({hx[:10]})"
                                raise RuntimeError(last_err)
                            async with self._stats_lock:
                                self._ok += 1
                            return hx

                        last_err = f"rpc_error:{err}"
                        raise RuntimeError(last_err)

                    async with self._stats_lock:
                        self._ok += 1
                    return data["result"]

                except asyncio.TimeoutError:
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                    METRICS.observe("rpc_latency_ms", float(dt_ms))
                    METRICS.observe(f"rpc_latency_ms:{host}", float(dt_ms))
                    last_err = f"timeout({to_s}s)"
                except aiohttp.ClientResponseError as e:
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                    METRICS.observe("rpc_latency_ms", float(dt_ms))
                    METRICS.observe(f"rpc_latency_ms:{host}", float(dt_ms))
                    last_err = f"http_{e.status}"
                    if e.status not in (429, 500, 502, 503, 504):
                        break
                except (aiohttp.ClientError, RuntimeError, ValueError) as e:
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                    METRICS.observe("rpc_latency_ms", float(dt_ms))
                    METRICS.observe(f"rpc_latency_ms:{host}", float(dt_ms))
                    last_err = f"{type(e).__name__}: {e}"

                if attempt < self.max_retries:
                    sleep_s = (self.backoff_base_s * (2 ** attempt)) + random.random() * 0.25
                    if last_err and ("http_429" in last_err or "rate limit" in last_err.lower()):
                        sleep_s += float(getattr(config, "RPC_RATE_LIMIT_BACKOFF_S", 0.35))
                    await asyncio.sleep(sleep_s)

            METRICS.inc_reason("rpc_fail_by_reason", _normalize_rpc_error(last_err), 1)
            async with self._stats_lock:
                self._fail += 1
            raise Exception(f"RPC call failed after retries: {last_err}")

        finally:
            async with self._stats_lock:
                self._inflight = max(0, self._inflight - 1)

    async def call_batch(
        self,
        method: str,
        params_list: List[list],
        *,
        timeout_s: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        if not params_list:
            return []

        session = await self._get_session()
        to_s = float(timeout_s) if timeout_s is not None else self.default_timeout_s
        min_t = float(getattr(config, "RPC_TIMEOUT_MIN_S", 2.0))
        max_t = float(getattr(config, "RPC_TIMEOUT_MAX_S", 4.0))
        if max_t < min_t:
            max_t = min_t
        to_s = max(min_t, min(max_t, to_s))

        async with self._stats_lock:
            self._inflight += 1

        try:
            self._id += len(params_list)
            start_id = self._id - len(params_list) + 1
            payload = [
                {"jsonrpc": "2.0", "id": start_id + i, "method": method, "params": params_list[i]}
                for i in range(len(params_list))
            ]

            async def _do():
                async with session.post(self.url, json=payload) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=text,
                            headers=resp.headers,
                        )
                    return await resp.json()

            data = await asyncio.wait_for(_do(), timeout=to_s)
            if not isinstance(data, list):
                raise RuntimeError("batch_response_not_list")

            by_id: Dict[int, Any] = {}
            for entry in data:
                if isinstance(entry, dict) and isinstance(entry.get("id"), int):
                    by_id[int(entry.get("id"))] = entry

            out: List[Dict[str, Any]] = []
            for i in range(len(params_list)):
                entry = by_id.get(start_id + i)
                if not isinstance(entry, dict):
                    out.append({"error": "missing"})
                    continue
                if "error" in entry:
                    out.append({"error": entry.get("error")})
                else:
                    out.append({"result": entry.get("result")})
            async with self._stats_lock:
                self._ok += 1
            return out
        except Exception:
            async with self._stats_lock:
                self._fail += 1
            raise
        finally:
            async with self._stats_lock:
                self._inflight = max(0, self._inflight - 1)
    def stats(self) -> List[Dict[str, Any]]:
        return [
            {
                "url": self.url,
                "inflight": self._inflight,
                "ok": self._ok,
                "fail": self._fail,
                "lat_ms": round(float(self._lat_ewma_ms), 1),
            }
        ]

    async def eth_call(
        self,
        to: str,
        data: str,
        block: str = "latest",
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
        allow_error_data: bool = False,
        block_ctx: Optional[Any] = None,
    ) -> str:
        return await self.call(
            "eth_call",
            [{"to": to, "data": data}, block],
            timeout_s=timeout_s,
            allow_revert_data=allow_revert_data,
            allow_error_data=allow_error_data,
            block_ctx=block_ctx,
        )

    async def get_block_number(self, *, timeout_s: Optional[float] = None) -> int:
        res = await self.call("eth_blockNumber", [], timeout_s=timeout_s)
        return int(res, 16)


class RPCPool:
    """Parallel RPC pool (load-balancing across all RPCs).

    This is NOT a failover-only design. Each request is routed to a single RPC
    endpoint chosen by least in-flight requests (so concurrent quoting spreads
    over all endpoints). On error/timeout, it retries on another endpoint.

    This approach:
      - uses all RPCs in parallel (higher throughput)
      - reduces per-endpoint rate limits
      - still survives flaky endpoints via retry-on-others

    Note: We intentionally do *not* send the same request to all RPCs (hedging),
    because that multiplies request volume.
    """

    def __init__(
        self,
        urls: Sequence[str],
        *,
        default_timeout_s: Optional[float] = None,
        max_retries_per_call: Optional[int] = None,
        per_rpc_max_inflight: int = 10,
        ewma_alpha: float = 0.20,
        priority_weights: Optional[Sequence[float]] = None,
        fallback_only: Optional[Sequence[str]] = None,
        cb_threshold: Optional[int] = None,
        cb_cooldown_s: Optional[float] = None,
    ):
        if default_timeout_s is None:
            default_timeout_s = float(getattr(config, "RPC_TIMEOUT_S", getattr(config, "RPC_DEFAULT_TIMEOUT_S", 3.0)))
        cleaned = [_normalize_url(u) for u in (urls or []) if str(u).strip()]
        if not cleaned:
            raise ValueError("RPCPool requires at least one url")

        self.urls: List[str] = cleaned
        self._clients = [AsyncRPC(u, default_timeout_s=default_timeout_s, max_retries=0) for u in self.urls]
        self.last_url: Optional[str] = None

        # Per-endpoint concurrency guard (prevents a single RPC from being flooded)
        self._sems: List[asyncio.Semaphore] = [asyncio.Semaphore(max(1, int(per_rpc_max_inflight))) for _ in self._clients]
        self._max_inflight: List[int] = [max(1, int(per_rpc_max_inflight)) for _ in self._clients]

        weights = priority_weights if priority_weights is not None else getattr(config, "RPC_PRIORITY_WEIGHTS", None)
        self._priority_weights: List[float] = _normalize_weights(weights, len(self._clients))

        fallback_cfg = fallback_only if fallback_only is not None else getattr(config, "RPC_FALLBACK_ONLY", None)
        self._fallback_only: Set[int] = set()
        if fallback_cfg:
            fallback_hosts = {_url_host(x) for x in fallback_cfg if str(x).strip()}
            for i, url in enumerate(self.urls):
                if _url_host(url) in fallback_hosts:
                    self._fallback_only.add(i)

        self._cb_threshold = int(cb_threshold if cb_threshold is not None else getattr(config, "RPC_CB_THRESHOLD", 5))
        self._cb_cooldown_s = float(cb_cooldown_s if cb_cooldown_s is not None else getattr(config, "RPC_CB_COOLDOWN_S", 30.0))
        self._cb_fail: List[int] = [0 for _ in self._clients]
        self._cb_open_until: List[float] = [0.0 for _ in self._clients]
        if max_retries_per_call is None:
            max_retries_per_call = int(getattr(config, "RPC_RETRY_COUNT", 1))
        self.max_retries_per_call = int(max_retries_per_call)

        # Latency EWMA per endpoint (ms). Start with a modest baseline so a new RPC isn't unfairly punished.
        self._lat_ewma_ms: List[float] = [350.0 for _ in self._clients]
        self._ewma_alpha = float(ewma_alpha)

        # In-flight counters for least-load selection
        self._inflight: List[int] = [0 for _ in self._clients]
        self._lock = asyncio.Lock()

        # Simple stats (useful for debugging)
        self._ok: List[int] = [0 for _ in self._clients]
        self._fail: List[int] = [0 for _ in self._clients]
        self._timeouts: List[int] = [0 for _ in self._clients]

        # Health windows + auto-ban
        self._health_window = int(getattr(config, "RPC_HEALTH_WINDOW", 50))
        self._health: List[EndpointHealth] = [EndpointHealth(self._health_window) for _ in self._clients]
        self._ban_until: List[float] = [0.0 for _ in self._clients]
        self._ban_reason: List[Optional[str]] = [None for _ in self._clients]
        self._ban_total = 0
        self._out_of_sync_bans_total = 0
        self._fallbacks_total = 0
        self._last_pinned_url: Optional[str] = None
        self._host_to_idx: Dict[str, int] = {_url_host(url): i for i, url in enumerate(self.urls)}
        self._pinned_missing_warned: Set[str] = set()

    async def close(self) -> None:
        for c in self._clients:
            try:
                await c.close()
            except Exception:
                pass

    def _resolve_idx(self, url: Optional[str]) -> Optional[int]:
        if not url:
            return None
        host = _url_host(url)
        return self._host_to_idx.get(host)

    def _health_stats(self, idx: int) -> Dict[str, Any]:
        try:
            return self._health[idx].stats()
        except Exception:
            return {
                "count": 0,
                "success_rate": None,
                "timeout_rate": None,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
            }

    def _health_score(self, idx: int) -> float:
        stats = self._health_stats(idx)
        success_rate = stats.get("success_rate")
        timeout_rate = stats.get("timeout_rate")
        p95 = stats.get("p95_latency_ms")
        base = float(success_rate) if success_rate is not None else 1.0
        penalty_latency = 0.0
        if p95 is not None:
            penalty_latency = min(float(p95) / 2000.0, 1.0) * 0.3
        penalty_timeouts = 0.0
        if timeout_rate is not None:
            penalty_timeouts = float(timeout_rate) * 0.7
        return float(base - penalty_latency - penalty_timeouts)

    def _should_ban(self, idx: int) -> bool:
        stats = self._health_stats(idx)
        timeout_rate = stats.get("timeout_rate")
        success_rate = stats.get("success_rate")
        p95 = stats.get("p95_latency_ms")
        max_timeout = float(getattr(config, "RPC_TIMEOUT_RATE_THRESHOLD", getattr(config, "RPC_BAN_TIMEOUT_RATE", 0.2)))
        min_success = float(getattr(config, "RPC_BAN_SUCCESS_RATE", 0.7))
        max_p95 = float(getattr(config, "RPC_LATENCY_P95_MS_THRESHOLD", getattr(config, "RPC_BAN_LATENCY_P95_MS", 2500.0)))
        if timeout_rate is not None and float(timeout_rate) > max_timeout:
            return True
        if success_rate is not None and float(success_rate) < min_success:
            return True
        if p95 is not None and float(p95) > max_p95:
            return True
        return False

    def _is_banned(self, idx: int, now_s: Optional[float] = None) -> bool:
        now = float(now_s if now_s is not None else time.time())
        return now < float(self._ban_until[idx])

    def _apply_ban_if_needed(self, idx: int, now_s: Optional[float] = None) -> None:
        if not self._should_ban(idx):
            return
        now = float(now_s if now_s is not None else time.time())
        ban_s = float(getattr(config, "RPC_HEALTH_BAN_SECONDS", getattr(config, "RPC_BAN_SECONDS", 60)))
        until = now + max(0.0, ban_s)
        if until > float(self._ban_until[idx]):
            self._ban_until[idx] = until
            self._ban_total += 1
            self._ban_reason[idx] = "health"

    def _record_health(self, idx: int, ok: bool, latency_ms: float, reason: str) -> None:
        try:
            self._health[idx].record(ok, latency_ms, reason)
            self._apply_ban_if_needed(idx)
        except Exception:
            return

    def pick_pinned_endpoint(self) -> Optional[str]:
        now = time.time()
        candidates = [i for i in range(len(self._clients)) if not self._is_cb_open(i, now) and not self._is_banned(i, now)]
        if not candidates:
            candidates = list(range(len(self._clients)))
        if self._fallback_only and candidates:
            non_fallback = [i for i in candidates if i not in self._fallback_only]
            if non_fallback:
                candidates = non_fallback
        best_idx = None
        best_score = None
        for i in candidates:
            score = self._health_score(i)
            if best_score is None or score > best_score:
                best_score = score
                best_idx = i
        if best_idx is None:
            return None
        self._last_pinned_url = self.urls[best_idx]
        return self.urls[best_idx]

    def mark_out_of_sync(self, url: Optional[str]) -> None:
        idx = self._resolve_idx(url)
        if idx is None:
            return
        self._record_health(idx, ok=False, latency_ms=0.0, reason="out_of_sync")
        now = time.time()
        ban_s = float(getattr(config, "RPC_OUT_OF_SYNC_BAN_SECONDS", getattr(config, "RPC_BAN_SECONDS", 60)))
        until = now + max(0.0, ban_s)
        if until > float(self._ban_until[idx]):
            self._ban_until[idx] = until
            self._ban_total += 1
            self._out_of_sync_bans_total += 1
            self._ban_reason[idx] = "out_of_sync"

    def _record_fallback(self, reason: Optional[str]) -> None:
        self._fallbacks_total += 1
        METRICS.inc("rpc_fallbacks_total", 1)
        if reason:
            METRICS.inc_reason("rpc_fallbacks_by_reason", str(reason), 1)

    def record_health_for_test(self, url: str, *, ok: bool, latency_ms: float, reason: str) -> None:
        idx = self._resolve_idx(url)
        if idx is None:
            return
        self._record_health(idx, ok=bool(ok), latency_ms=float(latency_ms), reason=str(reason))

    def is_banned(self, url: str, *, now_s: Optional[float] = None) -> bool:
        idx = self._resolve_idx(url)
        if idx is None:
            return False
        return self._is_banned(idx, now_s=now_s)

    def health_snapshot(self) -> Dict[str, Any]:
        endpoints: List[Dict[str, Any]] = []
        now = time.time()
        for i, url in enumerate(self.urls):
            stats = self._health_stats(i)
            endpoints.append({
                "url": url,
                "host": _url_host(url),
                "success_rate": stats.get("success_rate"),
                "timeout_rate": stats.get("timeout_rate"),
                "p50_latency_ms": stats.get("p50_latency_ms"),
                "p95_latency_ms": stats.get("p95_latency_ms"),
                "banned_until": float(self._ban_until[i]) if self._ban_until[i] and self._ban_until[i] > now else None,
            })
        return {
            "endpoints": endpoints,
            "bans_total": int(self._ban_total),
            "out_of_sync_bans_total": int(self._out_of_sync_bans_total),
            "fallbacks_total": int(self._fallbacks_total),
            "pinned_last": self._last_pinned_url,
        }

    def _score(self, idx: int) -> float:
        """Lower is better."""
        ok = self._ok[idx]
        fail = self._fail[idx]
        fail_rate = float(fail) / float(ok + fail + 1)
        # Higher weight = higher preference (more share).
        weight = max(0.1, float(self._priority_weights[idx]))
        return (float(self._inflight[idx]) / weight) + (self._lat_ewma_ms[idx] / 250.0) + (fail_rate * 6.0)

    def _is_cb_open(self, idx: int, now_s: Optional[float] = None) -> bool:
        now = float(now_s if now_s is not None else time.time())
        return now < float(self._cb_open_until[idx])

    async def _pick_idx(self, banned: Set[int], pinned_idx: Optional[int] = None) -> Optional[int]:
        async with self._lock:
            now = time.time()
            if pinned_idx is not None:
                if pinned_idx not in banned and not self._is_cb_open(pinned_idx, now) and not self._is_banned(pinned_idx, now):
                    self._inflight[pinned_idx] += 1
                    return pinned_idx
            candidates = [
                i
                for i in range(len(self._clients))
                if i not in banned and not self._is_cb_open(i, now) and not self._is_banned(i, now)
            ]
            if self._fallback_only and candidates:
                non_fallback = [i for i in candidates if i not in self._fallback_only]
                if non_fallback:
                    # Prefer non-fallback endpoints while they have capacity.
                    available = [i for i in non_fallback if self._inflight[i] < self._max_inflight[i]]
                    if available:
                        candidates = available

            best_idx: Optional[int] = None
            best_val: Optional[float] = None
            for i in candidates:
                val = self._score(i)
                if best_val is None or val < best_val:
                    best_val = val
                    best_idx = i
            if best_idx is None:
                return None
            self._inflight[best_idx] += 1
            return best_idx

    async def _done_idx(self, idx: int, ok: bool) -> None:
        async with self._lock:
            self._inflight[idx] = max(0, self._inflight[idx] - 1)
            if ok:
                self._ok[idx] += 1
                self._cb_fail[idx] = 0
                self._cb_open_until[idx] = 0.0
            else:
                self._fail[idx] += 1
                self._cb_fail[idx] += 1
                if self._cb_threshold > 0 and self._cb_fail[idx] >= self._cb_threshold:
                    self._cb_open_until[idx] = time.time() + float(self._cb_cooldown_s)
                    self._cb_fail[idx] = 0

    def stats(self) -> List[Dict[str, Any]]:
        """Return per-RPC counters."""
        out: List[Dict[str, Any]] = []
        for i, url in enumerate(self.urls):
            cb_open = self._is_cb_open(i)
            out.append({
                "url": url,
                "inflight": self._inflight[i],
                "ok": self._ok[i],
                "fail": self._fail[i],
                "timeout": self._timeouts[i],
                "lat_ms": round(float(self._lat_ewma_ms[i]), 1),
                "weight": float(self._priority_weights[i]),
                "fallback_only": bool(i in self._fallback_only),
                "cb_open": bool(cb_open),
                "banned_until": float(self._ban_until[i]) if self._ban_until[i] else None,
            })
        return out

    async def call(
        self,
        method: str,
        params: list,
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
        allow_error_data: bool = False,
        block_ctx: Optional[Any] = None,
    ) -> Any:
        pinned_url = None
        if block_ctx is not None:
            pinned_url = getattr(block_ctx, "pinned_http_endpoint", None)
        if not pinned_url:
            pinned_url = get_pinned_http_endpoint()
        pinned_idx = self._resolve_idx(pinned_url)
        if pinned_url and pinned_idx is None:
            METRICS.inc("pinned_violation_count", 1)
            if pinned_url not in self._pinned_missing_warned:
                self._pinned_missing_warned.add(pinned_url)
                print(f"[WARN] pinned endpoint not in pool: {pinned_url}")
        if pinned_idx is not None:
            self._last_pinned_url = self.urls[pinned_idx]

        async def _call_idx(idx: int) -> Any:
            c = self._clients[idx]
            self.last_url = self.urls[idx]
            t0 = time.perf_counter()
            try:
                async with self._sems[idx]:
                    res = await c.call(
                        method,
                        params,
                        timeout_s=timeout_s,
                        allow_revert_data=allow_revert_data,
                        allow_error_data=allow_error_data,
                    )
                dt_ms = (time.perf_counter() - t0) * 1000.0
                self._lat_ewma_ms[idx] = (1.0 - self._ewma_alpha) * self._lat_ewma_ms[idx] + self._ewma_alpha * float(dt_ms)
                await self._done_idx(idx, ok=True)
                self._record_health(idx, ok=True, latency_ms=float(dt_ms), reason="ok")
                return res
            except Exception as e:
                dt_ms = (time.perf_counter() - t0) * 1000.0
                reason = _normalize_rpc_error(e)
                if reason == "timeout":
                    self._timeouts[idx] += 1
                await self._done_idx(idx, ok=False)
                self._record_health(idx, ok=False, latency_ms=float(dt_ms), reason=reason)
                raise

        # Sticky mode: try pinned endpoint first, fall back only on timeout or health degradation.
        if pinned_idx is not None:
            banned: Set[int] = set()
            fallback_reason: Optional[str] = None
            last_err: Optional[Exception] = None
            if self._is_banned(pinned_idx):
                banned.add(pinned_idx)
                fallback_reason = self._ban_reason[pinned_idx] or "health_ban"
            else:
                try:
                    idx = await self._pick_idx(banned, pinned_idx=pinned_idx)
                    if idx is None:
                        banned.add(pinned_idx)
                    else:
                        return await _call_idx(idx)
                except Exception as e:
                    last_err = e
                    banned.add(pinned_idx)
                    allow_fallback = False
                    try:
                        reason = _normalize_rpc_error(e)
                        if reason == "timeout":
                            allow_fallback = True
                            fallback_reason = "timeout"
                    except Exception:
                        allow_fallback = False
                    if self._is_banned(pinned_idx):
                        allow_fallback = True
                        fallback_reason = self._ban_reason[pinned_idx] or fallback_reason or "health_ban"
                    if not allow_fallback:
                        raise last_err

            idx2 = await self._pick_idx(banned, pinned_idx=None)
            if idx2 is None:
                raise last_err or Exception("RPCPool call failed")
            self._record_fallback(fallback_reason or "fallback")
            try:
                self._last_pinned_url = self.urls[idx2]
                set_pinned_http_endpoint(self.urls[idx2])
            except Exception:
                pass
            return await _call_idx(idx2)

        # Non-sticky mode (legacy behavior)
        banned: Set[int] = set()
        attempts = 0
        last_err: Optional[Exception] = None
        max_total = len(self._clients) + self.max_retries_per_call

        while attempts < max_total:
            if len(banned) >= len(self._clients):
                break
            idx = await self._pick_idx(banned)
            if idx is None:
                break
            try:
                return await _call_idx(idx)
            except Exception as e:
                last_err = e
                banned.add(idx)
                attempts += 1
                try:
                    msg = str(e).lower()
                    if "http_429" in msg or "rate limit" in msg:
                        sleep_s = float(getattr(config, "RPC_RATE_LIMIT_BACKOFF_S", 0.35)) + random.random() * 0.25
                        await asyncio.sleep(sleep_s)
                except Exception:
                    pass
                continue

        raise last_err or Exception("RPCPool call failed")

    async def call_batch(
        self,
        method: str,
        params_list: List[list],
        *,
        timeout_s: Optional[float] = None,
        block_ctx: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        if not params_list:
            return []

        pinned_url = None
        if block_ctx is not None:
            pinned_url = getattr(block_ctx, "pinned_http_endpoint", None)
        if not pinned_url:
            pinned_url = get_pinned_http_endpoint()
        pinned_idx = self._resolve_idx(pinned_url)
        if pinned_url and pinned_idx is None:
            METRICS.inc("pinned_violation_count", 1)
            if pinned_url not in self._pinned_missing_warned:
                self._pinned_missing_warned.add(pinned_url)
                print(f"[WARN] pinned endpoint not in pool: {pinned_url}")
        if pinned_idx is not None:
            self._last_pinned_url = self.urls[pinned_idx]

        async def _call_idx(idx: int) -> List[Dict[str, Any]]:
            c = self._clients[idx]
            self.last_url = self.urls[idx]
            host = _url_host(self.urls[idx])
            METRICS.inc("rpc_requests_total", len(params_list))
            METRICS.inc_reason("rpc_requests_by_endpoint", host, len(params_list))
            t0 = time.perf_counter()
            try:
                async with self._sems[idx]:
                    res = await c.call_batch(method, params_list, timeout_s=timeout_s)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                METRICS.observe("rpc_latency_ms", float(dt_ms))
                METRICS.observe(f"rpc_latency_ms:{host}", float(dt_ms))
                self._lat_ewma_ms[idx] = (1.0 - self._ewma_alpha) * self._lat_ewma_ms[idx] + self._ewma_alpha * float(dt_ms)
                await self._done_idx(idx, ok=True)
                self._record_health(idx, ok=True, latency_ms=float(dt_ms), reason="ok")
                return res
            except Exception as e:
                dt_ms = (time.perf_counter() - t0) * 1000.0
                reason = _normalize_rpc_error(e)
                await self._done_idx(idx, ok=False)
                self._record_health(idx, ok=False, latency_ms=float(dt_ms), reason=reason)
                raise

        if pinned_idx is not None:
            banned: Set[int] = set()
            fallback_reason: Optional[str] = None
            if self._is_banned(pinned_idx):
                banned.add(pinned_idx)
                fallback_reason = self._ban_reason[pinned_idx] or "health_ban"
            else:
                try:
                    idx = await self._pick_idx(banned, pinned_idx=pinned_idx)
                    if idx is None:
                        banned.add(pinned_idx)
                    else:
                        return await _call_idx(idx)
                except Exception as e:
                    banned.add(pinned_idx)
                    allow_fallback = False
                    try:
                        reason = _normalize_rpc_error(e)
                        if reason == "timeout":
                            allow_fallback = True
                            fallback_reason = "timeout"
                    except Exception:
                        allow_fallback = False
                    if self._is_banned(pinned_idx):
                        allow_fallback = True
                        fallback_reason = self._ban_reason[pinned_idx] or fallback_reason or "health_ban"
                    if not allow_fallback:
                        raise

            idx2 = await self._pick_idx(banned, pinned_idx=None)
            if idx2 is None:
                raise Exception("RPCPool call_batch failed")
            self._record_fallback(fallback_reason or "fallback")
            try:
                self._last_pinned_url = self.urls[idx2]
                set_pinned_http_endpoint(self.urls[idx2])
            except Exception:
                pass
            return await _call_idx(idx2)

        banned: Set[int] = set()
        attempts = 0
        last_err: Optional[Exception] = None
        max_total = len(self._clients) + self.max_retries_per_call
        while attempts < max_total:
            if len(banned) >= len(self._clients):
                break
            idx = await self._pick_idx(banned)
            if idx is None:
                break
            try:
                return await _call_idx(idx)
            except Exception as e:
                last_err = e
                banned.add(idx)
                attempts += 1
                try:
                    msg = str(e).lower()
                    if "http_429" in msg or "rate limit" in msg:
                        sleep_s = float(getattr(config, "RPC_RATE_LIMIT_BACKOFF_S", 0.35)) + random.random() * 0.25
                        await asyncio.sleep(sleep_s)
                except Exception:
                    pass
                continue

        raise last_err or Exception("RPCPool call_batch failed")

    async def eth_call(
        self,
        to: str,
        data: str,
        block: str = "latest",
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
        allow_error_data: bool = False,
    ) -> str:
        return await self.call(
            "eth_call",
            [{"to": to, "data": data}, block],
            timeout_s=timeout_s,
            allow_revert_data=allow_revert_data,
            allow_error_data=allow_error_data,
        )

    async def get_block_number(self, *, timeout_s: Optional[float] = None) -> int:
        res = await self.call("eth_blockNumber", [], timeout_s=timeout_s)
        return int(res, 16)


# Backwards-compat alias (older code imports MultiAsyncRPC)
MultiAsyncRPC = RPCPool


# ------------------------
# Synchronous Web3 provider helper for fork_test.py

def get_provider(rpc_urls: Optional[Sequence[str]] = None) -> Web3:
    """Return a connected Web3 provider.

    Uses multiple URLs if provided (env RPC_URLS="a,b,c" or bot.config.RPC_URLS).
    Falls back to bot.config.RPC_URL.
    """

    urls = list(rpc_urls) if rpc_urls else get_rpc_urls()

    last_err: Optional[str] = None
    for url in urls:
        provider = Web3(Web3.HTTPProvider(url))
        try:
            if provider.is_connected():
                return provider
        except Exception as e:
            last_err = str(e)
            continue

    raise ConnectionError(f"Cannot connect to any RPC endpoint. Last error: {last_err}")
