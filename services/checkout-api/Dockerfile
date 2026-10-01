FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download 2>/dev/null || true
COPY . .
RUN CGO_ENABLED=0 GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w" -o /checkout-api .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /checkout-api /checkout-api
EXPOSE 8080
ENTRYPOINT ["/checkout-api"]
