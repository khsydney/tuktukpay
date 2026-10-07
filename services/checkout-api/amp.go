package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

// AMP-style task mandate enforcement ("authorize a task, not hand over the account").
//
// The consumer's wallet issued the agent a scoped, time-boxed token. TukTukPay, as the
// acquirer, enforces it before risk scoring: signature, expiry, amount ceiling,
// currency, merchant allowlist, single use (replay) and the agent's Know-Your-Agent
// rating. Every decision is a span attribute + event, so "why was the agent stopped?"
// is one Tag Spotlight click (mandate.verdict / mandate.block_reason).
//
// Token format (see services/agent-console/amp.py): amp1.<b64url(payload)>.<b64url(hmac-sha256)>

type Mandate struct {
	TaskID     string   `json:"task_id"`
	AgentID    string   `json:"agent_id"`
	CustomerID string   `json:"customer_id"`
	Wallet     string   `json:"wallet"`
	Intent     string   `json:"intent"`
	Category   string   `json:"category"`
	MaxAmount  float64  `json:"max_amount"`
	Currency   string   `json:"currency"`
	Merchants  []string `json:"merchants"`
	MaxUses    int      `json:"max_uses"`
	IssuedAt   int64    `json:"issued_at"`
	ExpiresAt  int64    `json:"expires_at"`
	Nonce      string   `json:"nonce"`
	KYARating  string   `json:"kya_rating"`
}

type KYA struct {
	AgentID    string `json:"agent_id"`
	Registered bool   `json:"registered"`
	Rating     string `json:"rating"`
	Operator   string `json:"operator"`
}

type MandateResult struct {
	Verdict string // allowed | blocked
	Reason  string // mandate_<reason> when blocked
	Detail  string
	Mandate *Mandate
	KYA     *KYA
}

var errMalformed = errors.New("malformed token")

func parseMandate(token string, secret []byte) (*Mandate, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] != "amp1" {
		return nil, errMalformed
	}
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(parts[1]))
	expected := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	if !hmac.Equal([]byte(expected), []byte(parts[2])) {
		return nil, errors.New("invalid_signature")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, errMalformed
	}
	var m Mandate
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, errMalformed
	}
	return &m, nil
}

