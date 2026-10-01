package com.tuktukpay.router;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Collections;
import java.util.Map;
import java.util.logging.Logger;

/** Polls the workshop chaos-controller every 2s and caches the flags. */
public final class ChaosFlags {
    private static final Logger LOG = Logger.getLogger(ChaosFlags.class.getName());
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build();
    private final URI url;
    private volatile Map<String, Object> flags = Collections.emptyMap();

    public ChaosFlags(String baseUrl) {
        this.url = URI.create(baseUrl.replaceAll("/$", "") + "/flags");
        Thread t = new Thread(this::loop, "chaos-poller");
        t.setDaemon(true);
        t.start();
    }

    private void loop() {
        while (true) {
            try {
                HttpRequest req = HttpRequest.newBuilder(url).timeout(Duration.ofMillis(1500)).GET().build();
                HttpResponse<String> res = http.send(req, HttpResponse.BodyHandlers.ofString());
                if (res.statusCode() == 200) flags = Json.parseObject(res.body());
            } catch (Exception e) {
                LOG.fine("chaos-controller unreachable: " + e.getMessage());
            }
            try { Thread.sleep(2000); } catch (InterruptedException e) { return; }
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
