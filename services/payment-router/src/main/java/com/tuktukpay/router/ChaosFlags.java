package com.tuktukpay.router;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.logging.Logger;
import java.util.stream.Collectors;

/**
 * Polls the workshop chaos-controller every 2s and caches the flags. Transitions of
 * the flags this service consumes (`watch`) are logged as config.changed events — the
 * "feature flag flipped at 10:02" breadcrumb an AI root-cause analysis latches onto.
 */
public final class ChaosFlags {
    private static final Logger LOG = Logger.getLogger(ChaosFlags.class.getName());
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build();
    private final URI url;
    private final Set<String> watch;
    private final Map<String, String> known = new HashMap<>();
    private volatile Map<String, Object> flags = Collections.emptyMap();

    public ChaosFlags(String baseUrl) {
        this(baseUrl, Set.of());
    }

    public ChaosFlags(String baseUrl, Set<String> watch) {
        this.url = URI.create(baseUrl.replaceAll("/$", "") + "/flags");
        this.watch = watch;
        Thread t = new Thread(this::loop, "chaos-poller");
        t.setDaemon(true);
        t.start();
    }

    private void loop() {
        while (true) {
            try {
                HttpRequest req = HttpRequest.newBuilder(url).timeout(Duration.ofMillis(1500)).GET().build();
                HttpResponse<String> res = http.send(req, HttpResponse.BodyHandlers.ofString());
                if (res.statusCode() == 200) {
                    flags = Json.parseObject(res.body());
                    logTransitions();
                }
            } catch (Exception e) {
                LOG.fine("chaos-controller unreachable: " + e.getMessage());
            }
            try { Thread.sleep(2000); } catch (InterruptedException e) { return; }
        }
    }

    @SuppressWarnings("unchecked")
    private void logTransitions() {
        for (String name : watch) {
            boolean enabled = enabled(name);
            Object f = flags.get(name);
            Map<String, Object> params = Collections.emptyMap();
            if (f instanceof Map && ((Map<String, Object>) f).get("params") instanceof Map) {
                params = (Map<String, Object>) ((Map<String, Object>) f).get("params");
            }
            String paramsJson = Json.write(params);
            String current = enabled + "|" + paramsJson;
            String previous = known.put(name, current);
            if (previous == null || previous.equals(current)) continue;
            String paramText = params.entrySet().stream().map(e -> e.getKey() + "=" + e.getValue()).collect(Collectors.joining(" "));
            Log.event(Log.Level.INFO, "config.changed",
                    "feature flag " + name + (enabled ? " enabled" : " disabled") + (enabled && !paramText.isEmpty() ? ": " + paramText : ""),
                    "feature_flag.name", name, "feature_flag.enabled", enabled, "feature_flag.params", paramsJson);
        }
    }

    @SuppressWarnings("unchecked")
    public boolean enabled(String name) {
        Object f = flags.get(name);
        return f instanceof Map && Boolean.TRUE.equals(((Map<String, Object>) f).get("enabled"));
    }

    @SuppressWarnings("unchecked")
    public String param(String name, String key, String def) {
        Object f = flags.get(name);
        if (!(f instanceof Map)) return def;
        Object params = ((Map<String, Object>) f).get("params");
        if (!(params instanceof Map)) return def;
        Object v = ((Map<String, Object>) params).get(key);
        return v == null ? def : String.valueOf(v);
    }
}
