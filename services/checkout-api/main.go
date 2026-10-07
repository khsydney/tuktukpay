// TukTukPay checkout-api — the public payments API of a fictional SEA payment
// service provider. Written in Go with the upstream OpenTelemetry SDK so the
// audience can see exactly which lines create spans, attributes and metrics.
//
// Flow for POST /v1/payments:
//  1. validate + classify the request (human vs AI-agent initiated)
//  2. risk-engine   -> fraud score / decision            (Python)
//  3. payment-router -> pick acquirer, authorize, failover (Java)
//  4. ledger        -> persist the payment + entries      (Node or .NET)
//  5. Redis stream  -> async webhook to the merchant      (Python worker)
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/redis/go-redis/extra/redisotel/v9"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

const serviceName = "checkout-api"

var (
	tracer = otel.Tracer(serviceName)
	meter  = otel.Meter(serviceName)

	paymentsCounter metric.Int64Counter
	paymentsAmount  metric.Float64Histogram
	declinesCounter metric.Int64Counter
	mandateCounter  metric.Int64Counter
)

// ---------- request / response contracts ----------

type Card struct {
	BIN     string `json:"bin"`
	Last4   string `json:"last4"`
	Network string `json:"network"`
}

type Wallet struct {
	Provider string `json:"provider"`
}

type Customer struct {
	ID      string `json:"id"`
	Country string `json:"country"`
}

type PaymentRequest struct {
	MerchantID    string            `json:"merchant_id"`
	Amount        float64           `json:"amount"`
	Currency      string            `json:"currency"`
	PaymentMethod string            `json:"payment_method"` // card | wallet | bank_transfer | qr
	Card          *Card             `json:"card,omitempty"`
	Wallet        *Wallet           `json:"wallet,omitempty"`
	Customer      Customer          `json:"customer"`
	OrderID       string            `json:"order_id"`
	Channel       string            `json:"channel"` // web | app | api
	Metadata      map[string]string `json:"metadata,omitempty"`
}

type RiskResponse struct {
	Score        float64  `json:"score"`
	Decision     string   `json:"decision"` // allow | review | deny
	ModelVersion string   `json:"model_version"`
	Reasons      []string `json:"reasons"`
	LatencyMs    float64  `json:"latency_ms"`
}

type RouteAttempt struct {
	Acquirer     string `json:"acquirer"`
	Outcome      string `json:"outcome"`
	ResponseCode string `json:"response_code"`
	LatencyMs    int64  `json:"latency_ms"`
}

type RouteResponse struct {
	Acquirer      string         `json:"acquirer"`
	Status        string         `json:"status"` // approved | declined | error
	AuthCode      string         `json:"auth_code"`
	DeclineReason string         `json:"decline_reason"`
	Failover      bool           `json:"failover"`
	Attempts      []RouteAttempt `json:"attempts"`
}

type PaymentResponse struct {
	PaymentID     string        `json:"payment_id"`
	Status        string        `json:"status"`
	Acquirer      string        `json:"acquirer,omitempty"`
	AuthCode      string        `json:"auth_code,omitempty"`
	DeclineReason string        `json:"decline_reason,omitempty"`
	Risk          *RiskResponse `json:"risk,omitempty"`
	Initiator     string        `json:"initiator"`
	MandateDetail string        `json:"mandate_detail,omitempty"`
	LatencyMs     int64         `json:"latency_ms"`
}

// ---------- service ----------

type Service struct {
	http      *http.Client
	riskURL   string
	routerURL string
	ledgerURL string
	walletURL string
	ampSecret []byte
	rdb       *redis.Client
	stream    string
	log       *slog.Logger
}

