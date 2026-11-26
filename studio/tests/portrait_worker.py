"""An image worker that records its requests and draws a PNG header, or fails for a prompt that names 'broken'."""
import json
from pathlib import Path
import sys

print('{"event":"ready"}', flush=True)
for line in sys.stdin:
    request = json.loads(line)
    log = Path(request["output"]).parent / "requests.jsonl"
    with log.open("a") as out:
        out.write(json.dumps(request) + "\n")
    if "broken" in request["prompt"]:
        sys.exit(1)
    Path(request["output"]).write_bytes(b"\x89PNG\r\n\x1a\n" + request["prompt"].encode())
    print('{"event":"done"}', flush=True)
