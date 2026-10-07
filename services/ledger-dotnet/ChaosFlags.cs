using System.Text.Json;

namespace TukTukPay.Ledger;

/// <summary>
/// Polls the workshop chaos-controller every 2s and caches the flags. Transitions of the
/// flags this service consumes (<c>watch</c>) are logged as config.changed events — the
/// "feature flag flipped at 10:02" breadcrumb an AI root-cause analysis latches onto.
/// </summary>
public sealed class ChaosFlags : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromMilliseconds(1500) };
    private readonly string _url;
    private readonly Timer _timer;
    private readonly ILogger? _log;
    private readonly HashSet<string> _watch;
    private readonly Dictionary<string, string> _known = new();
    private JsonDocument? _flags;

    public ChaosFlags(string baseUrl, ILogger? log = null, IEnumerable<string>? watch = null)
    {
        _url = baseUrl.TrimEnd('/') + "/flags";
        _log = log;
        _watch = new HashSet<string>(watch ?? Array.Empty<string>());
        _timer = new Timer(async _ => await PollAsync(), null, TimeSpan.Zero, TimeSpan.FromSeconds(2));
    }

    private async Task PollAsync()
    {
        try
        {
            var json = await _http.GetStringAsync(_url);
            var doc = JsonDocument.Parse(json);
            Interlocked.Exchange(ref _flags, doc)?.Dispose();
            LogTransitions(doc);
        }
        catch
        {
            // keep the last good copy; never let chaos polling break the service
        }
    }

    private void LogTransitions(JsonDocument doc)
    {
        if (_log is null) return;
        foreach (var name in _watch)
        {
            var enabled = false;
            var parameters = "{}";
            if (doc.RootElement.TryGetProperty(name, out var flag))
            {
                enabled = flag.TryGetProperty("enabled", out var e) && e.ValueKind == JsonValueKind.True;
                if (flag.TryGetProperty("params", out var p)) parameters = p.GetRawText();
            }
            var current = $"{enabled}|{parameters}";
            var isNew = !_known.TryGetValue(name, out var previous);
            _known[name] = current;
            if (isNew || previous == current) continue;
            _log.LogInformation("{event}: feature flag {feature_flag.name} {feature_flag.state}: {feature_flag.params}",
                "config.changed", name, enabled ? "enabled" : "disabled", parameters);
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
