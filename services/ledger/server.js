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

const chaos = new ChaosFlags(process.env.CHAOS_URL || 'http://localhost:8090');
const DATABASE_URL = process.env.DATABASE_URL || 'postgres://tuktukpay:tuktukpay@localhost:5432/tuktukpay';
const DEFAULT_POOL = Number(process.env.PG_POOL_SIZE || 10);

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
    }
    return pools.tiny;
  }
  return pools.normal;
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
  const client = await db.connect();
  const waitMs = Date.now() - t0;
  if (span) span.setAttributes({ 'db.pool.wait_ms': waitMs, 'db.pool.max': db.options.max, 'db.pool.waiting': db.waitingCount });
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
    res.status(201).json({ ok: true, payment_id: p.payment_id, pool_wait_ms: waitMs });
  } catch (e) {
    await client.query('ROLLBACK').catch(() => {});
    console.error(JSON.stringify({ level: 'error', msg: 'ledger write failed', payment_id: p.payment_id, error: e.message }));
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
    res.status(500).json({ error: e.message });
  }
});

const port = Number(process.env.PORT || 8084);
app.listen(port, () => console.log(JSON.stringify({ level: 'info', msg: 'ledger listening', port, pool: DEFAULT_POOL })));
