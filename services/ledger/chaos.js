'use strict';
// Polls the workshop chaos-controller every 2s and caches the flags in memory.
// Uses plain http.get; the collector drops these polling spans (see otel/collector-config.yaml).
//
// Transitions of the flags this service consumes (`watch`) are logged as
// `config.changed` events — the "feature flag flipped at 10:02" breadcrumb a real
// platform would have in its logs, and the one an AI root-cause analysis latches onto.
const http = require('http');
const { event } = require('./log');

class ChaosFlags {
  constructor(baseUrl, intervalMs = 2000, watch = []) {
    this.url = `${baseUrl.replace(/\/$/, '')}/flags`;
    this.flags = {};
    this.watch = new Set(watch);
    this.known = {};
    this.lastError = null;
    const tick = () => {
      const req = http.get(this.url, { timeout: 1500 }, (res) => {
        let body = '';
        res.on('data', (c) => (body += c));
        res.on('end', () => {
          try {
            this.flags = JSON.parse(body);
            this.logTransitions();
            if (this.lastError) { event('debug', 'chaos.reachable', 'chaos-controller reachable again'); this.lastError = null; }
          } catch (e) { /* ignore partial body */ }
        });
      });
      req.on('timeout', () => req.destroy(new Error('timeout')));
      req.on('error', (e) => {
        if (this.lastError !== e.message) { event('debug', 'chaos.unreachable', `chaos-controller unreachable (${e.message}); keeping last flags`); this.lastError = e.message; }
      });
    };
    tick();
    this.timer = setInterval(tick, intervalMs);
    this.timer.unref();
  }

  logTransitions() {
    for (const name of this.watch) {
      const flag = this.flags[name] || {};
      const enabled = Boolean(flag.enabled);
      const params = JSON.stringify(flag.params || {}, Object.keys(flag.params || {}).sort());
      const current = `${enabled}|${params}`;
      const previous = this.known[name];
      this.known[name] = current;
      if (previous === undefined || previous === current) continue;
      const paramText = Object.entries(flag.params || {}).map(([k, v]) => `${k}=${v}`).join(' ');
      event('info', 'config.changed', `feature flag ${name} ${enabled ? 'enabled' : 'disabled'}${enabled && paramText ? `: ${paramText}` : ''}`,
        { 'feature_flag.name': name, 'feature_flag.enabled': enabled, 'feature_flag.params': params });
    }
  }

  enabled(name) { return Boolean(this.flags[name] && this.flags[name].enabled); }

  param(name, key, def) {
    const f = this.flags[name];
    if (!f || !f.params || f.params[key] === undefined || f.params[key] === null) return def;
    return f.params[key];
  }
}

module.exports = { ChaosFlags };
