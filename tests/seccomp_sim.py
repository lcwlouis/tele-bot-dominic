"""A tiny classic-BPF interpreter for seccomp filters (the subset the plugin runner emits)."""

import struct

ARCH = {"aarch64": 0xC00000B7, "x86_64": 0xC000003E, "armv7l": 0x40000028}
RET_ALLOW = 0x7FFF0000
RET_KILL = 0x80000000
RET_ERRNO = 0x00050000


def parse(program: bytes) -> list[tuple[int, int, int, int]]:
    assert len(program) % 8 == 0
    return [struct.unpack("=HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]


def run(program: bytes, machine: str, nr: int, args: tuple[int, ...] = ()) -> int:
    """Return the seccomp action for one syscall on the given architecture."""
    data = struct.pack("=IIQ6Q", nr, ARCH[machine], 0, *(args + (0,) * 6)[:6])
    instructions = parse(program)
    accumulator, pc = 0, 0
    for _ in range(4096):
        code, jt, jf, k = instructions[pc]
        pc += 1
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            accumulator = struct.unpack_from("=I", data, k)[0]
        elif code == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += jt if accumulator == k else jf
        elif code == 0x45:  # BPF_JMP | BPF_JSET | BPF_K
            pc += jt if accumulator & k else jf
        elif code == 0x06:  # BPF_RET | BPF_K
            return k
        else:
            raise AssertionError(f"unsupported BPF opcode {code:#x}")
    raise AssertionError("filter did not terminate")


def errno_of(action: int) -> int | None:
    return action & 0xFFFF if action & 0xFFFF0000 == RET_ERRNO else None
