"""ttyplayer serve --stream: what mpv plays, encoded to Opus by ffmpeg, for many listeners at once.

mpv writes raw PCM to its stdout (MpvClient(headless_pcm=True)); a pacer thread hands it on to
ffmpeg at the speed it plays, and silence while mpv has none (paused, between tracks), so the
stream never stalls; one reader thread splits ffmpeg's Ogg output into pages and offers
each page to every listener. A listener that falls behind loses pages, never the others' sound.
Late listeners first get the stream's header pages, which an Opus decoder needs before any audio.
Linux and macOS only: next_chunk polls mpv's pipe with select, which Windows offers for sockets
alone, so cli.stream_ffmpeg refuses the mode there.
"""

import asyncio
import os
import select
import shutil
import struct
import subprocess
import threading
import time

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME = CHANNELS * 2  # bytes per sample frame, s16le
BYTES_PER_SECOND = SAMPLE_RATE * FRAME
PCM_CHUNK = BYTES_PER_SECOND // 50  # 20 ms
SILENCE = bytes(PCM_CHUNK)
MAX_LAG = 0.5  # seconds behind the clock (ffmpeg or the machine stalled) after which the clock starts over
STOP_TIMEOUT = 2  # seconds ffmpeg gets to exit before it is killed
BACKLOG = 250  # Ogg pages (20 ms each, so 5 s) a listener may fall behind before its new pages are dropped

# The mpv options that make it write PCM, in the format ffmpeg reads, to its stdout instead of a sound card.
MPV_PCM_OPTIONS = [
    "--ao=pcm",
    "--ao-pcm-file=/dev/stdout",
    "--ao-pcm-waveheader=no",
    "--audio-format=s16",
    f"--audio-samplerate={SAMPLE_RATE}",
    "--audio-channels=stereo",
]


def ffmpeg_argv(path):
    """ffmpeg reading that PCM on stdin and writing Ogg Opus on stdout, one small page per packet."""
    return [
        path, "-hide_banner", "-loglevel", "error",
        "-probesize", "32", "-analyzeduration", "0",  # raw PCM needs no probing: encode from the first chunk
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-i", "-",
        "-c:a", "libopus", "-b:a", "128k",
        "-page_duration", "20000", "-flush_packets", "1",  # a page per 20 ms packet, written at once
        "-f", "ogg", "-",
    ]  # fmt: skip


def find_ffmpeg():
    return shutil.which("ffmpeg")


# --- Ogg pages ----------------------------------------------------------------

OGG_HEADER = struct.Struct("<4sBBqIIIB")  # capture pattern, version, type, granule, serial, sequence, crc, segments


def read_page(source):
    """The next whole Ogg page from source, or None at its end."""
    header = source.read(OGG_HEADER.size)
    if len(header) < OGG_HEADER.size:
        return None
    capture, *_, segments = OGG_HEADER.unpack(header)
    if capture != b"OggS":
        raise ValueError("ffmpeg's output is not Ogg")
    table = source.read(segments)
    body = source.read(sum(table))
    if len(table) < segments or len(body) < sum(table):
        return None
    return header + table + body


def granule(page):
    """The page's granule position: 0 for the Opus header pages, the sample count after it for audio."""
    return OGG_HEADER.unpack_from(page)[3]


# --- the listeners --------------------------------------------------------------


class Listener:
    """One client's pages: the reader thread offers them, the client's event loop takes them.

    More than BACKLOG pages behind, the new ones are dropped for this client alone.
    """

    def __init__(self, loop, backlog=BACKLOG):
        self.loop = loop
        self.pages = asyncio.Queue(backlog)
        self.dropped = 0

    def offer(self, page):
        """From any thread: queue page; None ends the stream for this client."""
        try:
            self.loop.call_soon_threadsafe(self._put, page)
        except RuntimeError:
            pass  # the client's loop closed: nobody is listening any more

    def _put(self, page):
        if page is None and self.pages.full():
            self.pages.get_nowait()  # the end must get in: it costs the oldest page
        try:
            self.pages.put_nowait(page)
        except asyncio.QueueFull:
            self.dropped += 1

    async def next(self):
        """The next page, or None once the stream ended."""
        return await self.pages.get()


