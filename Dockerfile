FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY royalbooks ./royalbooks

# NAS checkouts may preserve restrictive source permissions (e.g. 0600/0700).
# The service intentionally runs as UID 10001, so make packaged Python code and
# its parent directories readable/traversable without making the code writable
# by the service user. Never chmod bind-mounted /data or CWA library contents.
RUN pip install --no-cache-dir . \
    && chmod 755 /app \
    && chmod 644 /app/pyproject.toml \
    && find /app/royalbooks -type d -exec chmod 755 {} + \
    && find /app/royalbooks -type f -exec chmod 644 {} + \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8094
CMD ["uvicorn", "royalbooks.app:app", "--host", "0.0.0.0", "--port", "8094"]
