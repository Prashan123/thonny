import os
import io
import sys
import logging
from thonny.plugins.micropython.backend import MicroPythonBackend, EOT, ends_overlap, ENCODING
import textwrap
from thonny.common import BackendEvent, serialize_message
from thonny.plugins.micropython.connection import ConnectionFailedException
from thonny.plugins.micropython.bare_metal_backend import NORMAL_PROMPT, LF
import re
import shlex


FALLBACK_BUILTIN_MODULES = [
    "cmath",
    "gc",
    "math",
    "sys",
    "array",
    # "binascii", # don't include it, as it may give false signal for reader/writer
    "collections",
    "errno",
    "hashlib",
    "heapq",
    "io",
    "json",
    "os",
    "re",
    "select",
    "socket",
    "ssl",
    "struct",
    "time",
    "zlib",
    "_thread",
    "btree",
    "micropython",
    "cryptolib",
    "ctypes",
]

PASTE_MODE_CMD = b"\x05"
PASTE_MODE_LINE_PREFIX = b"=== "


class MicroPythonOsBackend(MicroPythonBackend):
    def __init__(self, mp_executable, api_stubs_path, cwd=None):
        try:
            self._mp_executable = self._resolve_executable(mp_executable)
            self._connection = self._create_connection()
        except ConnectionFailedException as e:
            text = "\n" + str(e) + "\n"
            msg = BackendEvent(event_type="ProgramOutput", stream_name="stderr", data=text)
            sys.stdout.write(serialize_message(msg) + "\n")
            sys.stdout.flush()
            return

        super().__init__(None, api_stubs_path, cwd=cwd)

    def _resolve_executable(self, executable):
        result = self._which(executable)
        if result:
            return result
        else:
            msg = "Executable '%s' not found. Please check your configuration!" % executable
            if not executable.startswith("/"):
                msg += " You may need to provide its absolute path."
            raise ConnectionFailedException(msg)

    def _which(self, executable):
        raise NotImplementedError()

    def _create_connection(self, run_args=[]):
        raise NotImplementedError()

    def _tweak_welcome_text(self, original):
        return (
            original.replace("Use Ctrl-D to exit, Ctrl-E for paste mode\n", "").strip()
            + " ("
            + self._mp_executable
            + ")\n"
        )

    def _get_custom_helpers(self):
        return textwrap.dedent(
            """
            if not hasattr(os, "getcwd") or not hasattr(os, "getcwd") or not hasattr(os, "rmdir"):
                # https://github.com/pfalcon/pycopy-lib/blob/master/os/os/__init__.py
                
                import ffi
                
                libc = ffi.open(
                    "libc.so.6" if sys.platform == "linux" else "libc.dylib"
                )
                
                @classmethod
                def check_error(cls, ret):
                    if ret == -1:
                        raise OSError(cls.os.errno())
                
                _getcwd = libc.func("s", "getcwd", "si")
                @classmethod
                def getcwd(cls):
                    buf = bytearray(512)
                    return cls._getcwd(buf, 512)

                _chdir = libc.func("i", "chdir", "s")
                @classmethod
                def chdir(cls, dir):
                    r = cls._chdir(dir)
                    cls.check_error(r)
                
                _rmdir = libc.func("i", "rmdir", "s")
                @classmethod
                def rmdir(cls, name):
                    e = cls._rmdir(name)
                    cls.check_error(e)                                    
                """
        )

    def _process_until_initial_prompt(self, clean):
        output = []

        def collect_output(data, stream_name):
            output.append(data)

        self._report_time("befini")
        self._forward_output_until_active_prompt(collect_output, "stdout")
        self._original_welcome_text = b"".join(output).decode(ENCODING).replace("\r\n", "\n")
        self._welcome_text = self._tweak_welcome_text(self._original_welcome_text)
        self._report_time("afini")

    def _fetch_builtin_modules(self):
        return FALLBACK_BUILTIN_MODULES

    def _soft_reboot(self, side_command):
        raise NotImplementedError()

    def _execute_with_consumer(self, script, output_consumer):
        """Ensures prompt and submits the script.
        Returns (out, value_repr, err) if there are no problems, ie. all parts of the 
        output are present and it reaches active prompt.
        Otherwise raises ProtocolError.
        
        The execution may block. In this case the user should do something (eg. provide
        required input or issue an interrupt). The UI should remind the interrupt in case
        of Thonny commands.
        """
        self._connection.write(PASTE_MODE_CMD)
        self._connection.read_until(PASTE_MODE_LINE_PREFIX)
        self._connection.write(script + "#uuu")
        self._connection.read_until(b"#uuu")
        self._connection.write(EOT)
        self._connection.read_until(b"\n")

        out = self._connection.read_until(NORMAL_PROMPT)[: -len(NORMAL_PROMPT)]
        output_consumer(out, "stdout")

    def _forward_output_until_active_prompt(self, output_consumer, stream_name="stdout"):
        INCREMENTAL_OUTPUT_BLOCK_CLOSERS = re.compile(
            b"|".join(map(re.escape, [LF, NORMAL_PROMPT]))
        )

        pending = b""
        while True:
            # There may be an input submission waiting
            # and we can't progress without resolving it first
            self._check_for_side_commands()

            # Prefer whole lines, but allow also incremental output to single line
            new_data = self._connection.soft_read_until(
                INCREMENTAL_OUTPUT_BLOCK_CLOSERS, timeout=0.05
            )
            if not new_data:
                continue

            pending += new_data

            if pending.endswith(LF):
                output_consumer(pending, stream_name)
                pending = b""

            elif pending.endswith(NORMAL_PROMPT):
                out = pending[: -len(NORMAL_PROMPT)]
                output_consumer(out, stream_name)
                return NORMAL_PROMPT

            elif ends_overlap(pending, NORMAL_PROMPT):
                # Maybe we have a prefix of the prompt and the rest is still coming?
                follow_up = self._connection.soft_read(1, timeout=0.1)
                if not follow_up:
                    # most likely not a Python prompt, let's forget about it
                    output_consumer(pending, stream_name)
                    pending = b""
                else:
                    # Let's withhold this for now
                    pending += follow_up

            else:
                # No prompt in sight.
                # Output and keep working.
                output_consumer(pending, stream_name)
                pending = b""

    def _forward_unexpected_output(self, stream_name="stdout"):
        "Invoked between commands"
        data = self._connection.read_all()
        if data.endswith(NORMAL_PROMPT):
            self._send_output(data[: -len(NORMAL_PROMPT)], "stdout")
        elif data:
            self._send_output(data, "stdout")

    def _write(self, data):
        self._connection.write(data)

    def _cmd_Run(self, cmd):
        self._connection.close()
        self._report_time("befconn")
        args = cmd.args[:]
        if cmd.source and args[0] == "-c":
            if len(args) > 1:
                self._send_error_message(
                    "Warning: MicroPython doesn't allow program arguments (%s) together with '-c'"
                    % " ".join(map(shlex.quote, args[1:]))
                )
            args = ["-c", cmd.source]

        self._connection = self._create_connection(args)
        self._report_time("afconn")
        self._forward_output_until_active_prompt(self._send_output, "stdout")
        self._report_time("afforv")
        self.send_message(
            BackendEvent(event_type="HideTrailingOutput", text=self._original_welcome_text)
        )
        self._report_time("beffhelp")
        self._prepare_helpers()
        self._update_cwd()
        self._report_time("affhelp")

    def _cmd_execute_system_command(self, cmd):
        assert cmd.cmd_line.startswith("!")
        cmd_line = cmd.cmd_line[1:]
        # "or None" in order to avoid MP repl to print its value
        self._execute("__thonny_helper.os.system(%r) or None" % cmd_line)

    def _cmd_get_fs_info(self, cmd):
        script = """__thonny_helper.os.system("stat -f -c '%b %f %a %S' {path}") or None""".format(
            path=cmd.path
        )
        out, err = self._execute(script, capture_output=True)

        try:
            total, free, available, block_size = map(int, out.strip().split())
            return {
                "total": total * block_size,
                "free": available * block_size,
                "used": (total - free) * block_size,
            }
        except Exception:
            self._handle_bad_output(script, out, err)
            raise

    def _is_connected(self):
        return not self._connection._error


