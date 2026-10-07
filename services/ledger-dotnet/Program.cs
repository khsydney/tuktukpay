// TukTukPay ledger — .NET 8 minimal API + Npgsql. Same contract as ../ledger (Node.js).
//
// No OpenTelemetry SDK code here: the Splunk Distribution of OpenTelemetry .NET
// (auto-instrumentation, see Dockerfile) instruments ASP.NET Core, HttpClient
// and Npgsql at runtime. Business attributes are added through the built-in
// System.Diagnostics.Activity API, which the auto-instrumentation exports as
// span attributes.

using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;
using Npgsql;
using TukTukPay.Ledger;

var builder = WebApplication.CreateBuilder(args);
builder.Services.ConfigureHttpJsonOptions(o =>
{
    o.SerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower;
    o.SerializerOptions.DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull;
});
// Structured logs (docs/log-schema.md): one JSON line per event on stdout with TraceId /
// SpanId scopes; the Splunk .NET auto-instrumentation also exports every ILogger record
// over OTLP (OTEL_DOTNET_AUTO_LOGS_ENABLED, default on) with the message-template
// properties as attributes and the trace context attached.
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole(o =>
{
    o.IncludeScopes = true;
    o.UseUtcTimestamp = true;
    o.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ";
});
builder.Logging.Configure(o => o.ActivityTrackingOptions = ActivityTrackingOptions.TraceId | ActivityTrackingOptions.SpanId);
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
var app = builder.Build();
var log = app.Logger;

var chaos = new ChaosFlags(Environment.GetEnvironmentVariable("CHAOS_URL") ?? "http://localhost:8090", log, new[] { "ledger_db_slow" });
var databaseUrl = Environment.GetEnvironmentVariable("DATABASE_URL") ?? "postgres://tuktukpay:tuktukpay@localhost:5432/tuktukpay";
var connString = ToNpgsql(databaseUrl);
var dbHost = Uri.TryCreate(databaseUrl, UriKind.Absolute, out var dbUri) ? dbUri.Host : "postgres";
var defaultPool = int.Parse(Environment.GetEnvironmentVariable("PG_POOL_SIZE") ?? "10");
var poolWaitWarnMs = int.Parse(Environment.GetEnvironmentVariable("POOL_WAIT_WARN_MS") ?? "100");
var slowWriteMs = int.Parse(Environment.GetEnvironmentVariable("SLOW_WRITE_MS") ?? "500");

var normal = new NpgsqlDataSourceBuilder(connString + $";Maximum Pool Size={defaultPool};Application Name=ledger-dotnet").Build();
NpgsqlDataSource? tiny = null;
int tinySize = -1;

NpgsqlDataSource Pool()
{
    if (!chaos.Enabled("ledger_db_slow")) return normal;
    var size = chaos.Param("ledger_db_slow", "pool_size", 2);
    if (tiny is null || tinySize != size)
    {
        tiny?.Dispose();
        tiny = new NpgsqlDataSourceBuilder(connString + $";Maximum Pool Size={size};Application Name=ledger-dotnet-degraded").Build();
        tinySize = size;
        log.LogWarning("{event}: ledger connection pool to {db.host} reconfigured: max {db.pool.max} connections (was {db.pool.previous_max})",
            "db.pool.reconfigured", dbHost, size, defaultPool);
    }
    return tiny;
}

int PoolMax() => chaos.Enabled("ledger_db_slow") ? tinySize : defaultPool;

app.MapGet("/healthz", async () =>
{
    try
    {
        await using var cmd = normal.CreateCommand("SELECT 1");
        await cmd.ExecuteScalarAsync();
        return Results.Ok(new { status = "ok", runtime = ".NET " + Environment.Version });
    }
    catch (Exception e)
    {
        return Results.Json(new { status = "db_unavailable", error = e.Message }, statusCode: 503);
    }
});

