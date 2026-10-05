
import argparse
import errno
import os
import stat
import sys
import threading

try:
    from mfusepy import FUSE, FuseOSError, Operations
except ImportError:
    from fuse import FUSE, FuseOSError, Operations

from fatx import BlockDevice, FatxVolume, SplitFileDevice


class FatxFS(Operations):
    use_ns = True

    def __init__(self, vol: FatxVolume):
        self.vol = vol
        self.rw = vol.writable

    def init(self, path):
        print("MOUNTED", flush=True)

    def destroy(self, path):
        self.vol.sync()

    def _entry(self, path):
        e = self.vol.resolve(path)
        if e is None:
            raise FuseOSError(errno.ENOENT)
        return e

    def getattr(self, path, fh=None):
        e = self._entry(path)
        if e.is_dir:
            mode, nlink, size = stat.S_IFDIR | (0o777 if self.rw else 0o555), 2, 0
        else:
            mode, nlink, size = stat.S_IFREG | (0o666 if self.rw else 0o444), 1, e.size
        mtime = e.modified
        return {
            "st_mode": mode, "st_nlink": nlink, "st_size": size,
            "st_mtime": int(mtime * 1e9), "st_ctime": int((e.created or mtime) * 1e9),
            "st_atime": int((e.accessed or mtime) * 1e9),
        }

    def readdir(self, path, fh):
        e = self._entry(path)
        if not e.is_dir:
            raise FuseOSError(errno.ENOTDIR)
        yield "."
        yield ".."
        for child in self.vol.list_dir(e.first_cluster):
            yield child.name

    def open(self, path, flags):
        if not self.rw and flags & (os.O_WRONLY | os.O_RDWR):
            raise FuseOSError(errno.EROFS)
        if self._entry(path).is_dir:
            raise FuseOSError(errno.EISDIR)
        return 0

    def read(self, path, size, offset, fh):
        return self.vol.read_file(self._entry(path), offset, size)

    def statfs(self, path):
        free = self.vol.free_clusters or 0
        return {
            "f_bsize": self.vol.cluster_size, "f_frsize": self.vol.cluster_size,
            "f_blocks": self.vol.max_cluster, "f_bfree": free, "f_bavail": free,
            "f_namemax": 42,
        }

    def create(self, path, mode, fi=None):
        self.vol.create(path)
        return 0

    def mkdir(self, path, mode):
        self.vol.mkdir(path)

    def unlink(self, path):
        self.vol.unlink(path)

    def rmdir(self, path):
        self.vol.rmdir(path)

    def rename(self, old, new):
        self.vol.rename(old, new)

    def truncate(self, path, length, fh=None):
        self.vol.truncate(path, length)

    def write(self, path, data, offset, fh):
        return self.vol.write_file(path, bytes(data), offset)

    def utimens(self, path, times=None):
        atime, mtime = (t / 1e9 for t in times) if times else (None, None)
        self.vol.set_times(path, atime, mtime)

    def chmod(self, path, mode):
        return 0

    def chown(self, path, uid, gid):
        return 0

    def release(self, path, fh):
        self.vol.finish_file(path)
        return 0

    def fsync(self, path, datasync, fh):
        self.vol.sync()
        return 0


def shutdown(vol: FatxVolume, mountpoint: str, code: int = 0):
    """Flush and exit. Holding the lock means no operation is cut in half.
    On Windows, WinFsp removes the drive letter as soon as this process exits."""
    with vol.lock:
        try:
            vol.sync()
            print("UNMOUNTED", flush=True)
            if sys.platform != "win32":
                import subprocess
                subprocess.run(["fusermount", "-u", "-z", os.path.abspath(mountpoint)],
                               capture_output=True)
        finally:
            os._exit(code)


def watch_stdin(vol: FatxVolume, mountpoint: str):
    for line in sys.stdin:
        if line.strip().upper() == "UNMOUNT":
            break
    shutdown(vol, mountpoint)


def attach_std_streams():
    """In a windowed .exe, sys.stdin/stdout are None even when the app handed us pipes.
    Re-open them from the inherited Windows handles so status messages still flow."""
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt
    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = ctypes.c_void_p
    invalid = ctypes.c_void_p(-1).value
    for std_id, name, flags, mode in ((-10, "stdin", os.O_RDONLY, "r"),
                                      (-11, "stdout", os.O_WRONLY, "w")):
        if getattr(sys, name) is not None:
            continue
        handle = k32.GetStdHandle(std_id)
        if not handle or handle == invalid:
            continue
        try:
            fd = msvcrt.open_osfhandle(handle, flags)
            setattr(sys, name, open(fd, mode, buffering=1, encoding="utf-8", errors="replace"))
        except OSError:
            pass
    if sys.stderr is None:
        sys.stderr = sys.stdout


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mount an Xbox 360 FATX partition")
    ap.add_argument("--source", required=True, help=r"\\.\PhysicalDriveN or an image file")
    ap.add_argument("--offset", default="0", help="partition offset in bytes (hex ok)")
    ap.add_argument("--size", default="0", help="partition size in bytes (0 = to end)")
    ap.add_argument("--mount", required=True, help=r"drive letter like X: (or \\.\X: for a global drive)")
    ap.add_argument("--label", default="Xbox 360")
    ap.add_argument("--readonly", action="store_true", help="mount read-only")
    ap.add_argument("--stdin-control", action="store_true",
                    help="unmount when 'UNMOUNT' is read from stdin or stdin closes (used by app.py)")
    args = ap.parse_args(argv)

    usb = not args.source.startswith("\\\\.\\") and os.path.isdir(args.source)
    dev = (SplitFileDevice if usb else BlockDevice)(args.source, writable=not args.readonly)
    vol = FatxVolume(dev, int(args.offset, 0), int(args.size, 0) or None, args.label, usb=usb)
    print(f"Opened {args.label}: {vol.max_cluster} clusters of {vol.cluster_size} bytes "
          f"({'read-write' if vol.writable else 'read-only'})", flush=True)
    print("Counting free space…", flush=True)
    vol.count_free_clusters()

    if args.stdin_control and sys.stdin is not None:
        threading.Thread(target=watch_stdin, args=(vol, args.mount), daemon=True).start()

    opts = dict(uid=-1, gid=-1, volname=args.label.replace(",", " ")) if sys.platform == "win32" else {}
    try:
        FUSE(FatxFS(vol), args.mount, foreground=True, **opts)
    finally:
        vol.sync()
        dev.close()


def run_worker(argv=None) -> int:
    attach_std_streams()
    try:
        main(argv)
        return 0
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(run_worker())
