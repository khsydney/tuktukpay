package main

import (
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/log/global"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// setupOTel wires the OpenTelemetry SDK: OTLP/gRPC exporters for traces, metrics
// and logs pointed at the Splunk Distribution of the OpenTelemetry Collector
// (OTEL_EXPORTER_OTLP_ENDPOINT), a resource built from OTEL_RESOURCE_ATTRIBUTES
// (deployment.environment, service.version ...) and W3C trace-context +
// baggage propagation. Nothing here is Splunk-specific: the same binary can
// export to any OTLP backend, which is the whole "no lock-in" argument.
//
// The Splunk Distribution of OpenTelemetry Go (github.com/signalfx/splunk-otel-go)
// wraps this into a single distro.Run() call and adds Splunk defaults; we use the
// upstream SDK here so every step is visible.
func setupOTel(ctx context.Context) (func(context.Context) error, error) {
	if os.Getenv("OTEL_SERVICE_NAME") == "" {
		_ = os.Setenv("OTEL_SERVICE_NAME", serviceName)
	}

	res, err := resource.New(ctx,
		resource.WithFromEnv(),      // OTEL_RESOURCE_ATTRIBUTES + OTEL_SERVICE_NAME
		resource.WithTelemetrySDK(), // telemetry.sdk.*
		resource.WithHost(),
		resource.WithAttributes(semconv.ServiceNamespace("tuktukpay")),
	)
	if err != nil && !errors.Is(err, resource.ErrPartialResource) {
		return nil, err
	}

	traceExp, err := otlptracegrpc.New(ctx) // endpoint/insecure from OTEL_EXPORTER_OTLP_* env
	if err != nil {
		return nil, err
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExp),
		sdktrace.WithResource(res),
		// AlwaysSample: the collector/back end keeps 100% of traces (Splunk NoSample).
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())),
	)
	otel.SetTracerProvider(tp)

	metricExp, err := otlpmetricgrpc.New(ctx)
	if err != nil {
		return nil, err
	}
	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExp, sdkmetric.WithInterval(10*time.Second))),
	)
	otel.SetMeterProvider(mp)

	// Logs: the same resource and endpoint. logging.go bridges slog into this
	// provider, so every log line reaches the collector with trace_id / span_id.
	logExp, err := otlploggrpc.New(ctx)
	if err != nil {
		return nil, err
	}
	lp := sdklog.NewLoggerProvider(
		sdklog.WithResource(res),
		sdklog.WithProcessor(sdklog.NewBatchProcessor(logExp)),
	)
	global.SetLoggerProvider(lp)

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		tolerantTraceContext{}, propagation.Baggage{},
	))

	return func(ctx context.Context) error {
		return errors.Join(tp.Shutdown(ctx), mp.Shutdown(ctx), lp.Shutdown(ctx))
	}, nil
}

// tolerantTraceContext accepts W3C Trace Context Level 2 headers.
//
// Newer OpenTelemetry SDKs (Python 1.3x+, for example) set the "random trace id"
// flag, so traceparent ends in "-03". opentelemetry-go before v1.42.0 rejects any
// version-00 flags value above 0x02 and silently starts a new trace, which breaks
// the agent -> checkout-api link. Until the module can move to v1.42+ (Go 1.25),
// mask the flags down to the sampled bit before delegating to the standard
// propagator. Harmless on newer versions.
type tolerantTraceContext struct{ propagation.TraceContext }

func (t tolerantTraceContext) Extract(ctx context.Context, carrier propagation.TextMapCarrier) context.Context {
	if tp := carrier.Get("traceparent"); tp != "" {
		if parts := strings.Split(tp, "-"); len(parts) == 4 && len(parts[3]) == 2 {
			if b, err := hex.DecodeString(parts[3]); err == nil && len(b) == 1 && b[0] > 2 {
				parts[3] = fmt.Sprintf("%02x", b[0]&0x01)
				carrier = &overrideCarrier{TextMapCarrier: carrier, traceparent: strings.Join(parts, "-")}
			}
		}
	}
	return t.TraceContext.Extract(ctx, carrier)
}

type overrideCarrier struct {
	propagation.TextMapCarrier
	traceparent string
}

func (o *overrideCarrier) Get(key string) string {
	if strings.EqualFold(key, "traceparent") {
		return o.traceparent
	}
	return o.TextMapCarrier.Get(key)
}