app.MapPost("/v1/entries", async (EntryRequest p) =>
{
    if (string.IsNullOrEmpty(p.PaymentId) || string.IsNullOrEmpty(p.MerchantId))
        return Results.BadRequest(new { error = "payment_id and merchant_id are required" });

    var activity = Activity.Current;
    activity?.SetTag("payment.id", p.PaymentId);
    activity?.SetTag("merchant.id", p.MerchantId);
    activity?.SetTag("payment.outcome", p.Status ?? "unknown");

    var ds = Pool();
    var sw = Stopwatch.StartNew();
    NpgsqlConnection conn;
    try
    {
        conn = await ds.OpenConnectionAsync();
    }
    catch (Exception e)
    {
        log.LogError(e, "{event}: ledger could not get a database connection from {db.host} for payment {payment.id} after {duration_ms} ms: {error.message}",
            "db.connect_failed", dbHost, p.PaymentId, sw.ElapsedMilliseconds, e.Message);
        return Results.Json(new { error = "database unavailable" }, statusCode: 503);
    }
    await using var _ = conn;
    var waitMs = sw.ElapsedMilliseconds;
    activity?.SetTag("db.pool.wait_ms", waitMs);
    if (waitMs > poolWaitWarnMs)
    {
        // Act 5: the pool is too small for the write rate — requests queue for a connection.
        log.LogWarning("{event}: payment {payment.id} waited {db.pool.wait_ms} ms for a database connection (pool max {db.pool.max}, merchant {merchant.id})",
            "db.pool.wait", p.PaymentId, waitMs, PoolMax(), p.MerchantId);
    }
    await using var tx = await conn.BeginTransactionAsync();
    try
    {
        if (chaos.Enabled("ledger_db_slow"))
        {
            var sleepMs = chaos.Param("ledger_db_slow", "sleep_ms", 250);
            await using var sleep = new NpgsqlCommand("SELECT pg_sleep($1)", conn, tx);
            sleep.Parameters.AddWithValue(sleepMs / 1000.0);
            await sleep.ExecuteNonQueryAsync();
        }

        await using (var cmd = new NpgsqlCommand(
            @"INSERT INTO payments (payment_id, merchant_id, order_id, amount, currency, payment_method, status, acquirer, auth_code,
                                    decline_reason, initiator, card_bin, card_network, customer_country, risk_score, risk_model_version)
              VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
              ON CONFLICT (payment_id) DO UPDATE SET status = EXCLUDED.status, acquirer = EXCLUDED.acquirer,
                auth_code = EXCLUDED.auth_code, decline_reason = EXCLUDED.decline_reason, updated_at = now()", conn, tx))
        {
            cmd.Parameters.AddWithValue(p.PaymentId);
            cmd.Parameters.AddWithValue(p.MerchantId);
            cmd.Parameters.AddWithValue((object?)p.OrderId ?? DBNull.Value);
            cmd.Parameters.AddWithValue((decimal)p.Amount);
            cmd.Parameters.AddWithValue((object?)p.Currency ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)p.PaymentMethod ?? DBNull.Value);
            cmd.Parameters.AddWithValue(p.Status ?? "unknown");
            cmd.Parameters.AddWithValue((object?)p.Acquirer ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)p.AuthCode ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)p.DeclineReason ?? DBNull.Value);
            cmd.Parameters.AddWithValue(p.Initiator ?? "human");
            cmd.Parameters.AddWithValue((object?)p.CardBin ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)p.CardNetwork ?? DBNull.Value);
            cmd.Parameters.AddWithValue((object?)p.CustomerCountry ?? DBNull.Value);
            cmd.Parameters.AddWithValue(p.RiskScore.HasValue ? (object)(decimal)p.RiskScore.Value : DBNull.Value);
            cmd.Parameters.AddWithValue((object?)p.RiskModelVersion ?? DBNull.Value);
            await cmd.ExecuteNonQueryAsync();
        }

        if (p.Status == "approved")
        {
            await using var cmd = new NpgsqlCommand(
                @"INSERT INTO ledger_entries (payment_id, account, direction, amount, currency)
                  VALUES ($1, $2, 'credit', $3, $4), ($1, $5, 'debit', $3, $4)", conn, tx);
            cmd.Parameters.AddWithValue(p.PaymentId);
            cmd.Parameters.AddWithValue($"merchant:{p.MerchantId}:receivable");
            cmd.Parameters.AddWithValue((decimal)p.Amount);
            cmd.Parameters.AddWithValue((object?)p.Currency ?? DBNull.Value);
            cmd.Parameters.AddWithValue($"acquirer:{p.Acquirer ?? "unknown"}:settlement");
            await cmd.ExecuteNonQueryAsync();
        }

        await tx.CommitAsync();
        var totalMs = sw.ElapsedMilliseconds;
        if (totalMs > slowWriteMs)
            log.LogWarning("{event}: ledger write for payment {payment.id} took {duration_ms} ms ({db.pool.wait_ms} ms waiting for a connection, pool max {db.pool.max}, merchant {merchant.id})",
                "ledger.write_slow", p.PaymentId, totalMs, waitMs, PoolMax(), p.MerchantId);
        else
            log.LogDebug("{event}: recorded payment {payment.id} ({payment.outcome}) in {duration_ms} ms", "ledger.entry_recorded", p.PaymentId, p.Status ?? "unknown", totalMs);
        return Results.Created($"/v1/payments/{p.PaymentId}", new { ok = true, payment_id = p.PaymentId, pool_wait_ms = waitMs });
    }
    catch (Exception e)
    {
        await tx.RollbackAsync();
        log.LogError(e, "{event}: ledger write failed for payment {payment.id} (merchant {merchant.id}) after {duration_ms} ms: {error.type} {error.message}",
            "ledger.write_failed", p.PaymentId, p.MerchantId, sw.ElapsedMilliseconds, e.GetType().Name, e.Message);
        return Results.Json(new { error = e.Message }, statusCode: 500);
    }
});

