import json
from pathlib import Path
import sys
import time

kind = sys.argv[1]
print('{"event":"ready"}', flush=True)
for line in sys.stdin:
    request = json.loads(line)
    text = request.get("text")
    if text == "slow":
        time.sleep(60)
    if text == "crash":
        sys.exit(1)
    if kind == "image":
        assert request["size"] == 768
        assert request["portrait"].endswith("portrait.png")
        assert request["turnaround"].endswith("turnaround.png")
        assert "Keep the same outfit" in request["prompt"]
    if kind in ("video", "special"):
        if request["portrait"].endswith("still.png"):
            assert Path(request["portrait"]).read_bytes().startswith(b"\x89PNG")
        else:
            assert request["portrait"].endswith("portrait.png")
        assert Path(request["audio"]).read_bytes() == b"fake"
    Path(request["output"]).write_bytes(b"fake")
    print('{"event":"done"}', flush=True)
