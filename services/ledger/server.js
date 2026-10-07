// TukTukPay ledger — system of record for payments and double-entry ledger
// lines (Node.js + PostgreSQL). A .NET 8 implementation of the same API lives
// in ../ledger-dotnet for teams that want to show zero-code .NET instrumentation.
//
// Instrumented with ZERO code changes via `node -r @splunk/otel/instrument`.
// The pg instrumentation records every SQL statement as a span, which is what
// Splunk APM Database Query Performance aggregates.

'use strict';

const express = require('express');
const { Pool } = require('pg');
const { trace } = require('@opentelemetry/api');
const { ChaosFlags } = require('./chaos');
const { event } = require('./log');

const chaos = new ChaosFlags(process.env.CHAOS_URL || 'http://localhost:8090', 2000, ['ledger_db_slow', 'ledger_memory_leak']);
const DATABASE_URL = process.env.DATABASE_URL || 'postgres://tuktukpay:tuktukpay@localhost:5432/tuktukpay';
const DEFAULT_POOL = Number(process.env.PG_POOL_SIZE || 10);
const POOL_WAIT_WARN_MS = Number(process.env.POOL_WAIT_WARN_MS || 100);
const SLOW_WRITE_MS = Number(process.env.SLOW_WRITE_MS || 500);
const DB_HOST = (() => { try { return new URL(DATABASE_URL).hostname; } catch (e) { return 'postgres'; } })();

// Two pools: the normal one and a deliberately tiny one used while the
// `ledger_db_slow` chaos flag is on. Swapping pools lets the workshop show
// connection-pool saturation without restarting the service.
const pools = {
  normal: new Pool({ connectionString: DATABASE_URL, max: DEFAULT_POOL, application_name: 'ledger' }),
  tiny: null,
};
function pool() {
  if (chaos.enabled('ledger_db_slow')) {
    const size = Number(chaos.param('ledger_db_slow', 'pool_size', 2));
    if (!pools.tiny || pools.tiny.options.max !== size) {
      if (pools.tiny) pools.tiny.end().catch(() => {});
      pools.tiny = new Pool({ connectionString: DATABASE_URL, max: size, application_name: 'ledger-degraded' });
      event('warn', 'db.pool.reconfigured', `ledger connection pool to ${DB_HOST} reconfigured: max ${size} connections (was ${DEFAULT_POOL})`,
        { 'db.system': 'postgresql', 'db.pool.max': size, 'db.pool.previous_max': DEFAULT_POOL, 'peer.service': 'postgres' });
    }
    return pools.tiny;
  }
  return pools.normal;
}

// --- chaos: memory leak (Act 6, Kubernetes) ----------------------------------------
// Retains `mb_per_second` of buffers every second while the flag is on — a cache that
// forgot to evict after a release. Under a Kubernetes memory limit the kernel OOM-kills
// the container and the pod restarts (the AutoDetect "container restart count > 0"
// alert); on Docker Compose, with no limit, the leak stops at max_mb. The service logs
// its own memory pressure before it dies — the line a root-cause analysis should find.
const fs = require('fs');
const leaked = [];
let leakedMb = 0;
let lastPressureLog = 0;
function cgroupMemoryLimitMb() {
  for (const p of ['/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes']) {
    try {
      const v = fs.readFileSync(p, 'utf8').trim();
      if (v && v !== 'max') { const n = Number(v); if (n > 0 && n < 1e15) return Math.round(n / 1048576); }
    } catch (e) { /* not in a cgroup */ }
  }
  return 0;
}
setInterval(() => {
  if (!chaos.enabled('ledger_memory_leak')) {
    if (leaked.length) { leaked.length = 0; leakedMb = 0; event('info', 'memory.released', 'ledger released its retained buffers (leak flag off)', { 'memory.leaked_mb': 0 }); }
    return;
  }
  const perSec = Number(chaos.param('ledger_memory_leak', 'mb_per_second', 6));
  const maxMb = Number(chaos.param('ledger_memory_leak', 'max_mb', 1536));
  if (leakedMb < maxMb) {
    const buf = Buffer.alloc(perSec * 1048576, 1); // touched, so the kernel really charges it
    leaked.push(buf);
    leakedMb += perSec;
  }
  const now = Date.now();
  if (now - lastPressureLog > 5000) {
    lastPressureLog = now;
    const mem = process.memoryUsage();
    const rssMb = Math.round(mem.rss / 1048576);
    const limitMb = cgroupMemoryLimitMb();
    const pct = limitMb ? Math.round((rssMb / limitMb) * 100) : 0;
    event(limitMb && pct >= 80 ? 'error' : 'warn', 'memory.pressure',
      `ledger memory ${rssMb} MiB RSS${limitMb ? ` of ${limitMb} MiB container limit (${pct}%)` : ''}: settlement cache holds ${leakedMb} MiB and is not evicting`,
      { 'process.memory.rss_mb': rssMb, 'process.memory.heap_used_mb': Math.round(mem.heapUsed / 1048576), 'process.memory.external_mb': Math.round(mem.external / 1048576),
        'container.memory.limit_mb': limitMb, 'container.memory.utilization_pct': pct, 'memory.leaked_mb': leakedMb, 'error.type': limitMb && pct >= 80 ? 'memory_limit_near' : 'memory_growth' });
  }
}, 1000).unref();