app.MapGet("/v1/payments/{id}", async (string id) =>
{
    await using var cmd = Pool().CreateCommand("SELECT payment_id, merchant_id, order_id, amount, currency, payment_method, status, acquirer, auth_code, decline_reason, initiator, card_bin, card_network, customer_country, risk_score, risk_model_version, created_at FROM payments WHERE payment_id = $1");
    cmd.Parameters.AddWithValue(id);
    await using var reader = await cmd.ExecuteReaderAsync();
    if (!await reader.ReadAsync()) return Results.NotFound(new { error = "payment not found" });
    return Results.Ok(ReadPayment(reader));
});

app.MapGet("/v1/payments", async (string? merchant_id, string? status, int? limit) =>
{
    if (string.IsNullOrEmpty(merchant_id)) return Results.BadRequest(new { error = "merchant_id is required" });
    var max = Math.Min(limit ?? 20, 200);
    var sql = "SELECT payment_id, merchant_id, order_id, amount, currency, payment_method, status, acquirer, auth_code, decline_reason, initiator, card_bin, card_network, customer_country, risk_score, risk_model_version, created_at FROM payments WHERE merchant_id = $1"
              + (status is null ? "" : " AND status = $3") + " ORDER BY created_at DESC LIMIT $2";
    await using var cmd = Pool().CreateCommand(sql);
    cmd.Parameters.AddWithValue(merchant_id);
    cmd.Parameters.AddWithValue(max);
    if (status is not null) cmd.Parameters.AddWithValue(status);
    var rows = new List<Dictionary<string, object?>>();
    await using var reader = await cmd.ExecuteReaderAsync();
    while (await reader.ReadAsync()) rows.Add(ReadPayment(reader));
    return Results.Ok(new { merchant_id, count = rows.Count, payments = rows });
});

