FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY . .
EXPOSE 8083
# Zero-code instrumentation: the Splunk distro is preloaded with -r.
CMD ["node", "-r", "@splunk/otel/instrument", "server.js"]
