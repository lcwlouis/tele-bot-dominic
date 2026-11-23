"""Keep a process group tied to its supervisor, even during blocking renders."""

import os
import signal
import subprocess
import sys
import time


def main():
    parent = int(sys.argv[1])
    if os.getppid() != parent:
        return 1

    # Inherit stdin/stdout and the session created by the supervisor.
    child = subprocess.Popen(sys.argv[2:])
    while os.getppid() == parent:
        status = child.poll()
        if status is not None:
            return status if status >= 0 else 128 - status
        time.sleep(0.1)

    # This also kills any render or encoder subprocesses and the guard itself.
    os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    sys.exit(main())
