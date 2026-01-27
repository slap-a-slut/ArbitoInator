const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawn, spawnSync } = require('child_process');

// ---------------------------------
// Bot process manager (fork_test.py)
let botProc = null;
let botStartedAtMs = null;
let currentRunId = null;
let startInProgress = false;

// Runtime UI config (stored in project root)
const projectRoot = path.join(__dirname, '..');
const configPath = path.join(projectRoot, 'bot_config.json');
const uiStatePath = path.join(projectRoot, 'logs', 'ui_state.json');
const presetsDir = path.join(projectRoot, 'presets');
const mempoolStatusPath = path.join(projectRoot, 'logs', 'mempool_status.json');
const mempoolRecentPath = path.join(projectRoot, 'logs', 'mempool_recent.json');
const mempoolTriggersPath = path.join(projectRoot, 'logs', 'mempool_triggers.json');
const mempoolLogPath = path.join(projectRoot, 'logs', 'mempool.jsonl');
const triggerLogPath = path.join(projectRoot, 'logs', 'trigger_scans.jsonl');
const diagnosticPath = path.join(projectRoot, 'logs', 'diagnostic_snapshot.json');
const assistantPackPath = path.join(projectRoot, 'logs', 'assistant_pack.json');
const runLockPath = path.join(projectRoot, 'logs', 'bot_run.lock');
const UI_STATE_MAX_BYTES = 512 * 1024;

const DEFAULT_CONFIG = {
  // RPC failover (comma/newline separated in UI; stored as list)
  rpc_urls: [
    'https://ethereum-rpc.publicnode.com',
    'https://eth.llamarpc.com',
    'https://rpc.ankr.com/eth',
    'https://go.getblock.us',
    'https://eth.merkle.io',
    'https://rpc.flashbots.net',
  ],
  rpc_http_urls: [
    'https://ethereum-rpc.publicnode.com',
    'https://eth.llamarpc.com',
    'https://rpc.ankr.com/eth',
    'https://go.getblock.us',
    'https://eth.merkle.io',
    'https://rpc.flashbots.net',
  ],
  rpc_ws_urls: [],
  rpc_http_endpoints: [],
  rpc_ws_endpoints: [],
  rpc_ws_pairing: {},
  dexes: ['univ3'],

  // thresholds
  min_profit_pct: 0.05,
  min_profit_abs: 0.05,
  slippage_bps: 8,
  mev_buffer_bps: 5,
  max_gas_gwei: null,

  // performance
  concurrency: 10,
  block_budget_s: 10,
  prepare_budget_ratio: 0.2,
  prepare_budget_min_s: 2,
  prepare_budget_max_s: 6,
  max_candidates_stage1: 200,
  max_total_expanded: 400,
  max_expanded_per_candidate: 6,
  rpc_timeout_s: 3,
  rpc_retry_count: 1,
  rpc_batch_eth_calls: true,
  rpc_batch_max_calls: 80,
  rpc_batch_flush_ms: 8,

  // multidex / routing
  enable_multidex: false,
  max_hops: 3,
  beam_k: 20,
  edge_top_m: 2,
  trigger_prefer_cross_dex: true,
  trigger_require_cross_dex: false,
  trigger_require_three_hops: true,
  trigger_base_fallback_enabled: true,
  trigger_allow_two_hop_fallback: true,
  trigger_cross_dex_fallback: true,
  trigger_connectors: ['WETH', 'USDC', 'USDT', 'DAI'],
  trigger_max_candidates_raw: 80,
  trigger_prepare_budget_ms: 250,
  trigger_probe_budget_ms: 0,
  trigger_refine_budget_ms: 0,
  trigger_probe_budget_ratio: 0.4,
  trigger_probe_top_k: 12,
  trigger_probe_gas_units: 180000,
  trigger_probe_min_net: 0,
  trigger_probe_amounts_usdc: [50, 100],
  trigger_probe_amounts_weth: [0.005, 0.01],
  trigger_cross_dex_bonus_bps: 5,
  trigger_same_dex_penalty_bps: 5,
  trigger_edge_top_m_per_dex: 2,
  probe_amount: 1,

  // modes
  scan_mode: 'auto', // auto|fixed
  scan_source: 'block', // block|mempool|hybrid

  // fixed mode
  amount_presets: [1, 5],

  // auto mode (stage1/stage2)
  stage1_amount: 1,
  stage1_fee_tiers: [500, 3000],
  stage2_top_k: 30,
  stage2_amount_min: 0.5,
  stage2_amount_max: 50,
  stage2_max_evals: 6,

  // rpc timeouts
  rpc_timeout_stage1_s: 3,
  rpc_timeout_stage2_s: 4,
  rpc_health_ban_seconds: 60,
  rpc_timeout_rate_threshold: 0.2,
  rpc_latency_p95_ms_threshold: 2500,
  out_of_sync_ban_seconds: 60,
  tx_fetch_batch_enabled: true,
  tx_fetch_max_retries: 3,
  tx_fetch_retry_backoff_ms: [200, 500, 1000],
  tx_fetch_per_endpoint_max_inflight: 4,

  // V2 filters
  v2_min_reserve_ratio: 20,
  v2_max_price_impact_bps: 300,

  // Debug funnel / simulation controls
  sim_profile: '',
  debug_funnel: false,
  gas_off: false,
  fixed_gas_units: 0,
  sim_backend: 'quote',
  execution_mode: 'off',
  arb_executor_address: '',
  arb_executor_owner: '',

  // mempool (optional)
  mempool_enabled: false,
  mempool_ws_urls: [
    'wss://ethereum.publicnode.com',
    'wss://eth.llamarpc.com/ws',
  ],
  mempool_max_inflight_tx: 200,
  mempool_fetch_tx_concurrency: 20,
  mempool_filter_to: [],
  mempool_watch_mode: 'strict',
  mempool_watched_router_sets: 'core',
  mempool_min_value_usd: 25,
  mempool_allow_unknown_tokens: true,
  mempool_strict_unknown_tokens: false,
  mempool_raw_min_enabled: false,
  mempool_usd_per_eth: 2000,
  mempool_dedup_ttl_s: 120,
  mempool_trigger_scan_budget_s: 1.5,
  mempool_trigger_max_queue: 50,
  mempool_trigger_max_concurrent: 1,
  mempool_trigger_ttl_s: 60,
  mempool_confirm_timeout_s: 2,
  mempool_post_scan_budget_s: 1,

  // UI reporting/base currency. We force scanning cycles that start/end in this token
  // so the web panel never mixes numbers with a different symbol.
  report_currency: 'USDC', // USDC|USDT

  verbose: false,
};

