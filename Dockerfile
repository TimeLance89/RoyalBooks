FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY royalbooks ./royalbooks
RUN pip install --no-cache-dir . && mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8094
CMD ["uvicorn", "royalbooks.app:app", "--host", "0.0.0.0", "--port", "8094"]
