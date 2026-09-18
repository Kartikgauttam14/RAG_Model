from prometheus_client import Counter, Histogram

REQUEST_COUNT = Counter("rag_http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("rag_http_request_duration_seconds", "HTTP request latency", ["method", "path"])
PROVIDER_FAILURES = Counter("rag_provider_failures_total", "External provider failures", ["provider", "operation"])
