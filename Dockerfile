# Blast-radius selector. The base image digest is the one the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN pip install -r /tmp/requirements.lock
COPY tools/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN pip install --target /tmp/dw dulwich==1.2.21 || true
RUN PYTHONPATH=/tmp/dw python /tmp/fetch_sqlglot.py /opt/sq && rm -rf /tmp/dw && chmod -R a+rX /opt/sq
COPY app /app/app
COPY data /app/data
COPY tools/warm.py /tmp/warm.py
ENV PYTHONPYCACHEPREFIX=/opt/pyc
RUN python -m compileall -q /app/app && python /tmp/warm.py 2 && rm -rf /tmp/w0 /tmp/w1 && chmod -R a+rX /opt/pyc /app
ENV PYTHONDONTWRITEBYTECODE=1 BR_SRC=/opt/sq BR_MAP=/app/data/map.json.gz
CMD ["python", "/app/app/selector.py"]
