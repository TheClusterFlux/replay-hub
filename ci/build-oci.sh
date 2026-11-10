#!/usr/bin/env bash
set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# The trusted preparer hands off only the reviewed dependency image without registry credentials.
test "$(git hash-object requirements.txt)" = 1192fd17fd8b133d329ebf9d53c4540abc835a2f
test "$(git hash-object Dockerfile)" = 26bee1f9d4d536b3f4e0b0ab07210371823bc022
layout="ocidir://$work/image"
regctl image import "$layout:base" /handoff-input/replay-runtime.tar
test "$(regctl image digest "$layout:base")" = "$(cat /handoff-input/replay-runtime.digest)"
mkdir -p "$work/layer/app/uploads"
cp -a app main.py entrypoint.sh "$work/layer/app/"
chmod 755 "$work/layer/app/entrypoint.sh"
python3 - <<'PY'
import ast, os
from pathlib import Path
for source in [Path("main.py"), *Path("app").glob("*.py")]:
    ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
os.environ.update(MONGODB_URI="mongodb://test", MONGO_DB_NAME="restored")
import runpy
c = runpy.run_path("app/config.py")
assert c["MONGO_URI"] == "mongodb://test" and c["MONGO_DB_NAME"] == "restored"
assert c["VIDEO_ENCODING_STRATEGY"] == "CLIENT"
PY
sha256sum main.py | cut -d' ' -f1 > "$OUTPUT_DIR/page.sha256"
tar -C "$work/layer" --owner=1000 --group=1000 --numeric-owner -cf "$work/app.tar" app
regctl image mod "$layout:base" --create "$layout:release" --layer-add "tar=$work/app.tar" \
  --label "org.opencontainers.image.revision=$SOURCE_SHA" --label "org.opencontainers.image.source=https://github.com/TheClusterFlux/replay-hub"
regctl image digest "$layout:release" > "$OUTPUT_DIR/image.digest"
regctl image export "$layout:release" "$OUTPUT_DIR/image.tar"