// verifyMandate runs the whole policy and records it on a child span.
func (s *Service) verifyMandate(ctx context.Context, token, agentID, paymentID, merchantID, currency string, amount float64) MandateResult {
	ctx, span := tracer.Start(ctx, "amp.verify_mandate", trace.WithAttributes(
		attribute.String("agent.protocol", "amp/1.0"),
		attribute.String("agent.id", agentID),
	))
	defer span.End()

	block := func(reason, detail string, m *Mandate, k *KYA) MandateResult {
		span.SetAttributes(attribute.String("mandate.verdict", "blocked"), attribute.String("mandate.block_reason", reason))
		span.AddEvent("mandate.blocked", trace.WithAttributes(attribute.String("mandate.block_reason", reason), attribute.String("detail", detail)))
		span.SetStatus(codes.Error, reason)
		mandateCounter.Add(ctx, 1, metric.WithAttributeSet(metricAttrs("mandate.verdict", "blocked", "mandate.block_reason", reason, "amp.kya.rating", kyaRating(k, m))))
		// createPayment logs the WARN outcome line with the full payment context; this is the check-level detail.
		s.log.DebugContext(ctx, "mandate check failed: "+reason, "event", "mandate.check_failed", "payment.id", paymentID, "agent.id", agentID,
			"mandate.block_reason", reason, "mandate.detail", detail)
		return MandateResult{Verdict: "blocked", Reason: reason, Detail: detail, Mandate: m, KYA: k}
	}

	if token == "" {
		return block("mandate_missing", "agent declared amp/1.0 but sent no X-AMP-Task-Token", nil, nil)
	}
	m, err := parseMandate(token, s.ampSecret)
	if err != nil {
		reason := "mandate_malformed"
		if err.Error() == "invalid_signature" {
			reason = "mandate_invalid_signature"
		}
		return block(reason, err.Error(), nil, nil)
	}
	span.SetAttributes(
		attribute.String("amp.task_id", m.TaskID),
		attribute.String("amp.wallet", m.Wallet),
		attribute.Float64("amp.mandate.max_amount", m.MaxAmount),
		attribute.String("amp.mandate.currency", m.Currency),
		attribute.StringSlice("amp.mandate.merchants", m.Merchants),
		attribute.Int64("amp.mandate.expires_at", m.ExpiresAt),
		attribute.String("amp.kya.rating", m.KYARating),
	)
	if m.AgentID != agentID {
		return block("mandate_agent_mismatch", fmt.Sprintf("token issued to %s, presented by %s", m.AgentID, agentID), m, nil)
	}
	if time.Now().Unix() > m.ExpiresAt {
		return block("mandate_expired", fmt.Sprintf("expired %ds ago", time.Now().Unix()-m.ExpiresAt), m, nil)
	}
	if len(m.Merchants) > 0 && !contains(m.Merchants, merchantID) {
		return block("mandate_merchant_not_allowed", fmt.Sprintf("merchant %s not in %v", merchantID, m.Merchants), m, nil)
	}
	if !strings.EqualFold(m.Currency, currency) {
		return block("mandate_currency_mismatch", fmt.Sprintf("mandate %s, payment %s", m.Currency, currency), m, nil)
	}
	if amount > m.MaxAmount+0.005 {
		return block("mandate_over_limit", fmt.Sprintf("payment %.2f exceeds mandate ceiling %.2f %s", amount, m.MaxAmount, m.Currency), m, nil)
	}

	// Know-Your-Agent: is this agent registered and trusted enough? (fail closed)
	k, err := s.lookupKYA(ctx, agentID)
	if err != nil {
		return block("mandate_kya_unavailable", err.Error(), m, nil)
	}
	span.SetAttributes(attribute.Bool("kya.registered", k.Registered), attribute.String("kya.rating", k.Rating), attribute.String("kya.operator", k.Operator))
	if !k.Registered || k.Rating == "D" || k.Rating == "none" {
		return block("mandate_kya_untrusted", fmt.Sprintf("agent %s rating %s registered=%t", agentID, k.Rating, k.Registered), m, k)
	}

	// Single use: consume the task token (Redis INCR with the mandate's lifetime).
	maxUses := m.MaxUses
	if maxUses <= 0 {
		maxUses = 1
	}
	key := "amp:uses:" + m.TaskID
	uses, err := s.rdb.Incr(ctx, key).Result()
	if err != nil {
		return block("mandate_replay_check_failed", err.Error(), m, k)
	}
	if uses == 1 {
		ttl := time.Until(time.Unix(m.ExpiresAt, 0)) + 5*time.Minute
		s.rdb.Expire(ctx, key, ttl)
	}
	if uses > int64(maxUses) {
		return block("mandate_replay", fmt.Sprintf("task token already used %d time(s)", uses-1), m, k)
	}

	span.SetAttributes(attribute.String("mandate.verdict", "allowed"), attribute.Int64("amp.mandate.use", uses))
	mandateCounter.Add(ctx, 1, metric.WithAttributeSet(metricAttrs("mandate.verdict", "allowed", "mandate.block_reason", "none", "amp.kya.rating", k.Rating)))
	s.log.InfoContext(ctx, fmt.Sprintf("mandate %s verified for agent %s (KYA %s, use %d/%d, ceiling %.2f %s)", m.TaskID, agentID, k.Rating, uses, maxUses, m.MaxAmount, m.Currency),
		"event", "mandate.verified", "payment.id", paymentID, "agent.id", agentID, "amp.task_id", m.TaskID, "amp.kya.rating", k.Rating,
		"kya.operator", k.Operator, "amp.wallet", m.Wallet, "amp.mandate.max_amount", m.MaxAmount, "amp.mandate.currency", m.Currency,
		"amp.mandate.use", uses, "merchant.id", merchantID, "payment.amount", amount, "mandate.verdict", "allowed")
	return MandateResult{Verdict: "allowed", Mandate: m, KYA: k}
}

func (s *Service) lookupKYA(ctx context.Context, agentID string) (*KYA, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.walletURL+"/v1/kya/"+agentID, nil)
	if err != nil {
		return nil, err
	}
	res, err := s.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("kya registry unreachable: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode >= 500 {
		return nil, fmt.Errorf("kya registry returned %d", res.StatusCode)
	}
	var k KYA
	if err := json.NewDecoder(res.Body).Decode(&k); err != nil {
		return nil, fmt.Errorf("kya registry response invalid: %w", err)
	}
	return &k, nil
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

func kyaRating(k *KYA, m *Mandate) string {
	if k != nil && k.Rating != "" {
		return k.Rating
	}
	if m != nil && m.KYARating != "" {
		return m.KYARating
	}
	return "unknown"
}

func metricAttrs(kv ...string) attribute.Set {
	attrs := make([]attribute.KeyValue, 0, len(kv)/2)
	for i := 0; i+1 < len(kv); i += 2 {
		attrs = append(attrs, attribute.String(kv[i], kv[i+1]))
	}
	return attribute.NewSet(attrs...)
}