const DEFAULT_UI_PORT = 8080;

const PRESET_ORDER = [
  'smoke',
  'fast_rpc_sanity',
  'balanced',
  'coverage_heavy',
  'stress',
  'realistic_ish',
];

const PRESET_ALIASES = {
  dex_adapters: 'dexes',
  enable_multidex_beam: 'enable_multidex',
  reporting_currency: 'report_currency',
  amounts: 'amount_presets',
  slippage_safety_bps: 'slippage_bps',
};

const KNOWN_SETTING_KEYS = new Set(Object.keys(DEFAULT_CONFIG));

function resolvePython() {
  const venvPython = path.join(projectRoot, 'venv', 'bin', 'python');
  return process.env.PYTHON || (fs.existsSync(venvPython) ? venvPython : 'python3');
}

function resolveUiPort() {
  const raw = process.env.UI_PORT || process.env.PORT || DEFAULT_UI_PORT;
  const parsed = Number.parseInt(String(raw), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_UI_PORT;
  return parsed;
}

let uiPort = resolveUiPort();

function _asNumberArray(value) {
  if (Array.isArray(value)) {
    return value.map((v) => Number(v)).filter((v) => Number.isFinite(v));
  }
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((v) => Number(String(v).trim()))
      .filter((v) => Number.isFinite(v));
  }
  return null;
}

function _asStringArray(value) {
  if (Array.isArray(value)) {
    return value.map((v) => String(v).trim()).filter((v) => v.length > 0);
  }
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((v) => String(v).trim())
      .filter((v) => v.length > 0);
  }
  return null;
}

const ENDPOINT_HINTS = [
  { id: 'publicnode', match: ['publicnode.com'] },
  { id: 'llama', match: ['llamarpc.com'] },
  { id: 'ankr', match: ['rpc.ankr.com'] },
  { id: 'flashbots_calls', match: ['flashbots.net'] },
  { id: 'getblock', match: ['getblock.us'] },
  { id: 'merkle', match: ['merkle.io'] },
  { id: '0xrpc', match: ['0xrpc.io'] },
];

function endpointIdFromUrl(url) {
  const raw = String(url || '').trim().toLowerCase();
  const host = raw.includes('://') ? raw.split('://', 2)[1].split('/', 1)[0] : raw.split('/', 1)[0];
  for (const hint of ENDPOINT_HINTS) {
    if (hint.match.some((m) => host.includes(m))) return hint.id;
  }
  if (!host) return 'rpc';
  return host.replace(/[:/]/g, '_');
}

function buildEndpointsFromUrls(urls) {
  const list = [];
  const seen = new Set();
  (urls || []).forEach((u) => {
    const url = String(u || '').trim();
    if (!url) return;
    let id = endpointIdFromUrl(url);
    if (seen.has(id)) {
      let i = 2;
      while (seen.has(`${id}_${i}`)) i += 1;
      id = `${id}_${i}`;
    }
    seen.add(id);
    list.push({ id, url });
  });
  return list;
}

function normalizeEndpoints(raw, fallbackUrls) {
  if (Array.isArray(raw) && raw.length > 0) {
    return raw
      .map((e) => {
        if (typeof e === 'string') return { id: endpointIdFromUrl(e), url: e };
        if (!e || typeof e !== 'object') return null;
        const url = String(e.url || '').trim();
        if (!url) return null;
        const id = String(e.id || '').trim() || endpointIdFromUrl(url);
        return { id, url };
      })
      .filter(Boolean);
  }
  return buildEndpointsFromUrls(fallbackUrls || []);
}

function buildWsHttpPairing(wsEndpoints, httpEndpoints) {
  const pairing = {};
  const httpById = {};
  const httpByHost = {};
  (httpEndpoints || []).forEach((ep) => {
    if (ep && ep.id) httpById[String(ep.id)] = ep.id;
    const host = String(ep.url || '').trim().toLowerCase().split('://').pop().split('/')[0];
    if (host) httpByHost[host] = ep.id;
  });
  (wsEndpoints || []).forEach((ep) => {
    const host = String(ep.url || '').trim().toLowerCase().split('://').pop().split('/')[0];
    if (!host) return;
    const httpId = httpByHost[host] || httpById[ep.id];
    if (httpId) pairing[ep.id] = httpId;
  });
  return pairing;
}

