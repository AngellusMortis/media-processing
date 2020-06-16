#!/usr/bin/env python
from __future__ import print_function, unicode_literals

import contextlib
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from datetime import datetime

import click
import ffmpeg
import gevent
import gevent.monkey
import time
from pacbar import pacbar
from tendo import singleton

gevent.monkey.patch_all(thread=False)


@contextlib.contextmanager
def _tmpdir_scope():
    tmpdir = tempfile.mkdtemp()
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir)


def _do_watch_progress(filename, sock, handler):
    """Function to run in a separate gevent greenlet to read progress
    events from a unix-domain socket."""
    connection, client_address = sock.accept()
    data = b""
    try:
        while True:
            more_data = connection.recv(16)
            if not more_data:
                break
            data += more_data
            lines = data.split(b"\n")
            for line in lines[:-1]:
                line = line.decode()
                parts = line.split("=")
                key = parts[0] if len(parts) > 0 else None
                value = parts[1] if len(parts) > 1 else None
                handler(key, value)
            data = lines[-1]
    finally:
        connection.close()


@contextlib.contextmanager
def _watch_progress(handler):
    """Context manager for creating a unix-domain socket and listen for
    ffmpeg progress events.

    The socket filename is yielded from the context manager and the
    socket is closed when the context manager is exited.

    Args:
        handler: a function to be called when progress events are
            received; receives a ``key`` argument and ``value``
            argument. (The example ``show_progress`` below uses tqdm)

    Yields:
        socket_filename: the name of the socket file.
    """
    with _tmpdir_scope() as tmpdir:
        socket_filename = os.path.join(tmpdir, "sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with contextlib.closing(sock):
            sock.bind(socket_filename)
            sock.listen(1)
            child = gevent.spawn(
                _do_watch_progress, socket_filename, sock, handler
            )
            try:
                yield socket_filename
            except Exception:
                gevent.kill(child)
                raise


@contextlib.contextmanager
def show_progress(total_duration, proc=None):
    """Create a unix-domain socket to watch progress and render tqdm
    progress bar."""

    last_print = time.monotonic()
    with pacbar(length=total_duration) as bar:

        def handler(key, value):
            if key == "out_time_ms":
                out_time = int(value)
                bar.update(out_time - bar.pos)

                if proc is not None:
                    now = time.monotonic()
                    if (now - last_print) > 300:
                        proc._log(f"encode progress {out_time/total_duration * 100}")
                        last_print = now

            elif key == "progress" and value == "end":
                bar.update(bar.length - bar.pos)
            elif key == "speed":
                bar.label = f"{value:>7}"

        with _watch_progress(handler) as socket_filename:
            yield socket_filename


class Processor(object):
    path = None
    output_path = None
    processing_mode = None
    dry_run = False
    verbose = False
    fake = False
    delete = True
    logger = None
    threads = 0

    allowed_extensions = {
        "movies": ["mkv", "mp4", "avi", "m4v", "wmv", "m2ts"],
        "television": ["mkv", "mp4"],
        "music": ["flac"],
    }
    files_to_process = []

    video_resolutions = {
        2160: {"width": 3840, "height": 2160},
        1080: {"width": 1920, "height": 1080},
        720: {"width": 1280, "height": 720},
        480: {"width": 720, "height": 480},
    }

    def __init__(
        self,
        path,
        output_path,
        processing_mode,
        dry_run,
        verbose,
        fake,
        no_delete,
        log_file,
        threads,
    ):
        self.path = path
        self.output_path = output_path
        self.processing_mode = processing_mode
        self.dry_run = dry_run
        self.verbose = verbose
        self.files_to_process = []
        self.fake = fake
        self.delete = not no_delete
        self.lock = singleton.SingleInstance(self.processing_mode)
        self.threads = threads

        if log_file is not None:
            self.logger = logging.getLogger("download_media")
            self.logger.setLevel(logging.DEBUG)

            fh = logging.FileHandler(log_file)
            fh.setLevel(logging.DEBUG)
            self.logger.addHandler(fh)

        self._log(f"          Start Time : {datetime.now().isoformat()}")
        self._log(f"Processing Directory : {self.path}")
        self._log(f"    Output Directory : {self.output_path}")
        self._log(f"     Processing Mode : {self.processing_mode}")
        self._log(f"             Dry Run : {self.dry_run}")
        self._log(f"      Verbose Output : {self.verbose}")
        self._log(f"           Lock File : {self.lock.lockfile}")
        self._log(f"         CPU Threads : {self.threads}")

        if log_file is not None:
            self._log(f"              Logger : {log_file}")
        self._log()

    def _log(self, message=""):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        click.echo(f"{now} {message}")

        if self.logger is not None:
            self.logger.info(message)

    def _find_files(self):
        allowed_extensions = self.allowed_extensions[self.processing_mode]

        files_to_check = {}

        for root, dirs, files in os.walk(self.path):
            for name in files:
                file_parts = name.split(".")
                file_path = os.path.join(root, name)
                if file_parts[-1] in allowed_extensions:
                    if root not in files_to_check:
                        files_to_check[root] = []
                    files_to_check[root].append(name)
                elif not self.processing_mode == "music":
                    self._log(f"Found unknown file: {file_path}")

        if self.processing_mode == "movies":
            self._check_movies(files_to_check)
        else:
            for folder, filenames in files_to_check.items():
                for filename in filenames:
                    full_path = os.path.join(folder, filename)
                    self.files_to_process.append(full_path)

        self._log(f"Found {len(self.files_to_process)} files to process")
        if self.verbose:
            self._log(self.files_to_process)

    def _check_movies(self, files_to_check):
        for root_path, files in files_to_check.items():
            if len(files) > 1:
                if self.verbose:
                    self._log(
                        f"more than one file found in "
                        "{root_path}, checking duplicates"
                    )
                master = self._find_highest_res(files)
                files.remove(master)
                for file_name in files:
                    file_path = os.path.join(root_path, file_name)
                    self._run(f'os.remove("{file_path}")')
                self.files_to_process.append(os.path.join(root_path, master))
            else:
                self.files_to_process.append(os.path.join(root_path, files[0]))

    def _find_highest_res(self, files):
        highest_res = 0
        for file_name in files:
            if not (file_name.endswith("p.mp4")):
                master_file = file_name
                break
            match = re.search(r"(\d+)p.mp4", file_name)
            res = int(match.group(1))
            if res > highest_res:
                highest_res = res
        if highest_res > 0:
            for file_name in files:
                if file_name.endswith(f"{highest_res}p.mp4"):
                    master_file = file_name
                    break

        if self.verbose:
            self._log(f"master file found: {master_file}")
        return master_file

    def _process_files(self):
        self._log(f"Processing {len(self.files_to_process)} files\n")
        total = len(self.files_to_process)
        current = 0
        for file_path in self.files_to_process.copy():
            current += 1
            if self.processing_mode == "movies":
                self._process_movie_file(file_path, total, current)
            elif self.processing_mode == "music":
                self._process_music_file(file_path, total, current)
            self.files_to_process.remove(file_path)

    def _process_movie_file(self, file_path, total, current):
        filename = os.path.basename(file_path)
        base_input = os.path.dirname(file_path)
        file_folder = base_input.split(os.sep)[-1]

        if self.verbose:
            self._log(f"  file_path : {file_path}")
            self._log(f"   filename : {filename}")
            self._log(f" base_input : {base_input}")
            self._log(f"file_folder : {file_folder}")
            self._log(f"output_path : {self.output_path}")

        original_resolution = self._probe_file(file_path)

        if original_resolution == -1:
            return

        self._log(
            f"({current}/{total}) {filename} - "
            f"source: {original_resolution}p"
        )

        base_name = re.match(r"(.*\(\d+\))", filename).group(1)
        source_file = filename

        if self.verbose:
            self._log(f"  base_name : {base_name}")
            self._log(f"source_file : {source_file}")

        current_resolution = original_resolution
        for res in self.video_resolutions.values():
            target_resolution = res["height"]
            if target_resolution == 480 and original_resolution > 480:
                if self.verbose:
                    self._log(
                        "source resolution HD or better, skipping SD encode"
                    )
            elif current_resolution >= target_resolution:
                current_resolution, source_file = self._process_resolution(
                    target_resolution,
                    current_resolution,
                    source_file,
                    base_name,
                    base_input,
                    self.output_path,
                    file_folder,
                )

        base_output = os.path.join(
            self.output_path, str(current_resolution) + "p", file_folder
        )
        os.makedirs(base_output, exist_ok=True)
        os.chmod(base_output, 0o775)
        from_path = os.path.join(base_input, source_file)
        to_path = os.path.join(base_output, source_file)
        if self.delete:
            self._run(f'shutil.move("{from_path}", "{to_path}")')
        else:
            self._run(f'shutil.copyfile("{from_path}", "{to_path}")')
        self._run(f'os.chmod("{to_path}", 0o664)')
        if self.delete:
            self._run(f'shutil.rmtree("{base_input}")')

    def _process_music_file(self, file_path, total, current):
        filename = os.path.basename(file_path)
        directory = os.path.dirname(file_path)

        to_path = os.path.join(directory, filename.replace("flac", "mp3"))

        if self.verbose:
            self._log(f" file_path : {file_path}")
            self._log(f"  filename : {filename}")
            self._log(f" directory : {directory}")
            self._log(f"   to_path : {to_path}")

        self._log(f"({current}/{total}) {filename}")

        if not self.dry_run:
            total_duration = int(
                float(ffmpeg.probe(file_path)["format"]["duration"])
                * 1_000_000
            )

            options = {
                "y": None,
                "v": "fatal",
                "stats": None,
                "hide_banner": None,
                "ab": "320k",
                "map_metadata": 0,
                "id3v2_version": 3,
            }

            with show_progress(total_duration) as socket_filename:
                try:
                    (
                        ffmpeg.input(file_path)
                        .output(to_path, **options)
                        .global_args(
                            "-progress", "unix://{}".format(socket_filename)
                        )
                        .run(capture_stdout=True, capture_stderr=True)
                    )
                except ffmpeg.Error as e:
                    print("ffmpeg error")
                    print(e.stderr, file=sys.stderr)
                    sys.exit(1)
        self._run(f'os.chmod("{to_path}", 0o664)')

        if self.delete:
            self._run(f'os.remove("{file_path}")')

    def _probe_file(self, file_path):
        current_resolution = -1
        if self.verbose:
            self._log(f"probing {file_path}")
        probe = ffmpeg.probe(file_path)
        video_stream = next(
            (
                stream
                for stream in probe["streams"]
                if stream["codec_type"] == "video"
            ),
            None,
        )

        if video_stream is None:
            self._log(f"No video stream: {file_path}\n")
            return -1

        width = int(video_stream["width"])
        for res in self.video_resolutions.values():
            if width > (res["width"] - 10):
                current_resolution = res["height"]
                break

        return current_resolution

    def _process_resolution(
        self,
        target_resolution,
        current_resolution,
        source_file,
        base_name,
        base_input,
        output_path,
        file_folder,
    ):

        is_processed = source_file.endswith(f" - {current_resolution}p.mp4")
        output_file = f"{base_name} - {target_resolution}p.mp4"
        from_path = os.path.join(base_input, source_file)
        to_path = os.path.join(base_input, output_file)
        output_file = f"{base_name} - {target_resolution}p.mp4"
        title = re.match(r"(.*) \(\d{4}\)", base_name).group(1)
        if title.endswith(", The"):
            title = f"The {title[:-5]}"

        if self.verbose:
            self._log(f"target_resolution : {target_resolution}")
            self._log(f"     is_processed : {is_processed}")
            self._log(f"      output_file : {output_file}")
            self._log(f"        from_path : {from_path}")
            self._log(f"      output_path : {output_path}")
            self._log(f"          to_path : {to_path}")
            self._log(f"      output_file : {output_file}")
            self._log(f"            title : {title}")

        if is_processed and current_resolution == target_resolution:
            return target_resolution, source_file

        self._log(f"    encode: {source_file} -> {output_file}")
        self._encode_video(from_path, to_path, target_resolution, title)
        source_file = output_file

        base_output = os.path.join(
            output_path, str(current_resolution) + "p", file_folder
        )
        os.makedirs(base_output, exist_ok=True)
        os.chmod(base_output, 0o775)
        source_to_path = os.path.join(base_output, source_file)

        if self.delete:
            if is_processed:
                self._run(f'shutil.move("{from_path}", "{source_to_path}")')
                self._run(f'os.chmod("{source_to_path}", 0o664)')
            else:
                self._run(f'os.remove("{from_path}")')
        elif is_processed:
            self._run(f'shutil.copyfile("{from_path}", "{source_to_path}")')
            self._run(f'os.chmod("{source_to_path}", 0o664)')

        return target_resolution, output_file

    def _encode_video(self, from_path, to_path, target_resolution, title):
        width = self.video_resolutions[target_resolution]["width"]

        if self.verbose:
            self._log(f"        from_path : {from_path}")
            self._log(f"          to_path : {to_path}")
            self._log(f"target_resolution : {target_resolution}")
            self._log(f"            width : {width}")
            self._log(f"            title : {title}")

        if self.dry_run:
            if self.fake:
                import time

                bar = pacbar(length=5)
                for x in range(5):
                    time.sleep(1)
                    bar.update(1)
                bar.render_finish()
        else:
            total_duration = int(
                float(ffmpeg.probe(from_path)["format"]["duration"])
                * 1_000_000
            )

            options = {
                "y": None,
                "v": "fatal",
                "stats": None,
                "hide_banner": None,
                "metadata": f"title={title}",
                "c:v": "libx265",
                "sn": None,
                "map_metadata": -1,
                "profile:v": "main10",
                "pix_fmt:v": "yuv420p10le",
                "preset:v": "fast",
                "crf": 21,
                "c:a:0": "aac",
                "ac": 6,
                "vf": f"scale={width}:-2:flags=lanczos",
                "movflags": "+faststart",
            }

            if self.threads > 0:
                options["threads"] = self.threads

            with show_progress(total_duration, self) as socket_filename:
                try:
                    (
                        ffmpeg.input(from_path)
                        .output(to_path, **options)
                        .global_args(
                            "-progress", "unix://{}".format(socket_filename)
                        )
                        .run(capture_stdout=True, capture_stderr=True)
                    )
                except ffmpeg.Error as e:
                    print("ffmpeg error")
                    print(e.stderr, file=sys.stderr)
                    sys.exit(1)

    def _run(self, statement):
        if self.verbose:
            self._log(statement)
        if not self.dry_run:
            eval(statement)

    def process(self):
        self._log(f"Processing {self.processing_mode}\n")
        self._find_files()
        if self.processing_mode in ["movies", "music"]:
            self._process_files()
        self._log("\n")


@click.command()
@click.option(
    "-p",
    "--path",
    help="Path to search for media files to process",
    prompt="Enter processing path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
)
@click.option(
    "-o",
    "--output-path",
    help="Path to put processed files",
    prompt="Enter output processing path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
)
@click.option(
    "-m",
    "--processing-mode",
    help="Type of files to process and how to process them",
    type=click.Choice(["movies", "television", "music"]),
    multiple=True,
    default=["movies", "television", "music"],
)
@click.option(
    "-d",
    "--dry-run",
    help="Gather files to process and print details",
    is_flag=True,
)
@click.option("-v", "--verbose", help="Verbose console output", is_flag=True)
@click.option(
    "-f",
    "--fake",
    help="Fake file encoding. Should be used with dry-run",
    is_flag=True,
)
@click.option(
    "-n",
    "--no-delete",
    help=(
        "Do not delete files. Copy instead of rename. "
        "Does not affect inital file checking."
    ),
    is_flag=True,
)
@click.option(
    "-l", "--log-file", help="File to log output to", type=click.Path()
)
@click.option(
    "-t",
    "--threads",
    help=(
        "Number of threads to use, defaults to ffmpeg "
        "deciding on its own how many to use"
    ),
    type=int,
    default=0,
)
def main(processing_mode, *args, **kwargs):
    """Processes files in given directory"""

    base_path = None
    base_output_path = None
    if len(processing_mode) > 1:
        base_path = kwargs["path"]
        base_output_path = kwargs["output_path"]

    for mode in processing_mode:
        if base_path is not None:
            kwargs["path"] = os.path.join(base_path, mode)
        if base_output_path is not None:
            kwargs["output_path"] = os.path.join(base_output_path, mode)
        kwargs["processing_mode"] = mode
        processor = Processor(*args, **kwargs)
        processor.process()


if __name__ == "__main__":
    main()
