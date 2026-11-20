"""Check patchlib against `git apply` on the sample patches and synthetic edge cases."""
import os, subprocess, sys, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from selector import patchlib

sqlglot, patches = sys.argv[1], sys.argv[2:]
bad = 0
for path in patches:
    text = open(path, encoding="utf-8", newline="").read()
    files = patchlib.parse(text)
    work = tempfile.mkdtemp()
    try:
        for f in files:
            src = os.path.join(sqlglot, f.old_path) if f.old_path else None
            old = open(src, encoding="utf-8", newline="").read() if src and os.path.exists(src) and not f.new_file else None
            patchlib.apply(f, old)
            if old is not None:
                os.makedirs(os.path.dirname(os.path.join(work, f.old_path)), exist_ok=True)
                open(os.path.join(work, f.old_path), "w", encoding="utf-8", newline="").write(old)
        subprocess.run(["git", "init", "-q", work], check=True)
        r = subprocess.run(["git", "-C", work, "apply", "--whitespace=nowarn", os.path.abspath(path)], capture_output=True, text=True)
        for f in files:
            target = os.path.join(work, f.path)
            want = open(target, encoding="utf-8", newline="").read() if os.path.exists(target) else None
            if f.deleted:
                want = None
            if f.unapplied or want != f.new_text or r.returncode:
                bad += 1
                print("MISMATCH", path, f.path, "unapplied" if f.unapplied else "", r.stderr[:200])
    finally:
        shutil.rmtree(work, ignore_errors=True)
print("checked", len(patches), "patches, mismatches:", bad)
