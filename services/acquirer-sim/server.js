// TukTukPay acquirer-sim — stands in for the banks / card acquirers / wallet
// rails a PSP connects to. One process simulates several acquirers, each with
// its own latency profile and approval behaviour, plus workshop chaos hooks.
//
// Instrumented with ZERO code changes: the Splunk Distribution of OpenTelemetry
// JS is loaded with `node -r @splunk/otel/instrument server.js` (see Dockerfile).
// The only OpenTelemetry API usage below adds business attributes to the span
// the auto-instrumentation already created.

'use strict';

const express = require('express');
const crypto = require('crypto');
const { trace } = require('@opentelemetry/api');
const { ChaosFlags } = require('./chaos');
const { event } = require('./log');

const chaos = new ChaosFlags(process.env.CHAOS_URL || 'http://localhost:8090', 2000, ['acquirer_kbank_timeout']);
const app = express();
app.use(express.json({ limit: '256kb' }));

// name -> { latency: [meanMs, jitterMs], approval: base approval rate }
const ACQUIRERS = {
  'acq-uob': { latency: [90, 35], approval: 0.93, description: 'UOB regional card acquiring, booked in Singapore (SG/MY/USD, Amex) — cross-border for Thai cards' },
  'acq-kbank': { latency: [140, 50], approval: 0.95, description: 'Kasikornbank (KBank) merchant acquiring — best domestic THB approval' },
  'acq-cimb': { latency: [220, 90], approval: 0.9, description: 'CIMB regional card acquiring (ID/PH/VN)' },
  'acq-alipayplus': { latency: [350, 120], approval: 0.96, description: 'Alipay+ e-wallet aggregator (TrueMoney, Rabbit LINE Pay, ShopeePay, GrabPay, DANA, GCash, TNG, MoMo)' },
  'acq-itmx': { latency: [520, 220], approval: 0.97, description: 'ITMX real-time rails: PromptPay + linked cross-border QR (PayNow, DuitNow, QRIS, VietQR)' },
};

// ISO 8583-style response codes with the merchant-facing reason.
const DECLINES = [
  { code: '51', reason: 'insufficient_funds', weight: 45 },
  { code: '05', reason: 'do_not_honor', weight: 25 },
  { code: '14', reason: 'invalid_card_number', weight: 8 },
  { code: '54', reason: 'expired_card', weight: 8 },
  { code: '59', reason: 'suspected_fraud', weight: 6 },
  { code: '65', reason: 'exceeds_withdrawal_frequency', weight: 3 },
  { code: '91', reason: 'issuer_unavailable', weight: 5 }, // retryable -> router fails over
];
const DECLINE_TOTAL = DECLINES.reduce((s, d) => s + d.weight, 0);

// Deterministic pseudo-random in [0,1) from the payment id + salt so a payment
// behaves the same way on every retry (and demos are reproducible).
function roll(paymentId, salt) {
  const h = crypto.createHash('sha256').update(`${salt}:${paymentId}`).digest();
  return h.readUInt32BE(0) / 2 ** 32;
}

