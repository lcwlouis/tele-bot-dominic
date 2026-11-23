"""Create a new studio token file without overwriting an existing token."""

import argparse
import os
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    descriptor = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(secrets.token_urlsafe(48) + "\n")


if __name__ == "__main__":
    main()
