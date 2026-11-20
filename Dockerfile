# Built on linux/amd64 and run with no network. The digest is the image every
# commit was labeled in: its Python and SQLite decide which sqlglot tests run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit, straight from its own repository.
RUN python -c "import io, tarfile, urllib.request; \
data = urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz', timeout=120).read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /sqlglot

# The exact packages the commits were labeled with, so tests behave the same here.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock

COPY selector /app/selector
COPY maps /app/maps
# The run has a read-only disk and runs as `nobody`: compile now, readable by all.
RUN python -m compileall -q /sqlglot /app && chmod -R a+rX /sqlglot /app

ENV PYTHONPATH=/app SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
CMD ["python", "-m", "selector.main"]
