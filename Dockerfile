FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cmdb ./cmdb
COPY schemas ./schemas
COPY data ./data
COPY tests ./tests
COPY pytest.ini .

RUN useradd --system --uid 10001 appuser
USER appuser

EXPOSE 8080
ENTRYPOINT ["python", "-m", "cmdb"]
CMD ["--help"]