app.MapGet("/v1/merchants/{id}/summary", async (string id, int? hours) =>
{
    var window = Math.Min(hours ?? 1, 168);
    await using var cmd = Pool().CreateCommand(
        @"SELECT status, decline_reason, acquirer, count(*)::int AS count, sum(amount)::float8 AS amount
            FROM payments
           WHERE merchant_id = $1 AND created_at > now() - ($2 || ' hours')::interval
           GROUP BY status, decline_reason, acquirer
           ORDER BY count DESC");
    cmd.Parameters.AddWithValue(id);
    cmd.Parameters.AddWithValue(window.ToString());
    var rows = new List<Dictionary<string, object?>>();
    await using var reader = await cmd.ExecuteReaderAsync();
    while (await reader.ReadAsync())
    {
        rows.Add(new Dictionary<string, object?>
        {
            ["status"] = reader.GetString(0),
            ["decline_reason"] = reader.IsDBNull(1) ? null : reader.GetString(1),
            ["acquirer"] = reader.IsDBNull(2) ? null : reader.GetString(2),
            ["count"] = reader.GetInt32(3),
            ["amount"] = reader.IsDBNull(4) ? 0d : reader.GetDouble(4),
        });
    }
    var total = rows.Sum(r => (int)r["count"]!);
    var approved = rows.Where(r => (string?)r["status"] == "approved").Sum(r => (int)r["count"]!);
    return Results.Ok(new { merchant_id = id, window_hours = window, total, approved, approval_rate = total == 0 ? (double?)null : (double)approved / total, breakdown = rows });
});

var port = Environment.GetEnvironmentVariable("PORT") ?? "8084";
log.LogInformation("{event}: ledger (.NET {runtime}) listening on :{server.port} (postgres {db.host}, pool max {db.pool.max})",
    "service.started", Environment.Version, port, dbHost, defaultPool);
app.Run($"http://0.0.0.0:{port}");

static Dictionary<string, object?> ReadPayment(NpgsqlDataReader r)
{
    var d = new Dictionary<string, object?>();
    for (var i = 0; i < r.FieldCount; i++)
        d[r.GetName(i)] = r.IsDBNull(i) ? null : r.GetValue(i);
    return d;
}

// postgres://user:pass@host:port/db -> Npgsql key/value connection string
static string ToNpgsql(string url)
{
    if (!url.StartsWith("postgres", StringComparison.OrdinalIgnoreCase)) return url;
    var u = new Uri(url);
    var userInfo = u.UserInfo.Split(':', 2);
    var user = Uri.UnescapeDataString(userInfo[0]);
    var pass = userInfo.Length > 1 ? Uri.UnescapeDataString(userInfo[1]) : "";
    var port = u.Port > 0 ? u.Port : 5432;
    return $"Host={u.Host};Port={port};Username={user};Password={pass};Database={u.AbsolutePath.TrimStart('/')}";
}

public sealed record EntryRequest(
    [property: JsonPropertyName("payment_id")] string PaymentId,
    [property: JsonPropertyName("merchant_id")] string MerchantId,
    [property: JsonPropertyName("order_id")] string? OrderId,
    [property: JsonPropertyName("amount")] double Amount,
    [property: JsonPropertyName("currency")] string? Currency,
    [property: JsonPropertyName("payment_method")] string? PaymentMethod,
    [property: JsonPropertyName("status")] string? Status,
    [property: JsonPropertyName("acquirer")] string? Acquirer,
    [property: JsonPropertyName("auth_code")] string? AuthCode,
    [property: JsonPropertyName("decline_reason")] string? DeclineReason,
    [property: JsonPropertyName("initiator")] string? Initiator,
    [property: JsonPropertyName("card_bin")] string? CardBin,
    [property: JsonPropertyName("card_network")] string? CardNetwork,
    [property: JsonPropertyName("customer_country")] string? CustomerCountry,
    [property: JsonPropertyName("risk_score")] double? RiskScore,
    [property: JsonPropertyName("risk_model_version")] string? RiskModelVersion);