class Streamer:
    """Runs ffmpeg on pcm (mpv's stdout) and fans its pages out to the listeners until stop()."""

    def __init__(self, pcm, ffmpeg="ffmpeg", clock=time.monotonic, sleep=time.sleep):
        self.pcm = pcm
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.listeners = set()
        self.headers = []  # the pages before the first audio page, for listeners who join later
        self.pages_read = 0
        self.ended = False
        self.process = subprocess.Popen(ffmpeg_argv(ffmpeg), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.pacer = threading.Thread(target=self.pace, daemon=True)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.pacer.start()
        self.reader.start()

    def pace(self):
        """Hand ffmpeg a chunk of pcm each time the last one has played, until mpv's stdout ends.

        mpv's pcm output does not wait for a sound card, so without this it would race through each
        track and the listeners would get minutes of sound at once. Behind by more than MAX_LAG,
        the clock starts over rather than bursting to catch up.
        """
        start, sent = self.clock(), 0
        try:
            while True:
                ahead = sent / BYTES_PER_SECOND - (self.clock() - start)
                if ahead > 0:
                    self.sleep(ahead)
                elif ahead < -MAX_LAG:
                    start, sent = self.clock(), 0
                chunk = self.next_chunk()
                if not chunk:
                    break
                self.process.stdin.write(chunk)
                self.process.stdin.flush()
                sent += len(chunk)
        except (OSError, ValueError):
            pass  # ffmpeg, or mpv's pipe, closed under it: stop() is running
        finally:
            close_quietly(self.process.stdin)

    def next_chunk(self):
        """mpv's next PCM, whole frames only, if it has some ready; else silence; b"" once mpv quit."""
        fd = self.pcm.fileno()
        if not select.select([fd], [], [], 0)[0]:
            return SILENCE
        chunk = os.read(fd, PCM_CHUNK)
        while chunk and len(chunk) % FRAME:  # silence after half a frame would shift every later sample
            more = os.read(fd, FRAME - len(chunk) % FRAME)
            if not more:
                break
            chunk += more
        return chunk

    def read(self):
        """Offer each of ffmpeg's pages to every listener; at its end, end them all."""
        try:
            while page := read_page(self.process.stdout):
                with self.lock:
                    if granule(page) == 0 and len(self.headers) == self.pages_read:
                        self.headers.append(page)  # still before the first audio page
                    self.pages_read += 1
                    listeners = list(self.listeners)
                for listener in listeners:
                    listener.offer(page)
        except (OSError, ValueError):
            pass  # stop() closed the pipe, or ffmpeg wrote something else
        finally:
            with self.lock:
                self.ended = True
                listeners = list(self.listeners)
            for listener in listeners:
                listener.offer(None)

    def listen(self, loop):
        """A new Listener on loop, given the header pages first; it ends at once if the stream did."""
        listener = Listener(loop)
        with self.lock:
            for page in self.headers:
                listener.offer(page)
            if self.ended:
                listener.offer(None)
            else:
                self.listeners.add(listener)
        return listener

    def unlisten(self, listener):
        with self.lock:
            self.listeners.discard(listener)

    def end_listeners(self):
        """End every listener's stream now (the server is stopping); the encoder runs on."""
        with self.lock:
            listeners, self.listeners = self.listeners, set()
        for listener in listeners:
            listener.offer(None)

    def stop(self):
        """Stop ffmpeg (after mpv quit: its stdout already ended) and the two threads."""
        close_quietly(self.process.stdin)
        try:
            self.process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.pacer.join(timeout=1)
        self.reader.join(timeout=1)
        close_quietly(self.process.stdout)


def close_quietly(pipe):
    try:
        pipe.close()
    except OSError:
        pass  # a pipe whose other end is gone may fail its last flush