// Pool / query timing fields shared by the ledger log events.
function dbFields(db) {
  return { 'db.system': 'postgresql', 'peer.service': 'postgres', 'db.pool.max': db.options.max, 'db.pool.total': db.totalCount, 'db.pool.idle': db.idleCount, 'db.pool.waiting': db.waitingCount };
}

const app = express();
app.use(express.json({ limit: '256kb' }));

app.get('/healthz', async (req, res) => {
  try {
    await pools.normal.query('SELECT 1');
    res.json({ status: 'ok' });
  } catch (e) {
    res.status(503).json({ status: 'db_unavailable', error: e.message });
  }
});

// Record a payment and its ledger lines (idempotent on payment_id).
app.post('/v1/entries', async (req, res) => {
  const p = req.body || {};
  if (!p.payment_id || !p.merchant_id) return res.status(400).json({ error: 'payment_id and merchant_id are required' });
  const span = trace.getActiveSpan();
  if (span) span.setAttributes({ 'payment.id': p.payment_id, 'merchant.id': p.merchant_id, 'payment.outcome': p.status || 'unknown' });

  const db = pool();
  const t0 = Date.now();
  const paymentFields = { 'payment.id': p.payment_id, 'merchant.id': p.merchant_id, 'payment.outcome': p.status || 'unknown', 'payment.amount': Number(p.amount || 0), 'payment.currency': p.currency || '' };
  let client;
  try {
    client = await db.connect();
  } catch (e) {
    event('error', 'db.connect_failed', `ledger could not get a database connection from ${DB_HOST} for payment ${p.payment_id}: ${e.message}`,
      { ...paymentFields, ...dbFields(db), 'duration_ms': Date.now() - t0, 'error.type': e.code || e.name, 'error.message': e.message });
    return res.status(503).json({ error: 'database unavailable' });
  }
  const waitMs = Date.now() - t0;
  if (span) span.setAttributes({ 'db.pool.wait_ms': waitMs, 'db.pool.max': db.options.max, 'db.pool.waiting': db.waitingCount });
  if (waitMs > POOL_WAIT_WARN_MS) {
    // Act 5: the pool is too small for the write rate — requests queue for a connection.
    event('warn', 'db.pool.wait', `payment ${p.payment_id} waited ${waitMs} ms for a database connection (pool max ${db.options.max}, ${db.waitingCount} requests still waiting)`,
      { ...paymentFields, ...dbFields(db), 'db.pool.wait_ms': waitMs });
  }
  try {
    await client.query('BEGIN');
    if (chaos.enabled('ledger_db_slow')) {
      // Simulates lock contention / a slow storage volume on the write path.
      const sleepMs = Number(chaos.param('ledger_db_slow', 'sleep_ms', 250));
      await client.query('SELECT pg_sleep($1)', [sleepMs / 1000]);
    }
    await client.query(
      `INSERT INTO payments (payment_id, merchant_id, order_id, amount, currency, payment_method, status, acquirer, auth_code,
                             decline_reason, initiator, card_bin, card_network, customer_country, risk_score, risk_model_version)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
       ON CONFLICT (payment_id) DO UPDATE SET status = EXCLUDED.status, acquirer = EXCLUDED.acquirer,
         auth_code = EXCLUDED.auth_code, decline_reason = EXCLUDED.decline_reason, updated_at = now()`,
      [p.payment_id, p.merchant_id, p.order_id || null, p.amount || 0, p.currency || null, p.payment_method || null,
        p.status || 'unknown', p.acquirer || null, p.auth_code || null, p.decline_reason || null, p.initiator || 'human',
        p.card_bin || null, p.card_network || null, p.customer_country || null, p.risk_score ?? null, p.risk_model_version || null],
    );
    if (p.status === 'approved') {
      // Double entry: merchant receivable (credit) vs acquirer settlement (debit).
      await client.query(
        `INSERT INTO ledger_entries (payment_id, account, direction, amount, currency)
         VALUES ($1, $2, 'credit', $3, $4), ($1, $5, 'debit', $3, $4)`,
        [p.payment_id, `merchant:${p.merchant_id}:receivable`, p.amount || 0, p.currency || null, `acquirer:${p.acquirer || 'unknown'}:settlement`],
      );
    }
    await client.query('COMMIT');
    const totalMs = Date.now() - t0;
    if (totalMs > SLOW_WRITE_MS) {
      event('warn', 'ledger.write_slow', `ledger write for payment ${p.payment_id} took ${totalMs} ms (${waitMs} ms waiting for a connection, ${totalMs - waitMs} ms in the transaction)`,
        { ...paymentFields, ...dbFields(db), 'db.pool.wait_ms': waitMs, 'duration_ms': totalMs, 'db.operation': 'INSERT', 'db.collection.name': 'payments' });
    } else {
      event('debug', 'ledger.entry_recorded', `recorded payment ${p.payment_id} (${p.status}) in ${totalMs} ms`,
        { ...paymentFields, ...dbFields(db), 'db.pool.wait_ms': waitMs, 'duration_ms': totalMs });
    }
    res.status(201).json({ ok: true, payment_id: p.payment_id, pool_wait_ms: waitMs });
  } catch (e) {
    await client.query('ROLLBACK').catch(() => {});
    event('error', 'ledger.write_failed', `ledger write failed for payment ${p.payment_id}: ${e.code ? `${e.code} ` : ''}${e.message}`,
      { ...paymentFields, ...dbFields(db), 'db.pool.wait_ms': waitMs, 'duration_ms': Date.now() - t0, 'db.operation': 'INSERT', 'error.type': e.code || e.name, 'error.message': e.message });
    res.status(500).json({ error: e.message });
  } finally {
    client.release();
  }
});