class MicroPythonLocalBackend(MicroPythonOsBackend):
    def _create_connection(self, run_args=[]):
        from thonny.plugins.micropython.subprocess_connection import SubprocessConnection

        return SubprocessConnection(self._mp_executable, ["-i"] + run_args)

    def _which(self, executable):
        import shutil

        return shutil.which(executable)

    def _cmd_cd(self, cmd):
        result = super()._cmd_cd(cmd)
        os.chdir(self._cwd)
        return result


class MicroPythonSshBackend(MicroPythonOsBackend):
    def __init__(self, host, user, password, cwd, mp_executable, api_stubs_path):
        from paramiko.client import SSHClient

        self._host = host
        self._user = user
        self._password = password
        self._sftp = None
        self._client = SSHClient()
        self._client.load_system_host_keys()
        # TODO: does it get closed properly after process gets killed?
        self._client.connect(hostname=host, username=user, password=password)

        self._cwd = cwd
        super().__init__(mp_executable, api_stubs_path, cwd=cwd)

    def _which(self, executable):
        cmd_str = " ".join(map(shlex.quote, ["which", executable]))
        _, stdout, _ = self._client.exec_command(cmd_str, bufsize=0, timeout=3, get_pty=False)
        return stdout.readline().strip() or None

    def _create_connection(self, run_args=[]):
        # NB! It's connection to the micropython process, not to the host
        from thonny.plugins.micropython.ssh_connection import SshProcessConnection

        return SshProcessConnection(self._client, self._cwd, self._mp_executable, ["-i"] + run_args)

    def _tweak_welcome_text(self, original):
        return (
            super()._tweak_welcome_text(original).strip()
            + "\n"
            + self._user
            + "@"
            + self._host
            + "\n"
        )

    def _get_sftp(self):
        if self._sftp is None:
            import paramiko

            # TODO: does it get closed properly after process gets killed?
            self._sftp = paramiko.SFTPClient.from_transport(self._client.get_transport())

        return self._sftp

    def _cmd_write_file(self, cmd):
        with io.BytesIO(cmd["content_bytes"]) as fp:
            self._get_sftp().putfo(fp, cmd["path"])

        return {"path": cmd["path"], "editor_id": cmd.get("editor_id")}

    def _cmd_read_file(self, cmd):
        try:
            with io.BytesIO() as fp:
                self._get_sftp().getfo(cmd["path"], fp)
                fp.seek(0)
                content_bytes = fp.read()
            error = None
        except Exception as e:
            # TODO: _report_internal_error()
            error = str(e)
            content_bytes = None

        return {"content_bytes": content_bytes, "path": cmd["path"], "error": error}

    def _upload_file(self, source, target, notifier=None):
        self._move_data_via_sftp(self._get_sftp().put, source, target, notifier)

    def _download_file(self, source, target, notifier=None):
        self._move_data_via_sftp(self._get_sftp().get, source, target, notifier)

    def _move_data_via_sftp(self, op, source, target, notifier):
        if notifier is None:
            callback = None
        else:

            def callback(sent, total):
                notifier(sent)

        op(source, target, callback=callback)


if __name__ == "__main__":
    THONNY_USER_DIR = os.environ["THONNY_USER_DIR"]
    logger = logging.getLogger("thonny.micropython.backend")
    logger.propagate = False
    logFormatter = logging.Formatter("%(levelname)s: %(message)s")
    file_handler = logging.FileHandler(
        os.path.join(THONNY_USER_DIR, "micropython-backend.log"), encoding="UTF-8", mode="w"
    )
    file_handler.setFormatter(logFormatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str)
    parser.add_argument("--user", type=str)
    parser.add_argument("--password", type=str)
    parser.add_argument("--executable", type=str)
    parser.add_argument("--cwd", type=str)
    parser.add_argument("--api_stubs_path", type=str)
    args = parser.parse_args()

    if args.host:
        vm = MicroPythonSshBackend(
            args.host,
            args.user,
            args.password,
            args.cwd or None,
            args.executable,
            args.api_stubs_path,
        )
    else:
        vm = MicroPythonLocalBackend(args.executable, api_stubs_path=args.api_stubs_path)
