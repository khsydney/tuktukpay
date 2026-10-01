package com.tuktukpay.router;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * TukTukPay payment-router — picks an acquirer for each payment, authorizes it,
 * and fails over to a backup acquirer on timeouts / system errors. Modelled on
 * a PSP "payment orchestration" layer (think one API across many acquirers with
 * primary/backup routing).
 *
 * Deliberately plain Java (JDK HTTP server + client, no framework, no OpenTelemetry
 * code at all): every span you see for this service comes from the Splunk
 * Distribution of the OpenTelemetry Java agent attached with -javaagent. The
 * business attributes are exposed as response headers and captured by the agent
 * (OTEL_INSTRUMENTATION_HTTP_SERVER_CAPTURE_RESPONSE_HEADERS), then renamed by
 * the collector's transform processor into clean span tags.
 */
public final class RouterServer {
    private static final Logger LOG = Logger.getLogger(RouterServer.class.getName());

    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build();
    private final String acquirerBase;
    private final Duration acquirerTimeout;
    private final ChaosFlags chaos;

    public RouterServer(String acquirerBase, Duration acquirerTimeout, ChaosFlags chaos) {
        this.acquirerBase = acquirerBase.replaceAll("/$", "");
        this.acquirerTimeout = acquirerTimeout;
        this.chaos = chaos;
    }

    // ---------- routing table ----------

    /** Returns [primary, backup] acquirers for a payment. */
    static List<String> route(String method, String currency, String network) {
        List<String> r = new ArrayList<>();
        switch (method) {
            case "wallet":
                r.add("acq-alipayplus");
                break;
            case "qr":
            case "bank_transfer":
                r.add("acq-itmx");
                break;
            default: // card
                if ("amex".equals(network)) {
                    r.add("acq-uob");
                } else if ("THB".equals(currency)) {
                    r.add("acq-kbank");   // Kasikornbank: best domestic THB approval
                    r.add("acq-uob");     // UOB regional (SG-booked) backup
                } else if ("IDR".equals(currency) || "PHP".equals(currency) || "VND".equals(currency)) {
                    r.add("acq-cimb");
                    r.add("acq-uob");
                } else {                  // SGD, MYR, USD
                    r.add("acq-uob");
                    r.add("acq-cimb");
                }
        }
        return r;
    }

    // ---------- HTTP ----------