func newService(log *slog.Logger) (*Service, error) {
	rdb := redis.NewClient(&redis.Options{Addr: getenv("REDIS_ADDR", "localhost:6379")})
	if err := redisotel.InstrumentTracing(rdb); err != nil {
		return nil, err
	}
	return &Service{
		// otelhttp.NewTransport injects W3C traceparent headers into every
		// outbound call and records a CLIENT span — this is the hop that stitches
		// Go -> Python -> Java -> Node into one distributed trace.
		http: &http.Client{
			Transport: otelhttp.NewTransport(http.DefaultTransport,
				otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
					path := r.URL.Path
					if strings.HasPrefix(path, "/v1/payments/") {
						path = "/v1/payments/{id}" // keep span names low-cardinality
					}
					return r.Method + " " + r.URL.Host + path
				})),
			Timeout: 8 * time.Second,
		},
		riskURL:   getenv("RISK_URL", "http://localhost:8081"),
		routerURL: getenv("ROUTER_URL", "http://localhost:8082"),
		ledgerURL: getenv("LEDGER_URL", "http://localhost:8084"),
		walletURL: getenv("WALLET_URL", "http://localhost:8088"),
		ampSecret: []byte(getenv("AMP_SHARED_SECRET", "tuktukpay-amp-demo-secret")),
		rdb:       rdb,
		stream:    getenv("EVENT_STREAM", "payments.events"),
		log:       log,
	}, nil
}

