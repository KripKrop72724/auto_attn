"""Prove absolute deadlines through faults and actual blocking socket pairs."""
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("real_sockets", [False, True])
def test_socket_deadlines_do_not_restart_on_partial_io_or_readiness_races(tmp_path, real_sockets):
    main = ROOT / "firmware/zone_lite/main"
    fixture = ROOT / "tests/firmware"
    executable = tmp_path / "socket-io"
    defines = []
    if not real_sockets:
        # Include libc declarations before replacing calls: glibc's fortified
        # recv inline otherwise redirects the renamed mock back to real recv.
        ports = tmp_path / "socket_fault_ports.h"
        ports.write_text("""#include <sys/select.h>
#include <sys/socket.h>
int fault_select(int, fd_set *, fd_set *, fd_set *, struct timeval *);
ssize_t fault_recv(int, void *, size_t, int);
ssize_t fault_send(int, const void *, size_t, int);
#define select fault_select
#define recv fault_recv
#define send fault_send
""")
        defines = ["-include", str(ports)]
    subprocess.run([shutil.which("cc"), "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-D_DARWIN_C_SOURCE", *defines,
                    "-g", "-O1", "-Wall", "-Wextra", "-Werror", "-pthread",
                    "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-I", str(main),
                    str(fixture / ("zkt_socket_io_real_host.c" if real_sockets else "zkt_socket_io_host.c")),
                    str(main / "zkt_socket_io.c"), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True, timeout=30)
