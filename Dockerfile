FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cmdb ./cmdb
COPY schemas ./schemas
COPY data ./data
COPY tests ./tests
COPY pytest.ini .

# `cmdb <command>` from any directory inside the container (same as `python -m cmdb`)
RUN printf '#!/bin/sh\nexec python -m cmdb "$@"\n' > /usr/local/bin/cmdb && chmod +x /usr/local/bin/cmdb

RUN useradd --system --uid 10001 appuser
USER appuser

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"

# Default: run the enrichment API. Any other command runs once, e.g.
#   docker run --rm -it --env-file .env observability-cmdb plan
ENTRYPOINT ["python", "-m", "cmdb"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