func (s *Service) createPayment(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ctx := r.Context()
	span := trace.SpanFromContext(ctx) // SERVER span created by otelhttp.NewHandler

	var req PaymentRequest
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err := json.Unmarshal(body, &req); err != nil || req.MerchantID == "" || req.Amount <= 0 || req.Currency == "" {
		span.SetStatus(codes.Error, "invalid payment request")
		s.log.LogAttrs(ctx, slog.LevelWarn, "rejected invalid payment request",
			slog.String("event", "payment.rejected"), slog.Int("http.response.status_code", http.StatusBadRequest),
			slog.String("merchant.id", req.MerchantID), slog.Int("http.request.body.size", len(body)))
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid payment request"})
		return
	}

	paymentID := "pay_" + randomID(12)
	initiator, agentProto, agentID := classifyInitiator(r)

	// Business context as span attributes: this is what Tag Spotlight slices on.
	attrs := []attribute.KeyValue{
		attribute.String("payment.id", paymentID),
		attribute.String("merchant.id", req.MerchantID),
		attribute.Float64("payment.amount", req.Amount),
		attribute.String("payment.currency", req.Currency),
		attribute.String("payment.method", req.PaymentMethod),
		attribute.String("payment.channel", req.Channel),
		attribute.String("payment.initiator", initiator),
		attribute.String("customer.country", req.Customer.Country),
		attribute.String("order.id", req.OrderID),
	}
	if req.Card != nil {
		attrs = append(attrs,
			attribute.String("payment.card.bin", req.Card.BIN),
			attribute.String("payment.card.network", req.Card.Network),
		)
	}
	if req.Wallet != nil {
		attrs = append(attrs, attribute.String("payment.wallet.provider", req.Wallet.Provider))
	}
	if initiator == "agent" {
		attrs = append(attrs,
			attribute.String("agent.protocol", agentProto),
			attribute.String("agent.id", agentID),
		)
	}
	span.SetAttributes(attrs...)
	if key := r.Header.Get("Idempotency-Key"); key != "" {
		span.SetAttributes(attribute.String("payment.idempotency_key", key))
	}

	resp := PaymentResponse{PaymentID: paymentID, Initiator: initiator}

	// One structured log line per payment outcome (docs/log-schema.md), carrying the
	// same business keys as the span so Log Observer and Tag Spotlight agree.
	logOutcome := func(level slog.Level, event, msg string, status int, extra ...slog.Attr) {
		fields := append(kvToSlog(attrs),
			slog.String("event", event), slog.String("payment.outcome", resp.Status),
			slog.Int("http.response.status_code", status), slog.Int64("duration_ms", time.Since(start).Milliseconds()))
		if resp.DeclineReason != "" {
			fields = append(fields, slog.String("payment.decline_reason", resp.DeclineReason))
		}
		if resp.Acquirer != "" {
			fields = append(fields, slog.String("payment.acquirer", resp.Acquirer))
		}
		if resp.Risk != nil {
			fields = append(fields, slog.Float64("risk.score", resp.Risk.Score), slog.String("risk.decision", resp.Risk.Decision),
				slog.String("risk.model_version", resp.Risk.ModelVersion), slog.Any("risk.reasons", resp.Risk.Reasons))
		}
		s.log.LogAttrs(ctx, level, msg, append(fields, extra...)...)
	}

	// 0. Agentic governance: enforce the consumer's pre-approved task mandate (AMP) --
	agentVerified := false
	kyaRating := ""
	if initiator == "agent" {
		if strings.HasPrefix(agentProto, "amp") {
			mr := s.verifyMandate(ctx, r.Header.Get("X-AMP-Task-Token"), agentID, paymentID, req.MerchantID, req.Currency, req.Amount)
			if mr.Mandate != nil {
				span.SetAttributes(attribute.String("amp.task_id", mr.Mandate.TaskID), attribute.Float64("amp.mandate.max_amount", mr.Mandate.MaxAmount))
			}
			kyaRating = kyaRatingOf(mr)
			span.SetAttributes(attribute.String("mandate.verdict", mr.Verdict), attribute.String("amp.kya.rating", kyaRating))
			if mr.Verdict != "allowed" {
				resp.Status = "declined"
				resp.DeclineReason = mr.Reason
				resp.MandateDetail = mr.Detail
				span.SetAttributes(attribute.String("mandate.block_reason", mr.Reason), attribute.Bool("agent.verified", false),
					attribute.String("payment.outcome", "declined"), attribute.String("payment.decline_reason", mr.Reason))
				blocked := []slog.Attr{slog.String("mandate.verdict", "blocked"), slog.String("mandate.block_reason", mr.Reason),
					slog.String("mandate.detail", mr.Detail), slog.String("amp.kya.rating", kyaRating), slog.Bool("agent.verified", false)}
				if mr.Mandate != nil {
					blocked = append(blocked, slog.String("amp.task_id", mr.Mandate.TaskID), slog.Float64("amp.mandate.max_amount", mr.Mandate.MaxAmount),
						slog.String("amp.mandate.currency", mr.Mandate.Currency), slog.String("amp.wallet", mr.Mandate.Wallet))
				}
				if strings.HasPrefix(mr.Reason, "mandate_kya") {
					blocked = append(blocked, slog.String("peer.service", "wallet-sim")) // the Know-Your-Agent registry is a dependency
				}
				logOutcome(slog.LevelWarn, "mandate.blocked",
					fmt.Sprintf("mandate blocked payment %s from agent %s at %s: %s (%s)", paymentID, agentID, req.MerchantID, mr.Reason, mr.Detail),
					http.StatusForbidden, blocked...)
				s.persistAndPublish(ctx, &req, &resp, initiator)
				resp.LatencyMs = time.Since(start).Milliseconds()
				s.record(ctx, &req, &resp, initiator)
				writeJSON(w, http.StatusForbidden, resp)
				return
			}
			agentVerified = true
		}
		span.SetAttributes(attribute.Bool("agent.verified", agentVerified))
	}

	// 1. Risk scoring -------------------------------------------------------
	risk, err := s.scoreRisk(ctx, paymentID, &req, initiator, agentVerified, kyaRating)
	if err != nil {
		s.fail(ctx, span, w, &resp, "risk_engine_unavailable", err, kvToSlog(attrs), start)
		s.record(ctx, &req, &resp, initiator)
		return
	}
	resp.Risk = risk
	span.SetAttributes(
		attribute.Float64("risk.score", risk.Score),
		attribute.String("risk.decision", risk.Decision),
		attribute.String("risk.model_version", risk.ModelVersion),
		attribute.StringSlice("risk.reasons", risk.Reasons),
	)
	if risk.Decision == "deny" {
		resp.Status = "declined"
		resp.DeclineReason = "risk_declined"
		span.SetAttributes(attribute.String("payment.outcome", "declined"), attribute.String("payment.decline_reason", "risk_declined"))
		span.AddEvent("payment.declined_by_risk", trace.WithAttributes(attribute.StringSlice("reasons", risk.Reasons)))
		logOutcome(slog.LevelWarn, "payment.declined",
			fmt.Sprintf("risk model %s declined payment %s for %s before authorisation: score %.3f (%s)",
				risk.ModelVersion, paymentID, req.MerchantID, risk.Score, strings.Join(risk.Reasons, ", ")),
			http.StatusOK)
		s.persistAndPublish(ctx, &req, &resp, initiator)
		resp.LatencyMs = time.Since(start).Milliseconds()
		s.record(ctx, &req, &resp, initiator)
		writeJSON(w, http.StatusOK, resp)
		return
	}

	// 2. Route + authorize ---------------------------------------------------
	route, err := s.route(ctx, paymentID, &req, initiator)
	if err != nil {
		s.fail(ctx, span, w, &resp, "router_unavailable", err, kvToSlog(attrs), start)
		s.record(ctx, &req, &resp, initiator)
		return
	}
	resp.Status = route.Status
	resp.Acquirer = route.Acquirer
	resp.AuthCode = route.AuthCode
	resp.DeclineReason = route.DeclineReason
	span.SetAttributes(
		attribute.String("payment.acquirer", route.Acquirer),
		attribute.Int("route.attempts", len(route.Attempts)),
		attribute.Bool("route.failover", route.Failover),
		attribute.String("payment.outcome", route.Status),
	)
	if route.DeclineReason != "" {
		span.SetAttributes(attribute.String("payment.decline_reason", route.DeclineReason))
	}
	if route.Status == "error" {
		span.SetStatus(codes.Error, "authorization failed at all acquirers")
	}

	// 3. Persist + 4. publish event ------------------------------------------
	s.persistAndPublish(ctx, &req, &resp, initiator)

	resp.LatencyMs = time.Since(start).Milliseconds()
	s.record(ctx, &req, &resp, initiator)

	// The outcome line: acquirer, response code and every routing attempt
	// ("acq-kbank:timeout:2504ms,acq-uob:declined:05:98ms") — enough for a reader,
	// human or AI, to see a failover and its cost without opening the trace.
	last := RouteAttempt{}
	summary := make([]string, 0, len(route.Attempts))
	for _, a := range route.Attempts {
		last = a
		summary = append(summary, fmt.Sprintf("%s:%s:%s:%dms", a.Acquirer, a.Outcome, a.ResponseCode, a.LatencyMs))
	}
	routeFields := []slog.Attr{
		slog.Int("route.attempts", len(route.Attempts)), slog.Bool("route.failover", route.Failover),
		slog.String("route.attempt_summary", strings.Join(summary, ",")),
		slog.String("acquirer.response_code", last.ResponseCode), slog.Int64("acquirer.latency_ms", last.LatencyMs),
	}
	amount := fmt.Sprintf("%.2f %s", req.Amount, req.Currency)
	switch route.Status {
	case "approved":
		logOutcome(slog.LevelInfo, "payment.approved",
			fmt.Sprintf("payment %s approved by %s for %s: %s %s (auth %s, %d attempt(s), %d ms)",
				paymentID, route.Acquirer, req.MerchantID, amount, req.PaymentMethod, route.AuthCode, len(route.Attempts), resp.LatencyMs),
			http.StatusOK, routeFields...)
	case "declined":
		logOutcome(slog.LevelInfo, "payment.declined",
			fmt.Sprintf("payment %s declined by %s for %s: response %s %s (%s %s, failover=%t)",
				paymentID, route.Acquirer, req.MerchantID, last.ResponseCode, route.DeclineReason, amount, req.PaymentMethod, route.Failover),
			http.StatusOK, routeFields...)
	default:
		logOutcome(slog.LevelError, "payment.failed",
			fmt.Sprintf("payment %s failed at every acquirer for %s: %s (%s, attempts %s)",
				paymentID, req.MerchantID, route.DeclineReason, amount, strings.Join(summary, " ")),
			http.StatusOK, routeFields...)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Service) scoreRisk(ctx context.Context, paymentID string, req *PaymentRequest, initiator string, agentVerified bool, kyaRating string) (*RiskResponse, error) {
	payload := map[string]any{
		"payment_id":     paymentID,
		"merchant_id":    req.MerchantID,
		"amount":         req.Amount,
		"currency":       req.Currency,
		"payment_method": req.PaymentMethod,
		"customer":       req.Customer,
		"initiator":      initiator,
		"agent_verified": agentVerified,
		"kya_rating":     kyaRating,
		"channel":        req.Channel,
	}
	if req.Card != nil {
		payload["card_bin"] = req.Card.BIN
		payload["card_network"] = req.Card.Network
	}
	if req.Wallet != nil {
		payload["wallet_provider"] = req.Wallet.Provider
	}
	var out RiskResponse
	if err := s.postJSON(ctx, s.riskURL+"/v1/score", payload, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func (s *Service) route(ctx context.Context, paymentID string, req *PaymentRequest, initiator string) (*RouteResponse, error) {
	payload := map[string]any{
		"payment_id":     paymentID,
		"merchant_id":    req.MerchantID,
		"amount":         req.Amount,
		"currency":       req.Currency,
		"payment_method": req.PaymentMethod,
		"initiator":      initiator,
	}
	if req.Card != nil {
		payload["card_bin"] = req.Card.BIN
		payload["card_network"] = req.Card.Network
	}
	if req.Wallet != nil {
		payload["wallet_provider"] = req.Wallet.Provider
	}
	var out RouteResponse
	if err := s.postJSON(ctx, s.routerURL+"/v1/route", payload, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// persistAndPublish writes the payment to the ledger and emits an event on the
// Redis stream. The traceparent travels inside the message so the async webhook
// worker can join the same trace.
func (s *Service) persistAndPublish(ctx context.Context, req *PaymentRequest, resp *PaymentResponse, initiator string) {
	entry := map[string]any{
		"payment_id":       resp.PaymentID,
		"merchant_id":      req.MerchantID,
		"order_id":         req.OrderID,
		"amount":           req.Amount,
		"currency":         req.Currency,
		"payment_method":   req.PaymentMethod,
		"status":           resp.Status,
		"acquirer":         resp.Acquirer,
		"auth_code":        resp.AuthCode,
		"decline_reason":   resp.DeclineReason,
		"initiator":        initiator,
		"customer_country": req.Customer.Country,
	}
	if resp.Risk != nil {
		entry["risk_score"] = resp.Risk.Score
		entry["risk_model_version"] = resp.Risk.ModelVersion
	}
	if req.Card != nil {
		entry["card_bin"] = req.Card.BIN
		entry["card_network"] = req.Card.Network
	}
	var ack map[string]any
	ledgerStart := time.Now()
	if err := s.postJSON(ctx, s.ledgerURL+"/v1/entries", entry, &ack); err != nil {
		// The ledger is not on the critical path of the authorization decision.
		trace.SpanFromContext(ctx).AddEvent("ledger.write_failed", trace.WithAttributes(attribute.String("error", err.Error())))
		s.log.LogAttrs(ctx, slog.LevelError, fmt.Sprintf("ledger write failed for payment %s after %d ms: %s", resp.PaymentID, time.Since(ledgerStart).Milliseconds(), truncate(err.Error(), 200)),
			slog.String("event", "ledger.write_failed"), slog.String("payment.id", resp.PaymentID), slog.String("merchant.id", req.MerchantID),
			slog.String("payment.outcome", resp.Status), slog.String("peer.service", "ledger"), slog.Int64("duration_ms", time.Since(ledgerStart).Milliseconds()),
			slog.String("error.type", errorType(err)), slog.String("error.message", truncate(err.Error(), 300)))
	} else if ms := time.Since(ledgerStart).Milliseconds(); ms > 500 {
		s.log.LogAttrs(ctx, slog.LevelWarn, fmt.Sprintf("ledger write for payment %s took %d ms", resp.PaymentID, ms),
			slog.String("event", "ledger.write_slow"), slog.String("payment.id", resp.PaymentID), slog.String("merchant.id", req.MerchantID),
			slog.String("peer.service", "ledger"), slog.Int64("duration_ms", ms))
	}

	ctx, span := tracer.Start(ctx, "publish payments.events", trace.WithSpanKind(trace.SpanKindProducer),
		trace.WithAttributes(
			attribute.String("messaging.system", "redis"),
			attribute.String("messaging.destination.name", s.stream),
			attribute.String("messaging.operation.type", "publish"),
		))
	defer span.End()

	carrier := propagation.MapCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	eventJSON, _ := json.Marshal(map[string]any{
		"type":           "payment." + resp.Status,
		"payment_id":     resp.PaymentID,
		"merchant_id":    req.MerchantID,
		"order_id":       req.OrderID,
		"amount":         req.Amount,
		"currency":       req.Currency,
		"status":         resp.Status,
		"decline_reason": resp.DeclineReason,
		"acquirer":       resp.Acquirer,
		"occurred_at":    time.Now().UTC().Format(time.RFC3339Nano),
	})
	values := map[string]any{"event": string(eventJSON)}
	for k, v := range carrier {
		values[k] = v // traceparent / tracestate / baggage
	}
	if _, err := s.rdb.XAdd(ctx, &redis.XAddArgs{Stream: s.stream, MaxLen: 50000, Approx: true, Values: values}).Result(); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "event publish failed")
		s.log.LogAttrs(ctx, slog.LevelError, fmt.Sprintf("could not publish payment.%s event for %s to %s: %s", resp.Status, resp.PaymentID, s.stream, truncate(err.Error(), 200)),
			slog.String("event", "event.publish_failed"), slog.String("payment.id", resp.PaymentID), slog.String("merchant.id", req.MerchantID),
			slog.String("messaging.destination.name", s.stream), slog.String("peer.service", "redis"),
			slog.String("error.type", errorType(err)), slog.String("error.message", truncate(err.Error(), 300)))
	}
}

// fail answers 502 when a dependency on the authorisation path did not answer.
func (s *Service) fail(ctx context.Context, span trace.Span, w http.ResponseWriter, resp *PaymentResponse, reason string, err error, fields []slog.Attr, start time.Time) {
	resp.Status = "error"
	resp.DeclineReason = reason
	span.RecordError(err)
	span.SetStatus(codes.Error, reason)
	span.SetAttributes(attribute.String("payment.outcome", "error"), attribute.String("payment.decline_reason", reason))
	peer := map[string]string{"risk_engine_unavailable": "risk-engine", "router_unavailable": "payment-router"}[reason]
	fields = append(fields,
		slog.String("event", "dependency.unavailable"), slog.String("payment.outcome", "error"), slog.String("payment.decline_reason", reason),
		slog.String("peer.service", peer), slog.String("error.type", errorType(err)), slog.String("error.message", truncate(err.Error(), 300)),
		slog.Int("http.response.status_code", http.StatusBadGateway), slog.Int64("duration_ms", time.Since(start).Milliseconds()))
	s.log.LogAttrs(ctx, slog.LevelError, fmt.Sprintf("payment %s failed: %s did not answer (%s): %s", resp.PaymentID, peer, reason, truncate(err.Error(), 160)), fields...)
	writeJSON(w, http.StatusBadGateway, resp)
}

// errorType classifies an error for the error.type attribute without leaking details.
func errorType(err error) string {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "Client.Timeout") || strings.Contains(msg, "deadline exceeded"):
		return "timeout"
	case strings.Contains(msg, "connection refused"):
		return "connection_refused"
	case strings.Contains(msg, "returned 5"):
		return "upstream_5xx"
	default:
		return fmt.Sprintf("%T", err)
	}
}

// record emits the business metrics. Low-cardinality dimensions only: this is
// deliberately what a payments team would chart on the "war room" dashboard.
func (s *Service) record(ctx context.Context, req *PaymentRequest, resp *PaymentResponse, initiator string) {
	dims := []attribute.KeyValue{
		attribute.String("merchant.id", req.MerchantID),
		attribute.String("payment.currency", req.Currency),
		attribute.String("payment.method", req.PaymentMethod),
		attribute.String("payment.initiator", initiator),
		attribute.String("payment.acquirer", orDefault(resp.Acquirer, "none")),
		attribute.String("payment.outcome", resp.Status),
	}
	paymentsCounter.Add(ctx, 1, metric.WithAttributes(dims...))
	paymentsAmount.Record(ctx, req.Amount, metric.WithAttributes(
		attribute.String("merchant.id", req.MerchantID),
		attribute.String("payment.currency", req.Currency),
		attribute.String("payment.outcome", resp.Status),
	))
	if resp.Status != "approved" {
		declinesCounter.Add(ctx, 1, metric.WithAttributes(append(dims,
			attribute.String("payment.decline_reason", orDefault(resp.DeclineReason, "unknown")))...))
	}
}

func (s *Service) getPayment(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	req, _ := http.NewRequestWithContext(r.Context(), http.MethodGet, s.ledgerURL+"/v1/payments/"+id, nil)
	res, err := s.http.Do(req)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	defer res.Body.Close()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(res.StatusCode)
	io.Copy(w, res.Body)
}

func (s *Service) postJSON(ctx context.Context, url string, in any, out any) error {
	b, _ := json.Marshal(in)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := s.http.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(res.Body, 1<<20))
	if res.StatusCode >= 500 {
		return fmt.Errorf("%s returned %d: %s", url, res.StatusCode, truncate(string(body), 200))
	}
	if err := json.Unmarshal(body, out); err != nil {
		return fmt.Errorf("decode %s: %w", url, err)
	}
	return nil
}

