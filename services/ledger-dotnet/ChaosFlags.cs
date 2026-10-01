using System.Text.Json;

namespace TukTukPay.Ledger;

/// <summary>Polls the workshop chaos-controller every 2s and caches the flags.</summary>
public sealed class ChaosFlags : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMilliseconds(1500) };
    private readonly string _url;
    private readonly Timer _timer;
    private JsonDocument? _flags;

    public ChaosFlags(string baseUrl)
    {
        _url = baseUrl.TrimEnd('/') + "/flags";
        _timer = new Timer(async _ => await PollAsync(), null, TimeSpan.Zero, TimeSpan.FromSeconds(2));
    }

    private async Task PollAsync()
    {
        try
        {
            var json = await _http.GetStringAsync(_url);
            var doc = JsonDocument.Parse(json);
            Interlocked.Exchange(ref _flags, doc)?.Dispose();
        }
        catch
        {
            // keep the last good copy; never let chaos polling break the service
        }
    }

    public bool Enabled(string name)
    {
        var flags = _flags;
        return flags is not null
            && flags.RootElement.TryGetProperty(name, out var flag)
            && flag.TryGetProperty("enabled", out var enabled)
            && enabled.ValueKind == JsonValueKind.True;
    }

    public int Param(string name, string key, int defaultValue)
    {
        var flags = _flags;
        if (flags is null || !flags.RootElement.TryGetProperty(name, out var flag)
            || !flag.TryGetProperty("params", out var p) || !p.TryGetProperty(key, out var v))
            return defaultValue;
        return v.ValueKind switch
        {
            JsonValueKind.Number when v.TryGetInt32(out var i) => i,
            JsonValueKind.Number => (int)v.GetDouble(),
            JsonValueKind.String when int.TryParse(v.GetString(), out var s) => s,
            _ => defaultValue,
        };
    }

    public void Dispose()
    {
        _timer.Dispose();
        _http.Dispose();
        _flags?.Dispose();
    }
}
