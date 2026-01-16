const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

// ---------------------------------
// Bot process manager (fork_test.py)
let botProc = null;

// Runtime UI config (stored in project root)
const projectRoot = path.join(__dirname, '..');
const configPath = path.join(projectRoot, 'bot_config.json');

const DEFAULT_CONFIG = {
  // thresholds
  min_profit_pct: 0.05,
  min_profit_abs: 0.05,
  slippage_bps: 8,
  max_gas_gwei: null,

  // performance
  concurrency: 12,
  block_budget_s: 10,

  // modes
  scan_mode: 'auto', // auto|fixed

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
  rpc_timeout_stage1_s: 6,
  rpc_timeout_stage2_s: 10,

  verbose: false,
};

function readConfig() {
  try {
    if (!fs.existsSync(configPath)) return { ...DEFAULT_CONFIG };
    const raw = fs.readFileSync(configPath, 'utf8') || '{}';
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_CONFIG, ...parsed };
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

  const safe = { ...DEFAULT_CONFIG, ...cleaned };
  fs.writeFileSync(configPath, JSON.stringify(safe, null, 2));
  return safe;
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

function startBot() {
  if (isRunning()) return { ok: true, alreadyRunning: true };

  const script = path.join(projectRoot, 'fork_test.py');
  const python = process.env.PYTHON || 'python3';

  // Ensure Python dependencies exist (so we don't crash on ModuleNotFoundError: web3)
  // This keeps the project "works out of the box" when the bot is launched from the UI.
  const reqFile = path.join(projectRoot, 'requirements.txt');
  try {
    const check = spawnSync(python, ['-c', 'import web3'], { cwd: projectRoot });
    if (check.status !== 0) {
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
        return { ok: false, error: 'pip install -r requirements.txt failed' };
      }
    }
  } catch (e) {
    broadcast({ type: 'stderr', time: nowTime(), block: null, text: `Dependency check failed: ${String(e)}` });
  }

  const cfg = readConfig();

  botProc = spawn(python, ['-u', script], {
    cwd: projectRoot,
    // Keep both env vars for backwards compatibility.
    env: { ...process.env, BOT_CONFIG: configPath, ARBITOINATOR_CONFIG: configPath },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  broadcast({ type: 'status', time: nowTime(), block: null, text: 'Bot started' });

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
    broadcast({
      type: 'status',
      time: nowTime(),
      block: null,
      text: `Bot stopped (code=${code}, signal=${signal || 'none'})`,
    });
    botProc = null;
  });

  return { ok: true };
}

function stopBot() {
  if (!isRunning()) return { ok: true, alreadyStopped: true };
  try {
    botProc.kill('SIGINT');
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
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
  // Serve UI
  if (req.method === 'GET' && req.url === '/') {
    const file = path.join(__dirname, 'index.html');
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(fs.readFileSync(file));
    return;
  }

  // Status
  if (req.method === 'GET' && req.url === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, running: isRunning() }));
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
    })
  );
});

server.listen(8080, () => console.log('UI server running at http://localhost:8080'));

module.exports = { broadcast };