// classifyInitiator detects AI-agent-initiated payments. Real protocols
// (Visa Trusted Agent Protocol, Mastercard Agent Pay, Ant's Agentic Mobile
// Protocol) use signed HTTP messages; the simulator sends the same shape.
func classifyInitiator(r *http.Request) (initiator, protocol, agentID string) {
	if p := r.Header.Get("X-Agent-Protocol"); p != "" {
		return "agent", p, orDefault(r.Header.Get("X-Agent-Id"), "unknown-agent")
	}
	if strings.Contains(strings.ToLower(r.UserAgent()), "agent") {
		return "agent", "unsigned", "unknown-agent"
	}
	return "human", "", ""
}

// ---------- helpers ----------

func kyaRatingOf(mr MandateResult) string {
	if mr.KYA != nil && mr.KYA.Rating != "" {
		return mr.KYA.Rating
	}
	if mr.Mandate != nil && mr.Mandate.KYARating != "" {
		return mr.Mandate.KYARating
	}
	return "unknown"
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func randomID(n int) string {
	b := make([]byte, n/2)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// ---------- main ----------

func main() {
	boot := slog.New(slog.NewJSONHandler(os.Stdout, nil)) // before the SDK exists
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	shutdown, err := setupOTel(ctx)
	if err != nil {
		boot.Error("otel setup failed", "error", err)
		os.Exit(1)
	}
	defer func() { _ = shutdown(context.Background()) }()
	log := newLogger() // stdout JSON + OTLP, see logging.go

	paymentsCounter, _ = meter.Int64Counter("tuktukpay.payments.count", metric.WithDescription("Payments processed"))
	paymentsAmount, _ = meter.Float64Histogram("tuktukpay.payments.amount", metric.WithDescription("Payment amount in minor units"))
	declinesCounter, _ = meter.Int64Counter("tuktukpay.payments.declines", metric.WithDescription("Declined or failed payments"))
	mandateCounter, _ = meter.Int64Counter("tuktukpay.agent_mandates", metric.WithDescription("AMP task-mandate verifications by verdict and block reason"))

	svc, err := newService(log)
	if err != nil {
		log.Error("service init failed", "event", "service.start_failed", "error.message", err.Error())
		os.Exit(1)
	}

	// One otelhttp handler per route so SERVER spans get a low-cardinality name
	// ("GET /v1/payments/{id}", never the actual id) plus the http.route attribute.
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]string{"status": "ok"}) })
	mux.Handle("POST /v1/payments", otelhttp.NewHandler(
		otelhttp.WithRouteTag("/v1/payments", http.HandlerFunc(svc.createPayment)), "POST /v1/payments"))
	mux.Handle("GET /v1/payments/{id}", otelhttp.NewHandler(
		otelhttp.WithRouteTag("/v1/payments/{id}", http.HandlerFunc(svc.getPayment)), "GET /v1/payments/{id}"))

	srv := &http.Server{Addr: ":" + getenv("PORT", "8080"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Info("checkout-api listening on "+srv.Addr, "event", "service.started", "server.address", srv.Addr,
			"peer.services", []string{"risk-engine", "payment-router", "ledger", "wallet-sim", "redis"})
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("server error: "+err.Error(), "event", "service.failed", "error.message", err.Error())
			stop()
		}
	}()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}
