# Hosted mode. Each request brings its own bearer; the image holds no credential.
#   docker run --rm -p 8000:8000 phone-harness-mcp --port 8000 --host 0.0.0.0 --allowed-hosts mcp.example.com
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 mcp
USER mcp
EXPOSE 8000
ENTRYPOINT ["phone-harness-mcp"]
CMD ["--port", "8000", "--host", "0.0.0.0"]
