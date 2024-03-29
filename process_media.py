#!/usr/bin/env python3
import contextlib
import json
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
from gevent.subprocess import PIPE, STDOUT, Popen, run
from pacbar import pacbar
from tendo import singleton

gevent.monkey.patch_all(thread=False)

last_print = None


class DuplicateString(str):
    def __hash__(self):
        return hash(str(id(self)))


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
            child = gevent.spawn(_do_watch_progress, socket_filename, sock, handler)
            try:
                yield socket_filename
            except Exception:
                gevent.kill(child)
                raise


@contextlib.contextmanager
def show_progress(total_duration, proc=None, seek=False):
    """Create a unix-domain socket to watch progress and render tqdm
    progress bar."""
    global last_print

    last_print = time.monotonic()
    with pacbar(length=total_duration) as bar:
        if seek:
            bar.label = "seeking..."

        def handler(key, value):
            global last_print

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
                if bar.label != "seeking..." or bar.pos > 0:
                    bar.label = f"{value:>7}"

        with _watch_progress(handler) as socket_filename:
            yield socket_filename


class Processor(object):
    path = None
    output_path = None
    dry_run = False
    verbose = False
    fake = False
    delete = True
    logger = None
    threads = 0
    sample = False

    allowed_extensions = {"mkv", "mp4", "avi", "m4v", "wmv", "m2ts"}
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
        dry_run,
        verbose,
        fake,
        sample,
        no_delete,
        log_file,
        threads,
    ):
        self.path = path
        self.output_path = output_path
        self.dry_run = dry_run
        self.verbose = verbose
        self.files_to_process = []
        self.fake = fake
        self.sample = sample
        self.delete = not no_delete
        self.lock = singleton.SingleInstance("moveis")
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
        self._log(f"             Dry Run : {self.dry_run}")
        self._log(f"              Delete : {self.delete}")
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
        allowed_extensions = self.allowed_extensions

        files_to_check = {}

        for root, _, files in os.walk(self.path):
            for name in files:
                file_parts = name.split(".")
                file_path = os.path.join(root, name)
                if file_parts[-1] in allowed_extensions:
                    if root not in files_to_check:
                        files_to_check[root] = []
                    files_to_check[root].append(name)
                    self._log(f"Found unknown file: {file_path}")

        self._check_movies(files_to_check)
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
                    if self.delete:
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
            self._process_movie_file(file_path, total, current)
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
            f"({current}/{total}) {filename} - " f"source: {original_resolution}p"
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
                    self._log("source resolution HD or better, skipping SD encode")
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

        base_output = os.path.join(self.output_path, "main", file_folder)
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
                float(ffmpeg.probe(file_path)["format"]["duration"]) * 1_000_000
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
                        .global_args("-progress", "unix://{}".format(socket_filename))
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
            (stream for stream in probe["streams"] if stream["codec_type"] == "video"),
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

        title_parse = re.match(r"(.*) \((\d{4})\)", base_name)
        title = title_parse.group(1)
        release_year = title_parse.group(2)
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

        self._encode_video(from_path, to_path, target_resolution, title, release_year)

        if current_resolution == 2160:
            library_name = "2160p"
        else:
            library_name = "main"
        base_output = os.path.join(output_path, library_name, file_folder)
        orig_output = os.path.join(output_path, "orig", file_folder)

        if is_processed:
            os.makedirs(base_output, exist_ok=True)
            os.chmod(base_output, 0o775)
            source_to_path = os.path.join(base_output, source_file)
        else:
            if target_resolution == 2160:
                os.makedirs(orig_output, exist_ok=True)
                os.chmod(orig_output, 0o775)
            source_to_path = os.path.join(orig_output, source_file)

        if self.delete:
            if is_processed or target_resolution == 2160:
                self._run(f'shutil.move("{from_path}", "{source_to_path}")')
                self._run(f'os.chmod("{source_to_path}", 0o664)')
            else:
                self._run(f'os.remove("{from_path}")')
        elif is_processed or target_resolution == 2160:
            self._run(f'shutil.copyfile("{from_path}", "{source_to_path}")')
            self._run(f'os.chmod("{source_to_path}", 0o664)')

        return target_resolution, output_file

    def _check_hdr(self, from_path):
        p = Popen(
            f'ffmpeg -loglevel panic -i "{from_path}" -c:v copy -vbsf hevc_mp4toannexb -f hevc - | hdr10plus_parser --verify -',
            shell=True,
            stdin=PIPE,
            stdout=PIPE,
            stderr=STDOUT,
            close_fds=True,
        )
        output = p.stdout.read().decode("utf8")

        dynamic_hdr = None
        if "Dynamic HDR10+ metadata detected." in output:
            dynamic_hdr = True
            run(
                f'ffmpeg -i "{from_path}" -c:v copy -vbsf hevc_mp4toannexb -f hevc - | hdr10plus_parser -o /tmp/metadata.json -',
                shell=True,
            )
        else:
            p = Popen(
                f'ffprobe -v error -show_streams -select_streams v:0 -of json -i "{from_path}"',
                shell=True,
                stdin=PIPE,
                stdout=PIPE,
                stderr=STDOUT,
                close_fds=True,
            )

            try:
                output = json.loads(p.stdout.read().decode("utf8"))
            except json.decoder.JSONDecodeError:
                pass
            else:
                streams = output.get("streams", [{}])

                if len(streams) > 0 and streams[0].get("color_primaries") == "bt2020":
                    dynamic_hdr = False

        return dynamic_hdr

    def _get_audio_track(self, from_path):
        p = Popen(
            f'ffprobe -v error -show_entries stream=index:stream_tags=language -select_streams a -of json -i "{from_path}"',
            shell=True,
            stdin=PIPE,
            stdout=PIPE,
            stderr=STDOUT,
            close_fds=True,
        )

        track = 0
        language = None

        try:
            output = json.loads(p.stdout.read().decode("utf8"))
        except json.decoder.JSONDecodeError:
            pass
        else:
            for index, stream in enumerate(output["streams"]):
                lang = stream.get("tags", {}).get("language")

                if lang == "eng" or index == 0:
                    track = stream.get("index", index + 1) - 1
                    language = lang

                    if language == "eng":
                        break

        if language is None:
            language = "eng"

        if language != "eng":
            self._log(f"could not find English audio track. Found: {language}")
        return language, track

    def _encode_video(self, from_path, to_path, target_resolution, title, release_year):
        width = self.video_resolutions[target_resolution]["width"]

        dynamic_hdr = self._check_hdr(from_path)
        hdr_string = "none" if dynamic_hdr is None else ("yes" if dynamic_hdr else "no")
        language, track = self._get_audio_track(from_path)

        self._log(f"    encode (hdr: {hdr_string}): {from_path} -> {to_path}")
        if self.verbose:
            self._log(f"        from_path : {from_path}")
            self._log(f"          to_path : {to_path}")
            self._log(f"target_resolution : {target_resolution}")
            self._log(f"             hdr  : {hdr_string}")
            self._log(f"            width : {width}")
            self._log(f"            title : {title}")
            self._log(f"      audio track : {track}")

        total_duration = int(
            float(ffmpeg.probe(from_path)["format"]["duration"]) * 1_000_000
        )

        input_options = {
            "y": None,
            "v": "fatal",
            "stats": None,
            "hide_banner": None,
        }

        output_options = {
            DuplicateString("metadata"): f"title={title}",
            DuplicateString("metadata"): f"year={release_year}",
            "map_chapters": 0,
            "map_metadata": -1,
            "metadata:s:a:0": f"language={language}",
            "profile:v": "main10",
            "pix_fmt:v": "yuv420p10le",
            "preset:v": "veryfast",
            "crf": 20,
            "movflags": "+faststart",
            "vf": f"scale={width}:-2:flags=lanczos",
            "x265-params": "frame-threads=0",
            DuplicateString("map"): "0:v",
            DuplicateString("map"): f"0:a:{track}",
            "c:a": "aac",
            "c:v": "libx265",
        }

        if dynamic_hdr is not None:
            if dynamic_hdr:
                output_options["x265-params"] = (
                    "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                    "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
                    "max-cll=1016,115:hdr10=1:frame-threads=0:dhdr10-info=/tmp/metadata.json"
                )
            else:
                output_options["x265-params"] = (
                    "hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:frame-threads=0:"
                    "master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50):max-cll=0,0"
                )

        seek = False
        if self.sample and not from_path.endswith("p.mp4"):
            input_options["ss"] = "00:03:30"
            input_options["t"] = 30
            total_duration = 30_000_000
            seek = True

        if self.threads > 0:
            output_options["threads"] = self.threads

        with show_progress(total_duration, self, seek=seek) as socket_filename:
            try:
                stream = ffmpeg.input(from_path, **input_options).output(
                    to_path, **output_options
                )

                if self.dry_run:
                    self._log(" ".join(stream.compile()))
                    if self.fake:
                        import time

                        bar = pacbar(length=5)
                        for x in range(5):
                            time.sleep(1)
                            bar.update(1)
                        bar.render_finish()
                else:
                    (
                        stream.global_args(
                            "-progress", "unix://{}".format(socket_filename)
                        ).run(capture_stdout=True, capture_stderr=True)
                    )
            except ffmpeg.Error as e:
                print("ffmpeg error")
                print(e.stderr, file=sys.stderr)
                sys.exit(1)

            try:
                socket_filename.close()
            except AttributeError:
                pass

    def _run(self, statement):
        if self.verbose:
            self._log(statement)
        if not self.dry_run:
            eval(statement)

    def process(self):
        self._log(f"Processing movies\n")
        self._find_files()
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
    "-s",
    "--sample",
    help="Create sample files instead of processing the whole movie",
    is_flag=True,
)
@click.option(
    "-n",
    "--no-delete",
    help=(
        "Do not delete files. Copy instead of rename. "
        "Does not affect initial file checking."
    ),
    is_flag=True,
)
@click.option("-l", "--log-file", help="File to log output to", type=click.Path())
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
def main(*args, **kwargs):
    """Processes files in given directory"""

    base_path = None
    base_output_path = None
    base_path = kwargs["path"]
    base_output_path = kwargs["output_path"]

    if base_path is not None:
        kwargs["path"] = os.path.join(base_path)
    if base_output_path is not None:
        kwargs["output_path"] = os.path.join(base_output_path)
    processor = Processor(*args, **kwargs)
    processor.process()


if __name__ == "__main__":
    main()