function gaussianish(mean, jitter) {
  const u = Math.random() + Math.random() + Math.random() - 1.5; // ~normal, [-1.5,1.5]
  return Math.max(15, Math.round(mean + u * jitter));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function pickDecline(paymentId) {
  let x = roll(paymentId, 'decline') * DECLINE_TOTAL;
  for (const d of DECLINES) {
    if (x < d.weight) return d;
    x -= d.weight;
  }
  return DECLINES[0];
}

app.get('/healthz', (req, res) => res.json({ status: 'ok', acquirers: Object.keys(ACQUIRERS) }));

app.get('/acquirers', (req, res) => res.json(ACQUIRERS));

app.post('/acquirers/:name/authorize', async (req, res) => {
  const name = req.params.name;
  const acq = ACQUIRERS[name];
  const span = trace.getActiveSpan();
  if (!acq) {
    event('warn', 'acquirer.unknown', `authorisation request for unknown acquirer ${name}`, { 'acquirer.name': name, 'acquirer.response_code': '96' });
    return res.status(404).json({ status: 'error', response_code: '96', decline_reason: 'unknown_acquirer' });
  }
  const p = req.body || {};
  const paymentId = p.payment_id || crypto.randomUUID();
  const bin = String(p.card_bin || '');
  const currency = String(p.currency || '');
  const method = String(p.payment_method || 'card');
  const amount = Number(p.amount || 0);
  const started = Date.now();
  // Shared log fields for this authorisation (same names as the span attributes).
  const fields = {
    'acquirer.name': name, 'payment.id': paymentId, 'merchant.id': p.merchant_id || 'unknown', 'payment.currency': currency,
    'payment.method': method, 'payment.card.bin': bin, 'payment.card.network': String(p.card_network || ''), 'payment.amount': amount,
    'payment.initiator': String(p.initiator || 'human'),
  };

  if (span) {
    span.setAttributes({
      'acquirer.name': name,
      'payment.id': paymentId,
      'merchant.id': p.merchant_id || 'unknown',
      'payment.currency': currency,
      'payment.method': method,
      'payment.card.bin': bin,
    });
  }

  // --- chaos: acquirer KBank hangs for one BIN range ------------------------
  if (name === 'acq-kbank' && chaos.enabled('acquirer_kbank_timeout')) {
    const prefix = String(chaos.param('acquirer_kbank_timeout', 'bin_prefix', '457173'));
    const ccy = String(chaos.param('acquirer_kbank_timeout', 'currency', 'THB'));
    if (bin.startsWith(prefix) && (!ccy || currency === ccy)) {
      const hang = Number(chaos.param('acquirer_kbank_timeout', 'hang_ms', 3200));
      if (span) span.addEvent('acquirer.host_unresponsive', { 'acquirer.hang_ms': hang, 'payment.card.bin': bin });
      // The Act 1 smoking gun, from the acquirer side: one BIN range on one host is stuck.
      event('warn', 'acquirer.host_unresponsive',
        `${name} authorisation host not responding for BIN ${bin} ${currency}: payment ${paymentId} held ${hang} ms (issuer link degraded)`,
        { ...fields, 'acquirer.hang_ms': hang, 'acquirer.host': `${name}-auth-host-02`, 'error.type': 'issuer_link_degraded' });
      await sleep(hang); // the router gives up at 2.5s; we answer late anyway
      event('warn', 'acquirer.late_response',
        `${name} answered payment ${paymentId} after ${Date.now() - started} ms — the router has already timed out and failed over`,
        { ...fields, 'acquirer.response_code': '00', 'duration_ms': Date.now() - started, 'acquirer.late': true });
      return res.json({ status: 'approved', response_code: '00', auth_code: 'LATE01', network_txn_id: crypto.randomUUID(), late: true });
    }
  }

  await sleep(gaussianish(acq.latency[0], acq.latency[1]));

  // --- approval model ----------------------------------------------------------
  let approval = acq.approval;
  // Cross-border acquiring of domestic Thai cards through UOB's Singapore-booked entity
  // performs badly — the reason the router prefers acq-kbank for THB.
  if (name === 'acq-uob' && currency === 'THB') approval = 0.55;
  if (method === 'card' && amount > 20000) approval -= 0.05;

  const approved = roll(paymentId, `auth:${name}`) < approval;
  const crossBorder = name === 'acq-uob' && currency === 'THB';
  if (approved) {
    const authCode = crypto.randomBytes(3).toString('hex').toUpperCase();
    if (span) span.setAttributes({ 'acquirer.response_code': '00', 'acquirer.outcome': 'approved' });
    event('info', 'acquirer.authorised', `${name} approved payment ${paymentId} for ${fields['merchant.id']}: ${amount} ${currency} ${method} (auth ${authCode}, ${Date.now() - started} ms)`,
      { ...fields, 'acquirer.response_code': '00', 'acquirer.outcome': 'approved', 'duration_ms': Date.now() - started, 'acquirer.cross_border': crossBorder });
    return res.json({ status: 'approved', response_code: '00', auth_code: authCode, network_txn_id: crypto.randomUUID() });
  }
  let d = pickDecline(paymentId);
  // Foreign acquirer + domestic Thai card: issuers mostly answer "do not honor".
  if (crossBorder && roll(paymentId, 'xborder') < 0.8) d = DECLINES[1];
  if (span) span.setAttributes({ 'acquirer.response_code': d.code, 'acquirer.outcome': 'declined', 'acquirer.decline_reason': d.reason });
  event('info', 'acquirer.declined',
    `${name} declined payment ${paymentId} for ${fields['merchant.id']}: issuer response ${d.code} ${d.reason}${crossBorder ? ' (cross-border acquiring of a domestic THB card)' : ''}`,
    { ...fields, 'acquirer.response_code': d.code, 'acquirer.outcome': 'declined', 'acquirer.decline_reason': d.reason, 'duration_ms': Date.now() - started, 'acquirer.cross_border': crossBorder });
  return res.json({ status: 'declined', response_code: d.code, decline_reason: d.reason, auth_code: '' });
});

const port = Number(process.env.PORT || 8083);
app.listen(port, () => event('info', 'service.started', `acquirer-sim listening on :${port} (${Object.keys(ACQUIRERS).length} acquirers)`, { 'server.port': port, 'acquirer.names': Object.keys(ACQUIRERS) }));
