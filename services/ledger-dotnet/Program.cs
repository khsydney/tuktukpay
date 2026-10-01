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
var app = builder.Build();

var chaos = new ChaosFlags(Environment.GetEnvironmentVariable("CHAOS_URL") ?? "http://localhost:8090");
var connString = ToNpgsql(Environment.GetEnvironmentVariable("DATABASE_URL") ?? "postgres://tuktukpay:tuktukpay@localhost:5432/tuktukpay");
var defaultPool = int.Parse(Environment.GetEnvironmentVariable("PG_POOL_SIZE") ?? "10");

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
    }
    return tiny;
}

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
    await using var conn = await ds.OpenConnectionAsync();
    activity?.SetTag("db.pool.wait_ms", sw.ElapsedMilliseconds);
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
        return Results.Created($"/v1/payments/{p.PaymentId}", new { ok = true, payment_id = p.PaymentId, pool_wait_ms = sw.ElapsedMilliseconds });
    }
    catch (Exception e)
    {
        await tx.RollbackAsync();
        app.Logger.LogError(e, "ledger write failed payment_id={PaymentId}", p.PaymentId);
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

app.Run($"http://0.0.0.0:{Environment.GetEnvironmentVariable("PORT") ?? "8084"}");

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
