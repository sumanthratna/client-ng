# -*- coding: utf-8 -*-
"""
internal.
"""

from __future__ import print_function

import atexit
import logging
import multiprocessing
import os
import platform
import sys
import threading
import time

import psutil  # type: ignore
import six
from six.moves import queue
import wandb
from wandb.interface import constants
from wandb.internal import datastore
from wandb.internal import sender
from wandb.util import sentry_exc

# from wandb.stuff import io_wrap

from . import meta
from . import settings_static
from . import stats
from . import update


logger = logging.getLogger(__name__)


_exited = False


@atexit.register
def handle_exit(*args):
    global _exited
    if not _exited:
        _exited = True
        exc_type, exc_value, exc_traceback = sys.exc_info()
        if exc_traceback:
            logger.exception("Internal process exited with exception:")
        else:
            logger.info("Process exited cleanly")


# TODO: we may want this someday, but are avoiding it now to avoid conflicts
# signal.signal(signal.SIGTERM, handle_exit)
# signal.signal(signal.SIGINT, handle_exit)


def setup_logging(log_fname, log_level, run_id=None):
    # TODO: we may want make prints and stdout make it into the logs
    # sys.stdout = open(settings.log_internal, "a")
    # sys.stderr = open(settings.log_internal, "a")
    handler = logging.FileHandler(log_fname)
    handler.setLevel(log_level)

    class WBFilter(logging.Filter):
        def filter(self, record):
            record.run_id = run_id
            return True

    if run_id:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(threadName)-10s:%(process)d "
            "[%(run_id)s:%(filename)s:%(funcName)s():%(lineno)s] %(message)s"
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(threadName)-10s:%(process)d "
            "[%(filename)s:%(funcName)s():%(lineno)s] %(message)s"
        )

    handler.setFormatter(formatter)
    if run_id:
        handler.addFilter(WBFilter())
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


def wandb_stream_read(fd):
    # print("start reading", file=sys.stderr)
    while True:
        try:
            data = os.read(fd, 200)
        except OSError as e:
            sentry_exc(e)
            # print("problem", e, file=sys.stderr)
            return
        if len(data) == 0:
            break
        # print("got data:", data, file=sys.stderr)
    # print("done reading", file=sys.stderr)


def wandb_write(settings, q, stopped):
    ds = datastore.DataStore()
    ds.open_for_write(settings.sync_file)
    while not stopped.isSet():
        try:
            i = q.get(timeout=1)
        except queue.Empty:
            continue
        ds.write(i)
        # print("write", i)
    ds.close()


def wandb_read(settings, q, data_q, stopped):
    # ds = datastore.DataStore()
    # ds.open(data_filename)
    while not stopped.isSet():
        try:
            q.get(timeout=1)
        except queue.Empty:
            continue
        # ds.write(i)
        # print("write", i)
    # ds.close()


def wandb_send(
    settings,
    process_q,
    notify_q,
    q,
    resp_q,
    read_q,
    data_q,
    stopped,
    run_meta,
    system_stats,
):

    sh = sender.SendManager(
        settings, process_q, notify_q, resp_q, run_meta, system_stats
    )

    while not stopped.isSet():
        try:
            i = q.get(timeout=1)
        except queue.Empty:
            continue

        sh.send(i)

    sh.finish()


class WriteSerializingFile(object):
    """Wrapper for a file object that serializes writes.
    """

    def __init__(self, f):
        self.lock = threading.Lock()
        self.f = f

    def write(self, *args, **kargs):
        self.lock.acquire()
        try:
            self.f.write(*args, **kargs)
            self.f.flush()
        finally:
            self.lock.release()


def _get_stdout_stderr_streams():
    """Sets up STDOUT and STDERR streams. Only call this once."""
    if six.PY2 or not hasattr(sys.stdout, "buffer"):
        if hasattr(sys.stdout, "fileno") and sys.stdout.isatty():
            try:
                stdout = os.fdopen(sys.stdout.fileno(), "w+", 0)
                stderr = os.fdopen(sys.stderr.fileno(), "w+", 0)
            # OSError [Errno 22] Invalid argument wandb
            except OSError:
                stdout = sys.stdout
                stderr = sys.stderr
        else:
            stdout = sys.stdout
            stderr = sys.stderr
    else:  # we write binary so grab the raw I/O objects in python 3
        try:
            stdout = sys.stdout.buffer.raw
            stderr = sys.stderr.buffer.raw
        except AttributeError:
            # The testing environment and potentially others may have screwed with their
            # io so we fallback to raw stdout / err
            stdout = sys.stdout.buffer
            stderr = sys.stderr.buffer

    output_log_path = "output.txt"
    output_log = WriteSerializingFile(open(output_log_path, "wb"))

    stdout_streams = [stdout, output_log]
    stderr_streams = [stderr, output_log]

    return stdout_streams, stderr_streams


_check_process_last = None


def _check_process(settings, pid):
    global _check_process_last
    check_process_interval = settings._internal_check_process
    if not check_process_interval:
        return
    time_now = time.time()
    if _check_process_last and time_now < _check_process_last + check_process_interval:
        return
    _check_process_last = time_now

    exists = psutil.pid_exists(pid)
    if not exists:
        logger.warning("Internal process exiting, parent pid %s disappeared" % pid)
        # my_pid = os.getpid()
        # print("badness: process gone", pid, my_pid)
        handle_exit()
        os._exit(-1)


