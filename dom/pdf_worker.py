"""Read the text of a PDF in a process of its own, with memory and CPU limits (dom/workspace.py starts it).

A PDF is untrusted input from a chat, and a malformed one can take unbounded time or memory in a parser. This script
runs as a separate, killable process: the parent sends the file on stdin, a wall-clock timeout kills it, and the limits
below stop a runaway earlier. It imports nothing from Dom, only the PDF library.

Usage: python pdf_worker.py <characters wanted>. Prints the text (at most that many characters, UTF-8) and exits 0.
Exit 3: no text; 4: password-protected; 5: could not be read.
"""

import sys

MEMORY_BYTES = 512 * 1024 * 1024
CPU_SECONDS = 20
MAX_PAGES = 300


def limit() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    except (ImportError, ValueError, OSError):
        pass  # not every platform enforces both; the parent's wall-clock kill still applies


def main() -> int:
    limit()
    wanted = max(1, int(sys.argv[1]))
    data = sys.stdin.buffer.read()
    from io import BytesIO

    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            return 4
        out, size = [], 0
        for page in reader.pages[:MAX_PAGES]:
            text = page.extract_text() or ""
            out.append(text)
            size += len(text) + 2
            if size > wanted:
                break
    except MemoryError:
        return 5
    except Exception:
        return 5
    text = "\n\n".join(out).strip()
    if not text:
        return 3
    sys.stdout.buffer.write(text[:wanted].encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
