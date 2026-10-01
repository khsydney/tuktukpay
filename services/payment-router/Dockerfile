# payment-router: plain Java 21 (no framework, no build tool needed) + the
# Splunk Distribution of the OpenTelemetry Java agent for zero-code instrumentation.
FROM eclipse-temurin:21-jdk AS build
WORKDIR /src
COPY src ./src
RUN mkdir -p /out && javac -d /out $(find src -name "*.java") \
 && cd /out && jar cfe /payment-router.jar com.tuktukpay.router.RouterServer .

FROM eclipse-temurin:21-jre
WORKDIR /app
# Pin the agent version in production; "latest" keeps the workshop simple.
ADD https://github.com/signalfx/splunk-otel-java/releases/latest/download/splunk-otel-javaagent.jar /otel/splunk-otel-javaagent.jar
COPY --from=build /payment-router.jar /app/payment-router.jar
RUN chmod 644 /otel/splunk-otel-javaagent.jar
EXPOSE 8082
# The agent is attached via JAVA_TOOL_OPTIONS in docker-compose.yml so the same
# image runs with or without instrumentation ("before/after" demo).
CMD ["java", "-jar", "/app/payment-router.jar"]
