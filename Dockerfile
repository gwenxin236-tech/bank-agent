FROM python:3.12-slim

ARG INSTALL_PRODUCTION_EXTRAS=false
ARG INSTALL_LOCAL_RAG=false
ARG INSTALL_LOCAL_OCR=false
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BANK_AGENT_HOST=0.0.0.0 \
    BANK_AGENT_PORT=8090

RUN groupadd --system agent && useradd --system --gid agent --home-dir /app agent
WORKDIR /app

COPY requirements.txt requirements-production.txt requirements-rag-local.txt requirements-ocr-local.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_PRODUCTION_EXTRAS" = "true" ]; then pip install --no-cache-dir -r requirements-production.txt; fi

COPY . /app
RUN if [ "$INSTALL_LOCAL_RAG" = "true" ]; then \
      pip install --no-cache-dir --no-index --find-links=/app/rag-wheels --require-hashes -r /app/rag-wheels/requirements.lock; \
    fi
RUN if [ "$INSTALL_LOCAL_OCR" = "true" ]; then \
      pip install --no-cache-dir --no-index --find-links=/app/ocr-wheels --require-hashes -r /app/ocr-wheels/requirements.lock; \
    fi
RUN mkdir -p /app/data && chown -R agent:agent /app
USER agent

EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health/live', timeout=2)"

CMD ["python", "app/banking_agent.py"]