function normalizePresetSettings(raw, presetId, fileName) {
  const out = {};
  const warnings = [];
  if (!raw || typeof raw !== 'object') {
    warnings.push('settings is not an object');
    return { settings: out, warnings };
  }

  Object.entries(raw).forEach(([key, value]) => {
    if (key === 'stage2_amount_range') {
      const range = Array.isArray(value) ? value : null;
      if (!range || range.length < 2) {
        warnings.push('stage2_amount_range must be [min,max]');
        return;
      }
      const min = Number(range[0]);
      const max = Number(range[1]);
      if (Number.isFinite(min)) out.stage2_amount_min = min;
      else warnings.push('stage2_amount_range min is invalid');
      if (Number.isFinite(max)) out.stage2_amount_max = max;
      else warnings.push('stage2_amount_range max is invalid');
      return;
    }

    if (Object.prototype.hasOwnProperty.call(PRESET_ALIASES, key)) {
      const mapped = PRESET_ALIASES[key];
      if (mapped === 'dexes') {
        const list = _asStringArray(value);
        if (list) out[mapped] = list;
        else warnings.push('dex_adapters must be array or csv');
        return;
      }
      if (mapped === 'amount_presets') {
        const list = _asNumberArray(value);
        if (list) out[mapped] = list;
        else warnings.push('amounts must be array or csv');
        return;
      }
      out[mapped] = value;
      return;
    }

    if (KNOWN_SETTING_KEYS.has(key)) {
      out[key] = value;
      return;
    }

    warnings.push(`unknown key "${key}"`);
  });

  if (warnings.length) {
    const label = presetId ? `${presetId}` : 'unknown';
    const fileLabel = fileName ? ` (${fileName})` : '';
    console.warn(`[presets] ${label}${fileLabel}: ${warnings.join('; ')}`);
  }

  return { settings: out, warnings };
}

function loadPresetsFromDisk() {
  const presets = [];
  const byId = {};
  if (!fs.existsSync(presetsDir)) return { presets, byId };
  const files = fs.readdirSync(presetsDir).filter((f) => f.endsWith('.json')).sort();
  files.forEach((file) => {
    const full = path.join(presetsDir, file);
    let raw;
    try {
      raw = JSON.parse(fs.readFileSync(full, 'utf8'));
    } catch (e) {
      console.warn(`[presets] failed to parse ${file}: ${String(e)}`);
      return;
    }
    if (!raw || typeof raw !== 'object') {
      console.warn(`[presets] invalid JSON object in ${file}`);
      return;
    }
    const id = String(raw.id || '').trim();
    const name = String(raw.name || '').trim();
    const description = String(raw.description || '').trim();
    if (!id || !name || !raw.settings) {
      console.warn(`[presets] missing id/name/settings in ${file}`);
      return;
    }
    if (byId[id]) {
      console.warn(`[presets] duplicate id "${id}" in ${file}`);
      return;
    }
    const normalized = normalizePresetSettings(raw.settings, id, file);
    const preset = {
      id,
      name,
      description,
      settings: normalized.settings,
    };
    byId[id] = preset;
    presets.push(preset);
  });

  presets.sort((a, b) => {
    const ia = PRESET_ORDER.indexOf(a.id);
    const ib = PRESET_ORDER.indexOf(b.id);
    if (ia !== -1 || ib !== -1) {
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    }
    return String(a.name).localeCompare(String(b.name));
  });

  return { presets, byId };
}

function readConfig() {
  try {
    if (!fs.existsSync(configPath)) return { ...DEFAULT_CONFIG };
    const raw = fs.readFileSync(configPath, 'utf8') || '{}';
    const parsed = JSON.parse(raw);
    const cfg = { ...DEFAULT_CONFIG, ...parsed };
    const httpEndpoints = normalizeEndpoints(parsed.rpc_http_endpoints, cfg.rpc_http_urls);
    cfg.rpc_http_endpoints = httpEndpoints;
    if (httpEndpoints.length) {
      cfg.rpc_http_urls = httpEndpoints.map((e) => e.url).filter(Boolean);
      cfg.rpc_urls = cfg.rpc_http_urls;
    }
    const wsEndpoints = normalizeEndpoints(parsed.rpc_ws_endpoints, parsed.rpc_ws_urls || parsed.mempool_ws_urls || cfg.rpc_ws_urls);
    cfg.rpc_ws_endpoints = wsEndpoints;
    if (wsEndpoints.length) {
      cfg.rpc_ws_urls = wsEndpoints.map((e) => e.url).filter(Boolean);
      if (!Array.isArray(parsed.mempool_ws_urls) || parsed.mempool_ws_urls.length === 0) {
        cfg.mempool_ws_urls = cfg.rpc_ws_urls;
      }
    }
    if (!cfg.rpc_ws_pairing || typeof cfg.rpc_ws_pairing !== 'object') {
      cfg.rpc_ws_pairing = buildWsHttpPairing(cfg.rpc_ws_endpoints, cfg.rpc_http_endpoints);
    }
    const hasHttp = Object.prototype.hasOwnProperty.call(parsed || {}, 'rpc_http_urls');
    if (!hasHttp || !Array.isArray(cfg.rpc_http_urls) || cfg.rpc_http_urls.length === 0) {
      cfg.rpc_http_urls = Array.isArray(cfg.rpc_urls) ? [...cfg.rpc_urls] : [];
    }
    return cfg;
  } catch (e) {
    return { ...DEFAULT_CONFIG };
  }
}

