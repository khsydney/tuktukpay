'use strict';
// Polls the workshop chaos-controller every 2s and caches the flags in memory.
// Uses plain http.get; the collector drops these polling spans (see otel/collector-config.yaml).
const http = require('http');

class ChaosFlags {
  constructor(baseUrl, intervalMs = 2000) {
    this.url = `${baseUrl.replace(/\/$/, '')}/flags`;
    this.flags = {};
    this.lastError = null;
    const tick = () => {
      const req = http.get(this.url, { timeout: 1500 }, (res) => {
        let body = '';
        res.on('data', (c) => (body += c));
        res.on('end', () => {
          try {
            this.flags = JSON.parse(body);
            if (this.lastError) { console.log(JSON.stringify({ level: 'info', msg: 'chaos-controller reachable again' })); this.lastError = null; }
          } catch (e) { /* ignore partial body */ }
        });
      });
      req.on('timeout', () => req.destroy(new Error('timeout')));
      req.on('error', (e) => {
        if (this.lastError !== e.message) { console.log(JSON.stringify({ level: 'warn', msg: 'chaos-controller unreachable', error: e.message })); this.lastError = e.message; }
      });
    };
    tick();
    this.timer = setInterval(tick, intervalMs);
    this.timer.unref();
  }

  enabled(name) { return Boolean(this.flags[name] && this.flags[name].enabled); }

  param(name, key, def) {
    const f = this.flags[name];
    if (!f || !f.params || f.params[key] === undefined || f.params[key] === null) return def;
    return f.params[key];
  }
}

module.exports = { ChaosFlags };
