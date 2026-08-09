FROM espressif/idf:v5.5.3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    IDF_PATH=/opt/esp/idf \
    PYTHONPATH=/app/firmware/zone_lite/tools

RUN groupadd --system provisioner && useradd --system --gid provisioner provisioner \
    && mkdir -p /artifacts && chown provisioner:provisioner /artifacts
WORKDIR /app
COPY apps/add_provisioner/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY apps/add_provisioner/app.py ./app.py
COPY firmware/zone_lite/tools ./firmware/zone_lite/tools

USER provisioner
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8097", "--no-access-log"]
