FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/apps/add_backend:/app/packages/common

RUN addgroup --system add && adduser --system --ingroup add add
WORKDIR /app

COPY apps/add_backend/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY packages/common ./packages/common
COPY apps/add_backend ./apps/add_backend

USER add
EXPOSE 8096

CMD ["uvicorn", "zk_add.web:app", "--host", "0.0.0.0", "--port", "8096", "--proxy-headers", "--forwarded-allow-ips", "*"]