    public void start(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/healthz", ex -> respond(ex, 200, "{\"status\":\"ok\"}", Map.of()));
        server.createContext("/v1/route", this::handleRoute);
        server.setExecutor(Executors.newFixedThreadPool(64));
        server.start();
        LOG.info("payment-router listening on :" + port + " acquirers=" + acquirerBase);
    }

    private void handleRoute(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            respond(ex, 405, "{\"error\":\"method not allowed\"}", Map.of());
            return;
        }
        Map<String, Object> req;
        try {
            req = Json.parseObject(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
        } catch (RuntimeException e) {
            respond(ex, 400, "{\"error\":\"invalid json\"}", Map.of());
            return;
        }

        String paymentId = Json.str(req, "payment_id", "unknown");
        String merchant = Json.str(req, "merchant_id", "unknown");
        String method = Json.str(req, "payment_method", "card");
        String currency = Json.str(req, "currency", "SGD");
        String network = Json.str(req, "card_network", "");
        String bin = Json.str(req, "card_bin", "");
        double amount = Json.num(req, "amount", 0);

        List<String> candidates = route(method, currency, network);
        if (chaos.enabled("router_failover_disabled") && candidates.size() > 1) {
            candidates = List.of(candidates.get(0));
        }
        List<Map<String, Object>> attempts = new ArrayList<>();
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", "error");
        result.put("acquirer", candidates.get(0));
        result.put("auth_code", "");
        result.put("decline_reason", "acquirer_unavailable");
        result.put("failover", false);

        for (int n = 0; n < candidates.size(); n++) {
            String acquirer = candidates.get(n);
            long t0 = System.nanoTime();
            Map<String, Object> attempt = new LinkedHashMap<>();
            attempt.put("acquirer", acquirer);
            try {
                Map<String, Object> auth = authorize(acquirer, req);
                long ms = (System.nanoTime() - t0) / 1_000_000;
                String status = Json.str(auth, "status", "declined");
                String code = Json.str(auth, "response_code", "");
                attempt.put("outcome", status);
                attempt.put("response_code", code);
                attempt.put("latency_ms", ms);
                attempts.add(attempt);

                boolean retryable = "91".equals(code) || "96".equals(code) || "error".equals(status);
                if (retryable && n + 1 < candidates.size()) {
                    LOG.warning("acquirer " + acquirer + " returned " + code + " for " + paymentId + "; failing over");
                    result.put("failover", true);
                    continue;
                }
                result.put("status", status);
                result.put("acquirer", acquirer);
                result.put("auth_code", Json.str(auth, "auth_code", ""));
                result.put("decline_reason", Json.str(auth, "decline_reason", ""));
                if ("approved".equals(status)) result.put("decline_reason", "");
                break;
            } catch (HttpTimeoutException e) {
                long ms = (System.nanoTime() - t0) / 1_000_000;
                attempt.put("outcome", "timeout");
                attempt.put("response_code", "timeout");
                attempt.put("latency_ms", ms);
                attempts.add(attempt);
                LOG.log(Level.WARNING, "acquirer " + acquirer + " timed out after " + ms + "ms for " + paymentId
                        + " merchant=" + merchant + " bin=" + bin + " currency=" + currency);
                if (n + 1 < candidates.size()) {
                    result.put("failover", true);
                    continue;
                }
                result.put("status", "error");
                result.put("acquirer", acquirer);
                result.put("decline_reason", "acquirer_timeout");
            } catch (Exception e) {
                long ms = (System.nanoTime() - t0) / 1_000_000;
                attempt.put("outcome", "error");
                attempt.put("response_code", e.getClass().getSimpleName());
                attempt.put("latency_ms", ms);
                attempts.add(attempt);
                LOG.log(Level.SEVERE, "acquirer " + acquirer + " call failed for " + paymentId + ": " + e);
                if (n + 1 < candidates.size()) {
                    result.put("failover", true);
                    continue;
                }
                result.put("status", "error");
                result.put("acquirer", acquirer);
                result.put("decline_reason", "acquirer_unavailable");
            }
        }
        result.put("attempts", attempts);

        // Business attributes for the zero-code agent to capture as span tags.
        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("X-Payment-Acquirer", String.valueOf(result.get("acquirer")));
        headers.put("X-Route-Attempts", String.valueOf(attempts.size()));
        headers.put("X-Route-Failover", String.valueOf(result.get("failover")));
        headers.put("X-Route-Outcome", String.valueOf(result.get("status")));

        int code = "error".equals(result.get("status")) ? 502 : 200;
        if (code == 502) {
            LOG.severe("authorization failed at all acquirers payment_id=" + paymentId + " merchant=" + merchant
                    + " amount=" + amount + " " + currency + " attempts=" + Json.write(attempts));
        }
        respond(ex, code, Json.write(result), headers);
    }

    private Map<String, Object> authorize(String acquirer, Map<String, Object> req) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(acquirerBase + "/acquirers/" + acquirer + "/authorize"))
                .timeout(acquirerTimeout)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(req)))
                .build();
        HttpResponse<String> res = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() >= 500) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("status", "error");
            m.put("response_code", "96");
            m.put("decline_reason", "acquirer_system_error");
            return m;
        }
        return Json.parseObject(res.body());
    }

    private static void respond(HttpExchange ex, int status, String body, Map<String, String> headers) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        headers.forEach((k, v) -> ex.getResponseHeaders().set(k, v));
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    public static void main(String[] args) throws IOException {
        String acquirers = System.getenv().getOrDefault("ACQUIRER_URL", "http://localhost:8083");
        String chaosUrl = System.getenv().getOrDefault("CHAOS_URL", "http://localhost:8090");
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8082"));
        long timeoutMs = Long.parseLong(System.getenv().getOrDefault("ACQUIRER_TIMEOUT_MS", "2500"));
        new RouterServer(acquirers, Duration.ofMillis(timeoutMs), new ChaosFlags(chaosUrl)).start(port);
    }
}
