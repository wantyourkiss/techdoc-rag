ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TECHDOC_DATA_DIR=/data
WORKDIR /app
COPY requirements-minimal.txt requirements-dense.txt ./
RUN pip install --no-cache-dir -r requirements-minimal.txt && \
    groupadd --gid 10001 techdoc && useradd --uid 10001 --gid 10001 --no-create-home techdoc && \
    mkdir /data && chown 10001:10001 /data
COPY techdoc ./techdoc
COPY scripts ./scripts
USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "techdoc.api:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]

FROM base AS dense
USER root
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements-dense.txt
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
USER 10001:10001

FROM base AS runtime
