FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    STAGE1_ARCHIVE_PATH=/var/lib/nexus-mobile/stage1/runs

WORKDIR /opt/nexus-mobile
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
RUN pip install --no-cache-dir -r requirements.lock \
  && pip install --no-cache-dir --no-deps .

RUN mkdir -p /var/lib/nexus-mobile/stage1/runs \
   && mkdir -p /var/lib/nexus-mobile/auth \
    && useradd --system --uid 10001 --create-home nexus \
    && chown -R nexus:nexus /var/lib/nexus-mobile \
   && chmod 700 /var/lib/nexus-mobile/auth
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os; from urllib.request import urlopen; urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/mobile', timeout=3).read(1)" || exit 1
CMD ["python", "-m", "app"]
