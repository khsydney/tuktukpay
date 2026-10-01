# ledger (.NET 8) with the Splunk Distribution of OpenTelemetry .NET auto-instrumentation.
# Enable with:  docker compose --profile dotnet up -d   (see docker-compose.yml)
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY Ledger.csproj .
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Splunk OpenTelemetry .NET: download + install the auto-instrumentation.
RUN curl -sSfL https://github.com/signalfx/splunk-otel-dotnet/releases/latest/download/splunk-otel-dotnet-install.sh -O \
 && sh ./splunk-otel-dotnet-install.sh \
 && rm splunk-otel-dotnet-install.sh
COPY --from=build /app .
EXPOSE 8084
# instrument.sh exports CORECLR_* / DOTNET_STARTUP_HOOKS / OTEL_DOTNET_AUTO_* so the
# runtime loads the instrumentation — zero code changes in the application.
ENTRYPOINT ["sh", "-c", ". $HOME/.splunk-otel-dotnet/instrument.sh && dotnet Ledger.dll"]