function writeConfig(cfg) {
  const cleaned = { ...(cfg || {}) };

  // If UI sends empty strings / nulls, keep defaults instead of breaking the bot.
  for (const k of Object.keys(cleaned)) {
    if (cleaned[k] === '' || cleaned[k] === null) delete cleaned[k];
  }

  // Parse comma-list amounts into numbers
  if (typeof cleaned.amount_presets === 'string') {
    cleaned.amount_presets = cleaned.amount_presets
      .split(',')
      .map((x) => Number(String(x).trim()))
      .filter((x) => Number.isFinite(x) && x > 0);
  }

  // Parse rpc_urls from textarea/input into list
  if (typeof cleaned.rpc_urls === 'string') {
    cleaned.rpc_urls = cleaned.rpc_urls
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0);
  }
  if (typeof cleaned.rpc_http_urls === 'string') {
    cleaned.rpc_http_urls = cleaned.rpc_http_urls
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0);
  }
  if (typeof cleaned.rpc_ws_urls === 'string') {
    cleaned.rpc_ws_urls = cleaned.rpc_ws_urls
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0);
  }

  // Parse dexes from comma list into array
  if (typeof cleaned.dexes === 'string') {
    cleaned.dexes = cleaned.dexes
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim().toLowerCase())
      .filter((x) => x.length > 0);
  }

  // Parse mempool WS urls from textarea/input into list
  if (typeof cleaned.mempool_ws_urls === 'string') {
    cleaned.mempool_ws_urls = cleaned.mempool_ws_urls
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0);
  }

  // Parse mempool filter list
  if (typeof cleaned.mempool_filter_to === 'string') {
    cleaned.mempool_filter_to = cleaned.mempool_filter_to
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim().toLowerCase())
      .filter((x) => x.length > 0);
  }

  const safe = { ...DEFAULT_CONFIG, ...cleaned };

  const hasHttp = Object.prototype.hasOwnProperty.call(cleaned || {}, 'rpc_http_urls');
  if (!hasHttp || !Array.isArray(safe.rpc_http_urls) || safe.rpc_http_urls.length === 0) {
    safe.rpc_http_urls = Array.isArray(safe.rpc_urls) ? [...safe.rpc_urls] : [];
  }
  if (!Array.isArray(safe.rpc_urls) || safe.rpc_urls.length === 0) {
    safe.rpc_urls = Array.isArray(safe.rpc_http_urls) ? [...safe.rpc_http_urls] : [];
  }

  safe.rpc_http_endpoints = normalizeEndpoints(safe.rpc_http_endpoints, safe.rpc_http_urls);
  safe.rpc_ws_endpoints = normalizeEndpoints(safe.rpc_ws_endpoints, safe.rpc_ws_urls || safe.mempool_ws_urls);
  if (safe.rpc_http_endpoints.length) {
    safe.rpc_http_urls = safe.rpc_http_endpoints.map((e) => e.url).filter(Boolean);
    safe.rpc_urls = [...safe.rpc_http_urls];
  }
  if (safe.rpc_ws_endpoints.length) {
    safe.rpc_ws_urls = safe.rpc_ws_endpoints.map((e) => e.url).filter(Boolean);
    if (!Array.isArray(safe.mempool_ws_urls) || safe.mempool_ws_urls.length === 0) {
      safe.mempool_ws_urls = [...safe.rpc_ws_urls];
    }
  }
  if (!safe.rpc_ws_pairing || typeof safe.rpc_ws_pairing !== 'object') {
    safe.rpc_ws_pairing = buildWsHttpPairing(safe.rpc_ws_endpoints, safe.rpc_http_endpoints);
  }

  // Normalize report currency
  if (typeof safe.report_currency === 'string') {
    const rc = String(safe.report_currency).trim().toUpperCase();
    safe.report_currency = (rc === 'USDT') ? 'USDT' : 'USDC';
  } else {
    safe.report_currency = 'USDC';
  }

  // Normalize mempool watch mode/preset
  const watchMode = String(safe.mempool_watch_mode || 'strict').trim().toLowerCase();
  safe.mempool_watch_mode = (watchMode === 'routers_only') ? 'routers_only' : 'strict';
  const watchPreset = String(safe.mempool_watched_router_sets || 'core').trim().toLowerCase();
  safe.mempool_watched_router_sets = (watchPreset === 'extended') ? 'extended' : 'core';
  const simBackend = String(safe.sim_backend || 'quote').trim().toLowerCase();
  safe.sim_backend = (simBackend === 'eth_call' || simBackend === 'state_override') ? simBackend : 'quote';
  const execMode = String(safe.execution_mode || 'off').trim().toLowerCase();
  safe.execution_mode = (execMode === 'dryrun') ? 'dryrun' : 'off';

  // Normalize debug flags + profile
  const normBool = (v) => {
    if (typeof v === 'boolean') return v;
    if (typeof v === 'number') return v !== 0;
    if (typeof v === 'string') {
      return ['1', 'true', 'yes', 'on'].includes(v.trim().toLowerCase());
    }
    return false;
  };
  safe.debug_funnel = normBool(safe.debug_funnel);
  safe.gas_off = normBool(safe.gas_off);
  safe.enable_multidex = normBool(safe.enable_multidex);
  safe.trigger_prefer_cross_dex = normBool(safe.trigger_prefer_cross_dex);
  safe.trigger_require_cross_dex = normBool(safe.trigger_require_cross_dex);
  safe.trigger_require_three_hops = normBool(safe.trigger_require_three_hops);
  safe.trigger_base_fallback_enabled = normBool(safe.trigger_base_fallback_enabled);
  safe.trigger_allow_two_hop_fallback = normBool(safe.trigger_allow_two_hop_fallback);
  safe.trigger_cross_dex_fallback = normBool(safe.trigger_cross_dex_fallback);
  safe.mempool_enabled = normBool(safe.mempool_enabled);
  safe.mempool_allow_unknown_tokens = normBool(safe.mempool_allow_unknown_tokens);
  safe.mempool_strict_unknown_tokens = normBool(safe.mempool_strict_unknown_tokens);
  safe.mempool_raw_min_enabled = normBool(safe.mempool_raw_min_enabled);

  if (typeof safe.sim_profile === 'string') {
    const sp = safe.sim_profile.trim().toLowerCase();
    safe.sim_profile = (sp === 'debug') ? 'debug' : '';
  } else {
    safe.sim_profile = '';
  }

  try {
    const fg = Number(safe.fixed_gas_units || 0);
    safe.fixed_gas_units = (Number.isFinite(fg) && fg > 0) ? Math.round(fg) : 0;
  } catch {
    safe.fixed_gas_units = 0;
  }

  try {
    safe.max_hops = Math.max(2, Math.min(4, parseInt(safe.max_hops || 3, 10)));
  } catch {
    safe.max_hops = 3;
  }
  try {
    safe.beam_k = Math.max(1, Math.min(50, parseInt(safe.beam_k || 20, 10)));
  } catch {
    safe.beam_k = 20;
  }
  try {
    safe.edge_top_m = Math.max(1, Math.min(5, parseInt(safe.edge_top_m || 2, 10)));
  } catch {
    safe.edge_top_m = 2;
  }
  try {
    safe.trigger_edge_top_m_per_dex = Math.max(1, Math.min(5, parseInt(safe.trigger_edge_top_m_per_dex || 2, 10)));
  } catch {
    safe.trigger_edge_top_m_per_dex = 2;
  }
  try {
    safe.trigger_max_candidates_raw = Math.max(10, Math.min(300, parseInt(safe.trigger_max_candidates_raw || 80, 10)));
  } catch {
    safe.trigger_max_candidates_raw = 80;
  }
  try {
    safe.trigger_prepare_budget_ms = Math.max(50, Math.min(2000, parseInt(safe.trigger_prepare_budget_ms || 250, 10)));
  } catch {
    safe.trigger_prepare_budget_ms = 250;
  }
  if (typeof safe.trigger_connectors === 'string') {
    safe.trigger_connectors = safe.trigger_connectors
      .replace(/\n/g, ',')
      .split(',')
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0);
  }
  try {
    const src = String(safe.scan_source || 'block').trim().toLowerCase();
    safe.scan_source = ['block', 'mempool', 'hybrid'].includes(src) ? src : 'block';
  } catch {
    safe.scan_source = 'block';
  }
  try {
    const probe = Number(safe.probe_amount || 1);
    safe.probe_amount = (Number.isFinite(probe) && probe > 0) ? probe : 1;
  } catch {
    safe.probe_amount = 1;
  }
  try {
    const bonus = Number(safe.trigger_cross_dex_bonus_bps || 0);
    safe.trigger_cross_dex_bonus_bps = Number.isFinite(bonus) ? bonus : 0;
  } catch {
    safe.trigger_cross_dex_bonus_bps = 0;
  }
  try {
    const penalty = Number(safe.trigger_same_dex_penalty_bps || 0);
    safe.trigger_same_dex_penalty_bps = Number.isFinite(penalty) ? penalty : 0;
  } catch {
    safe.trigger_same_dex_penalty_bps = 0;
  }

  fs.writeFileSync(configPath, JSON.stringify(safe, null, 2));
  return safe;
}

