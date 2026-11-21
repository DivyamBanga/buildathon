"""Download sqlglot at one commit from its own repository and unpack it.

    python fetch_sqlglot.py <commit> <destination>

Used by the Dockerfile (the base image has no git). Retries a few times so a
dropped connection does not fail the build.
"""
import io
import os
import sys
import tarfile
import time
import urllib.request

commit, destination = sys.argv[1], sys.argv[2]
url = f"https://github.com/tobymao/sqlglot/archive/{commit}.tar.gz"
data = None
for attempt in range(5):
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        break
    except Exception as error:  # noqa: BLE001 - any network failure is retried
        print(f"attempt {attempt + 1} failed: {error!r}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
if data is None:
    raise SystemExit(f"could not download {url}")
parent = os.path.dirname(destination.rstrip("/")) or "/"
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    archive.extractall(parent, filter="data")
os.rename(os.path.join(parent, f"sqlglot-{commit}"), destination)
print(f"sqlglot {commit} unpacked to {destination}")
