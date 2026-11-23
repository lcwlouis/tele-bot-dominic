"""Keep dependency logs away from the worker protocol, including native writes."""

import json
import os
import sys


def output_channel():
    output = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return output


def serve(output, render):
    def emit(event):
        print(json.dumps({"event": event}), file=output, flush=True)

    emit("ready")
    for row in sys.stdin:
        try:
            render(json.loads(row))
        except Exception:
            emit("error")
            raise SystemExit(1) from None
        emit("done")