app.get('/v1/payments/:id', async (req, res) => {
  try {
    const { rows } = await pool().query('SELECT * FROM payments WHERE payment_id = $1', [req.params.id]);
    if (!rows.length) return res.status(404).json({ error: 'payment not found' });
    res.json(rows[0]);
  } catch (e) {
    event('error', 'ledger.query_failed', `payment lookup failed for ${req.params.id}: ${e.message}`, { 'payment.id': req.params.id, 'db.system': 'postgresql', 'peer.service': 'postgres', 'error.type': e.code || e.name, 'error.message': e.message });
    res.status(500).json({ error: e.message });
  }
});

// Used by the merchant copilot: recent payments for a merchant, optionally by status.
app.get('/v1/payments', async (req, res) => {
  const { merchant_id, status } = req.query;
  const limit = Math.min(Number(req.query.limit || 20), 200);
  if (!merchant_id) return res.status(400).json({ error: 'merchant_id is required' });
  try {
    const params = [merchant_id, limit];
    let sql = 'SELECT payment_id, order_id, amount, currency, payment_method, status, acquirer, decline_reason, initiator, card_bin, risk_score, risk_model_version, created_at FROM payments WHERE merchant_id = $1';
    if (status) { params.push(status); sql += ` AND status = $${params.length}`; }
    sql += ' ORDER BY created_at DESC LIMIT $2';
    const { rows } = await pool().query(sql, params);
    res.json({ merchant_id, count: rows.length, payments: rows });
  } catch (e) {
    event('error', 'ledger.query_failed', `recent payments query failed for ${merchant_id}: ${e.message}`, { 'merchant.id': merchant_id, 'db.system': 'postgresql', 'peer.service': 'postgres', 'error.type': e.code || e.name, 'error.message': e.message });
    res.status(500).json({ error: e.message });
  }
});

app.get('/v1/merchants/:id/summary', async (req, res) => {
  const hours = Math.min(Number(req.query.hours || 1), 168);
  try {
    const { rows } = await pool().query(
      `SELECT status, decline_reason, acquirer, count(*)::int AS count, sum(amount)::float AS amount
         FROM payments
        WHERE merchant_id = $1 AND created_at > now() - ($2 || ' hours')::interval
        GROUP BY status, decline_reason, acquirer
        ORDER BY count DESC`,
      [req.params.id, String(hours)],
    );
    const total = rows.reduce((s, r) => s + r.count, 0);
    const approved = rows.filter((r) => r.status === 'approved').reduce((s, r) => s + r.count, 0);
    res.json({ merchant_id: req.params.id, window_hours: hours, total, approved, approval_rate: total ? approved / total : null, breakdown: rows });
  } catch (e) {
    event('error', 'ledger.query_failed', `merchant summary query failed for ${req.params.id}: ${e.message}`, { 'merchant.id': req.params.id, 'db.system': 'postgresql', 'peer.service': 'postgres', 'error.type': e.code || e.name, 'error.message': e.message });
    res.status(500).json({ error: e.message });
  }
});

const port = Number(process.env.PORT || 8084);
app.listen(port, () => event('info', 'service.started', `ledger listening on :${port} (postgres ${DB_HOST}, pool max ${DEFAULT_POOL})`, { 'server.port': port, 'db.system': 'postgresql', 'db.pool.max': DEFAULT_POOL, 'peer.service': 'postgres' }));