def wandb_internal(  # noqa: C901
    settings,
    notify_queue,
    process_queue,
    req_queue,
    resp_queue,
    cancel_queue,
    child_pipe,
    log_level,
    use_redirect,
):
    parent_pid = os.getppid()

    # mark this process as internal
    wandb._IS_INTERNAL_PROCESS = True

    # fd = multiprocessing.reduction.recv_handle(child_pipe)
    # if msvcrt:
    #    fd = msvcrt.open_osfhandle(fd, os.O_WRONLY)
    # os.write(fd, "this is a test".encode())
    # os.close(fd)

    # Lets make sure we dont modify settings so use a static object
    settings = settings_static.SettingsStatic(settings)
    if settings.log_internal:
        setup_logging(settings.log_internal, log_level)

    pid = os.getpid()

    logger.info("W&B internal server running at pid: %s", pid)

    system_stats = None
    if not settings._disable_stats and not settings.offline:
        system_stats = stats.SystemStats(
            pid=pid, process_q=process_queue, notify_q=notify_queue
        )
        system_stats.start()

    run_meta = None
    if not settings._disable_meta and not settings.offline:
        # We'll gather the meta now, but wait until we have a run to persist by wiring
        # this through to the sender.
        # If we try to persist now, there may not be a run yet, and we'll error out.
        run_meta = meta.Meta(
            settings=settings, process_q=process_queue, notify_q=notify_queue,
        )
        run_meta.probe()

    current_version = wandb.__version__
    update.check_available(current_version)

    if use_redirect:
        pass
    else:
        if platform.system() == "Windows":
            # import msvcrt
            # stdout_handle = multiprocessing.reduction.recv_handle(child_pipe)
            # stderr_handle = multiprocessing.reduction.recv_handle(child_pipe)
            # stdout_fd = msvcrt.open_osfhandle(stdout_handle, os.O_RDONLY)
            # stderr_fd = msvcrt.open_osfhandle(stderr_handle, os.O_RDONLY)

            # logger.info("windows stdout: %d", stdout_fd)
            # logger.info("windows stderr: %d", stderr_fd)

            # read_thread = threading.Thread(name="wandb_stream_read",
            #     target=wandb_stream_read, args=(stdout_fd,))
            # read_thread.start()
            # stdout_read_file = os.fdopen(stdout_fd, 'rb')
            # stderr_read_file = os.fdopen(stderr_fd, 'rb')
            # stdout_streams, stderr_streams = _get_stdout_stderr_streams()
            # stdout_tee = io_wrap.Tee(stdout_read_file, *stdout_streams)
            # stderr_tee = io_wrap.Tee(stderr_read_file, *stderr_streams)
            pass
        else:
            stdout_fd = multiprocessing.reduction.recv_handle(child_pipe)
            stderr_fd = multiprocessing.reduction.recv_handle(child_pipe)
            logger.info("nonwindows stdout: %d", stdout_fd)
            logger.info("nonwindows stderr: %d", stderr_fd)

            # read_thread = threading.Thread(name="wandb_stream_read",
            #    target=wandb_stream_read, args=(stdout_fd,))
            # read_thread.start()
            # stdout_read_file = os.fdopen(stdout_fd, "rb")
            # stderr_read_file = os.fdopen(stderr_fd, "rb")
            # stdout_streams, stderr_streams = _get_stdout_stderr_streams()
            # stdout_tee = io_wrap.Tee(stdout_read_file, *stdout_streams)
            # stderr_tee = io_wrap.Tee(stderr_read_file, *stderr_streams)

    stopped = threading.Event()

    write_queue = queue.Queue()
    write_thread = threading.Thread(
        name="wandb_write", target=wandb_write, args=(settings, write_queue, stopped)
    )

    # offline requires doesnt need these queues and threads
    send_queue = None
    read_queue = None
    data_queue = None
    send_thread = None
    read_thread = None

    if not settings.offline:
        send_queue = queue.Queue()
        read_queue = queue.Queue()
        data_queue = queue.Queue()

        send_thread = threading.Thread(
            name="wandb_send",
            target=wandb_send,
            args=(
                settings,
                process_queue,
                notify_queue,
                send_queue,
                resp_queue,
                read_queue,
                data_queue,
                stopped,
                run_meta,
                system_stats,
            ),
        )
        read_thread = threading.Thread(
            name="wandb_read",
            target=wandb_read,
            args=(settings, read_queue, data_queue, stopped),
        )
        # sequencer_thread - future

    # startup all the threads
    if read_thread:
        read_thread.start()
    if send_thread:
        send_thread.start()
    if write_thread:
        write_thread.start()

    done = False
    while not done:
        count = 0
        # TODO: think about this try/except clause
        try:
            while True:
                try:
                    i = notify_queue.get(
                        block=True, timeout=settings._internal_queue_timeout
                    )
                except queue.Empty:
                    i = queue.Empty
                if i == queue.Empty:
                    pass
                elif i == constants.NOTIFY_PROCESS:
                    rec = process_queue.get()
                    if send_queue:
                        send_queue.put(rec)
                    write_queue.put(rec)
                elif i == constants.NOTIFY_SHUTDOWN:
                    # make sure queue is empty?
                    stopped.set()
                    done = True
                    break
                elif i == constants.NOTIFY_REQUEST:
                    rec = req_queue.get()
                    # check if reqresp set
                    if send_queue:
                        send_queue.put(rec)
                    if not rec.control.local:
                        write_queue.put(rec)
                else:
                    print("unknown", i)
                _check_process(settings, parent_pid)
        except KeyboardInterrupt:
            print("\nInterrupt: {}\n".format(count))
            count += 1
        finally:
            if count >= 2:
                done = True
        if done:
            break

    if system_stats:
        system_stats.shutdown()

    if read_thread:
        read_thread.join()
    if send_thread:
        send_thread.join()
    if write_thread:
        write_thread.join()