function buildAssistantPack() {
  const python = resolvePython();
  const out = spawnSync(python, ['-m', 'bot.assistant_pack', '--write'], {
    cwd: projectRoot,
    encoding: 'utf8',
  });
  if (out.status !== 0) {
    return { ok: false, error: String(out.stderr || out.stdout || 'assistant_pack failed') };
  }
  const pack = readJsonSafe(assistantPackPath);
  if (!pack) {
    return { ok: false, error: 'assistant_pack not found' };
  }
  return { ok: true, pack };
}

function readUiState() {
  try {
    if (!fs.existsSync(uiStatePath)) return null;
    const stat = fs.statSync(uiStatePath);
    if (stat.size > UI_STATE_MAX_BYTES) return null;
    const raw = fs.readFileSync(uiStatePath, 'utf8') || '';
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed;
  } catch (e) {
    return null;
  }
}

function writeUiState(state) {
  if (!state || typeof state !== 'object') return { ok: false, error: 'invalid state' };
  try { fs.mkdirSync(path.dirname(uiStatePath), { recursive: true }); } catch {}
  try {
    fs.writeFileSync(uiStatePath, JSON.stringify(state));
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function readJsonSafe(filePath, maxBytes = 256 * 1024) {
  try {
    if (!fs.existsSync(filePath)) return null;
    const stat = fs.statSync(filePath);
    if (stat.size > maxBytes) return null;
    const raw = fs.readFileSync(filePath, 'utf8') || '';
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed;
  } catch (e) {
    return null;
  }
}

function isPidAlive(pid) {
  if (!pid || Number(pid) <= 0) return false;
  try {
    process.kill(Number(pid), 0);
    return true;
  } catch (err) {
    return false;
  }
}

function readRunLock() {
  try {
    if (!fs.existsSync(runLockPath)) return null;
    const raw = fs.readFileSync(runLockPath, 'utf8') || '';
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') return parsed;
  } catch {}
  return null;
}

function clearRunLockIfStale() {
  const lock = readRunLock();
  if (!lock) return null;
  const pid = Number(lock.pid || 0);
  if (pid && isPidAlive(pid)) {
    return { alive: true, pid, run_id: lock.run_id || null };
  }
  try { fs.unlinkSync(runLockPath); } catch {}
  return { alive: false, pid, run_id: lock.run_id || null };
}

function killProcessTree(proc, signal = 'SIGINT') {
  if (!proc || !proc.pid) return;
  if (process.platform === 'win32') {
    try {
      spawnSync('taskkill', ['/PID', String(proc.pid), '/T', '/F']);
    } catch {}
    return;
  }
  try {
    process.kill(-proc.pid, signal);
  } catch {
    try { process.kill(proc.pid, signal); } catch {}
  }
}

function isRunning() {
  return !!(botProc && !botProc.killed);
}

function nowTime() {
  return new Date().toISOString().slice(11, 19);
}

function broadcast(data) {
  const msg = JSON.stringify(data);
  wss.clients.forEach((client) => {
    if (client.readyState === WebSocket.OPEN) client.send(msg);
  });
}

function resetMempoolArtifacts() {
  try { fs.writeFileSync(mempoolRecentPath, JSON.stringify([])); } catch {}
  try { fs.writeFileSync(mempoolTriggersPath, JSON.stringify([])); } catch {}
  try { fs.writeFileSync(mempoolStatusPath, JSON.stringify({})); } catch {}
  try { fs.writeFileSync(mempoolLogPath, ''); } catch {}
  try { fs.writeFileSync(triggerLogPath, ''); } catch {}
}

function startBot() {
  if (startInProgress) return { ok: false, error: 'start already in progress' };
  if (isRunning()) return { ok: false, alreadyRunning: true };
  const lockState = clearRunLockIfStale();
  if (lockState && lockState.alive) {
    broadcast({
      type: 'status',
      time: nowTime(),
      block: null,
      text: `RUN_ALREADY_RUNNING run_id=${lockState.run_id || 'unknown'} pid=${lockState.pid}`,
    });
    return { ok: false, alreadyRunning: true, run_id: lockState.run_id || null, pid: lockState.pid };
  }
  resetMempoolArtifacts();
  startInProgress = true;

  const script = path.join(projectRoot, 'fork_test.py');
  const python = resolvePython();

  // Ensure Python dependencies exist (so we don't crash on ModuleNotFoundError: web3)
  // This keeps the project "works out of the box" when the bot is launched from the UI.
  const reqFile = path.join(projectRoot, 'requirements.txt');
  try {
    const check = spawnSync(python, ['-c', 'import web3'], { cwd: projectRoot });
    if (check.status !== 0) {
      if (python === 'python3') {
        broadcast({
          type: 'status',
          time: nowTime(),
          block: null,
          text: 'Missing Python deps. Use the project venv or set PYTHON to a venv path.',
        });
        startInProgress = false;
        return { ok: false, error: 'missing python deps (use venv)' };
      }
      broadcast({ type: 'status', time: nowTime(), block: null, text: 'Installing Python dependencies (pip install -r requirements.txt)...' });
      const install = spawnSync(python, ['-m', 'pip', 'install', '-r', reqFile], {
        cwd: projectRoot,
        encoding: 'utf8',
      });
      const out = String(install.stdout || '');
      const err = String(install.stderr || '');
      out.split(/\r?\n/).filter(Boolean).forEach((line) => broadcast({ type: 'stdout', time: nowTime(), block: null, text: line }));
      err.split(/\r?\n/).filter(Boolean).forEach((line) => broadcast({ type: 'stderr', time: nowTime(), block: null, text: line }));

      if (install.status !== 0) {
        broadcast({ type: 'status', time: nowTime(), block: null, text: 'Dependency install failed. See stderr above.' });
        startInProgress = false;
        return { ok: false, error: 'pip install -r requirements.txt failed' };
      }
    }
  } catch (e) {
    broadcast({ type: 'stderr', time: nowTime(), block: null, text: `Dependency check failed: ${String(e)}` });
  }

  const cfg = readConfig();

  botStartedAtMs = Date.now();
  currentRunId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  botProc = spawn(python, ['-u', script], {
    cwd: projectRoot,
    // Keep both env vars for backwards compatibility.
    env: {
      ...process.env,
      PYTHON: python,
      RUN_ID: String(currentRunId || ''),
      UI_PORT: String(uiPort || DEFAULT_UI_PORT),
      BOT_CONFIG: configPath,
      ARBITOINATOR_CONFIG: configPath,
      // Multi-RPC failover list for Python
      RPC_URLS: Array.isArray(cfg.rpc_urls) ? cfg.rpc_urls.join(',') : String(cfg.rpc_urls || ''),
      RPC_HTTP_URLS: Array.isArray(cfg.rpc_http_urls) ? cfg.rpc_http_urls.join(',') : String(cfg.rpc_http_urls || ''),
      RPC_WS_URLS: Array.isArray(cfg.rpc_ws_urls) ? cfg.rpc_ws_urls.join(',') : String(cfg.rpc_ws_urls || ''),
      REPORT_CURRENCY: String(cfg.report_currency || 'USDC'),
      DEBUG_FUNNEL: cfg.debug_funnel ? '1' : '0',
      SIM_PROFILE: String(cfg.sim_profile || ''),
      GAS_OFF: cfg.gas_off ? '1' : '0',
      FIXED_GAS_UNITS: (cfg.fixed_gas_units && Number(cfg.fixed_gas_units) > 0) ? String(cfg.fixed_gas_units) : '',
      ENABLE_MULTIDEX: cfg.enable_multidex ? '1' : '0',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
  });

  broadcast({
    type: 'status',
    time: nowTime(),
    block: null,
    text: 'Bot started',
    running: true,
    started_at_ms: botStartedAtMs,
    run_id: currentRunId,
  });

  const forwardLines = (buf, stream) => {
    String(buf)
      .split(/\r?\n/)
      .filter(Boolean)
      .forEach((line) => {
        broadcast({
          type: stream === 'stderr' ? 'stderr' : 'stdout',
          time: nowTime(),
          block: null,
          text: line,
        });
      });
  };

  botProc.stdout.on('data', (buf) => forwardLines(buf, 'stdout'));
  botProc.stderr.on('data', (buf) => forwardLines(buf, 'stderr'));

  botProc.on('close', (code, signal) => {
    botStartedAtMs = null;
    currentRunId = null;
    startInProgress = false;
    broadcast({
      type: 'status',
      time: nowTime(),
      block: null,
      text: `Bot stopped (code=${code}, signal=${signal || 'none'})`,
      running: false,
      started_at_ms: null,
      run_id: currentRunId,
    });
    botProc = null;
  });

  startInProgress = false;
  return { ok: true, run_id: currentRunId };
}

function stopBot() {
  if (!isRunning()) return { ok: true, alreadyStopped: true };
  try {
    killProcessTree(botProc, 'SIGINT');
    // close handler will clear started_at; keep it for clients until then
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// Stop and WAIT until the process fully exits (so restart applies new env/config)
function stopBotWait(timeoutMs = 12000) {
  return new Promise((resolve) => {
    if (!isRunning()) return resolve({ ok: true, alreadyStopped: true });

    const proc = botProc;
    let done = false;

    const finish = (out) => {
      if (done) return;
      done = true;
      try { proc.removeAllListeners('close'); } catch {}
      resolve(out);
    };

    // If already closing, just wait.
    proc.once('close', () => finish({ ok: true }));

    try {
      killProcessTree(proc, 'SIGINT');
    } catch (e) {
      return finish({ ok: false, error: String(e) });
    }

    // Hard timeout safety: if SIGINT doesn't stop it, SIGKILL.
    setTimeout(() => {
      if (done) return;
      try { killProcessTree(proc, 'SIGKILL'); } catch {}
      // Give it a moment to emit close
      setTimeout(() => finish({ ok: true, forced: true }), 750);
    }, timeoutMs);
  });
}

// ---------------------------------
// HTTP server
const server = http.createServer((req, res) => {
  // Read config
  if (req.method === 'GET' && req.url === '/config') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, config: readConfig() }));
    return;
  }

  const urlPath = String(req.url || '').split('?')[0];

  if (req.method === 'GET' && urlPath === '/api/presets') {
    const { presets } = loadPresetsFromDisk();
    const list = presets.map((p) => ({
      id: p.id,
      name: p.name,
      description: p.description,
    }));
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, presets: list }));
    return;
  }

  if (req.method === 'GET' && urlPath.startsWith('/api/presets/')) {
    const id = decodeURIComponent(urlPath.slice('/api/presets/'.length));
    const { byId } = loadPresetsFromDisk();
    const preset = byId[id];
    if (!preset) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'preset not found' }));
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, preset }));
    return;
  }

  if (req.method === 'GET' && urlPath === '/api/mempool/status') {
    const status = readJsonSafe(mempoolStatusPath) || {};
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, status }));
    return;
  }

  if (req.method === 'GET' && urlPath === '/api/mempool/recent') {
    const recent = readJsonSafe(mempoolRecentPath) || [];
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, recent }));
    return;
  }

  if (req.method === 'GET' && urlPath === '/api/mempool/triggers') {
    const triggers = readJsonSafe(mempoolTriggersPath) || [];
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, triggers }));
    return;
  }

  if (req.method === 'GET' && urlPath === '/api/diagnostic') {
    const diagnostic = readJsonSafe(diagnosticPath) || {};
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, diagnostic }));
    return;
  }

  if (req.method === 'GET' && urlPath === '/api/assistant_pack') {
    const out = buildAssistantPack();
    res.writeHead(out.ok ? 200 : 500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
    return;
  }

  // Write config
  if (req.method === 'POST' && req.url === '/config') {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      try {
        const data = JSON.parse(body || '{}');
        const saved = writeConfig(data);
        broadcast({ type: 'status', time: nowTime(), block: null, text: 'Config saved' });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, config: saved }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      }
    });
    return;
  }
  // Read UI state (server-side persistence)
  if (req.method === 'GET' && req.url === '/ui_state') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, state: readUiState() }));
    return;
  }

  // Write UI state
  if (req.method === 'POST' && req.url === '/ui_state') {
    let body = '';
    let tooLarge = false;
    req.on('data', (chunk) => {
      if (tooLarge) return;
      body += chunk;
      if (body.length > UI_STATE_MAX_BYTES) {
        tooLarge = true;
        res.writeHead(413, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: 'payload too large' }));
        try { req.destroy(); } catch {}
      }
    });
    req.on('end', () => {
      if (tooLarge) return;
      try {
        const data = JSON.parse(body || '{}');
        const incoming = (data && typeof data === 'object' && data.state) ? data.state : data;
        if (!incoming || typeof incoming !== 'object') {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: false, error: 'invalid state' }));
          return;
        }

        if (typeof incoming.saved_at_ms !== 'number') {
          incoming.saved_at_ms = Date.now();
        }

        const existing = readUiState();
        const existingTs = (existing && typeof existing.saved_at_ms === 'number') ? existing.saved_at_ms : 0;
        const incomingTs = (typeof incoming.saved_at_ms === 'number') ? incoming.saved_at_ms : 0;
        if (existingTs && (!incomingTs || incomingTs < existingTs)) {
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: true, stored: false, skipped: true }));
          return;
        }

        const out = writeUiState(incoming);
        res.writeHead(out.ok ? 200 : 500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: out.ok, stored: out.ok, error: out.error || null }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      }
    });
    return;
  }
  // Serve UI
  if (req.method === 'GET' && req.url === '/') {
    const file = path.join(__dirname, 'index.html');
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(fs.readFileSync(file));
    return;
  }

  // Status
  if (req.method === 'GET' && req.url === '/status') {
    const diag = readJsonSafe(diagnosticPath) || {};
    const global = (diag && diag.global) ? diag.global : {};
    const hb = Number(global.last_heartbeat_ts || global.timestamp_ms || 0);
    const intervalS = Number(global.heartbeat_interval_s || 0);
    const thresholdMs = intervalS > 0 ? Math.max(20000, intervalS * 1500) : 30000;
    const stalled = !!(isRunning() && hb > 0 && (Date.now() - hb) > thresholdMs);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok: true,
      running: isRunning(),
      started_at_ms: botStartedAtMs,
      run_id: currentRunId,
      stalled,
      last_heartbeat_ts: hb || null,
    }));
    return;
  }

  // Start
  if (req.method === 'POST' && req.url === '/start') {
    const out = startBot();
    res.writeHead(out.ok ? 200 : 500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
    return;
  }

  // Stop
  if (req.method === 'POST' && req.url === '/stop') {
    const out = stopBot();
    res.writeHead(out.ok ? 200 : 500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
    return;
  }

  // Restart (stop, wait for exit, then start with latest config)
  if (req.method === 'POST' && req.url === '/restart') {
    (async () => {
      // Tell the UI to clear runtime stats immediately so users don't see stale RPC pools.
      broadcast({ type: 'reset', time: nowTime(), block: null, text: 'Restarting (applying config)...' });

      // If running, stop and wait for clean exit.
      if (isRunning()) {
        const stopped = await stopBotWait(12000);
        if (!stopped.ok) return stopped;
      }

      // Start fresh (will read config + env again)
      resetMempoolArtifacts();
      const started = startBot();
      return started;
    })()
      .then((out) => {
        res.writeHead(out.ok ? 200 : 500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(out));
      })
      .catch((e) => {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      });
    return;
  }

  // Bridge: Python -> UI (broadcast JSON)
  if (req.method === 'POST' && req.url === '/push') {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      try {
        const data = JSON.parse(body || '{}');
        broadcast(data);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: String(e) }));
      }
    });
    return;
  }

  res.writeHead(404);
  res.end();
});

// ---------------------------------
// WebSocket
const wss = new WebSocket.Server({ server });

wss.on('connection', (ws) => {
  console.log('Client connected');
  ws.send(
    JSON.stringify({
      type: 'status',
      time: nowTime(),
      block: null,
      text: 'connected',
      running: isRunning(),
      started_at_ms: botStartedAtMs,
    })
  );
});

function listenWithFallback(startPort, maxTries = 5) {
  let attempts = 0;
  const tryListen = (port) => {
    attempts += 1;
    server.once('error', (err) => {
      if (err && err.code === 'EADDRINUSE' && attempts < maxTries) {
        const nextPort = port + 1;
        console.warn(`Port ${port} in use, trying ${nextPort}...`);
        tryListen(nextPort);
        return;
      }
      console.error('UI server failed to start:', err);
      process.exitCode = 1;
    });
    server.listen(port, () => {
      uiPort = port;
      process.env.UI_PORT = String(port);
      console.log(`UI server running at http://localhost:${port}`);
      if (port !== startPort) {
        console.log(`Port ${startPort} unavailable, using ${port}.`);
      }
    });
  };
  tryListen(startPort);
}

listenWithFallback(uiPort);

module.exports = { broadcast };
