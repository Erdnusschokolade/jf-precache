#!/usr/bin/env python3
"""Jellyfin playback precache hook.

Listens for a Jellyfin webhook on playback start, resolves the media file path
via the PlaybackInfo API, and reads the file sequentially a few times so the data
lands in the ZFS ARC — subsequent seeking ("scrubbing") then serves from RAM
instead of the spinning pool. The trickplay images can be warmed too, but that
is off by default (see the "trickplay" option).

While playback continues, a keep-warm loop re-reads the file once every few
minutes: that refreshes the recency of the blocks in the ARC's MFU list, which
would otherwise decay over the length of a movie. Whether playback is still
running is decided by the /Sessions API, not by webhook events — that survives
missing stop events (client crash) and does not depend on how the user
configured the webhook plugin's payload template.

Configuration (API key etc.) is read from a config file, NOT hardcoded, so no
secret ends up in the packaged/tracked sources. See jf-precache.conf.example.
"""

import configparser
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

# ==========================
# CONFIG
# ==========================
# Search order: $JF_PRECACHE_CONFIG, /etc/jf-precache/jf-precache.conf,
# ~/.config/jf-precache/jf-precache.conf. The api_key has no default and must
# be provided; everything else has a sensible fallback.
CONFIG_PATHS = [
    os.environ.get("JF_PRECACHE_CONFIG"),
    "/etc/jf-precache/jf-precache.conf",
    os.path.expanduser("~/.config/jf-precache/jf-precache.conf"),
]

DEFAULTS = {
    "jf_url": "http://127.0.0.1:8096",
    "api_key": "",
    "bs": "64M",                     # dd block size (good range: 8M–64M)
    "runs": "3",                     # sequential passes -> MFU promotion (2–3 recommended)
    "webhook": "yes",                # listen for Jellyfin's playback webhook
    "listen_host": "127.0.0.1",
    "listen_port": "9000",
    "poll_interval": "0",            # seconds; 0 = off (webhook only)
    "trickplay": "no",               # also warm <name>.trickplay (see conf example)
    "keepwarm": "yes",               # re-read while playback is running
    "keepwarm_interval": "180",      # seconds between keep-warm passes
    "keepwarm_runtime_factor": "1.5",# budget = media runtime * this
    "require_active_session": "yes", # only precache what /Sessions says is playing
    "api_timeout": "5",              # seconds per Jellyfin API call
}

# Tuning knobs that are deliberately NOT configurable — a 250 line tool does not
# need twelve dials.
KEEPWARM_DUTY = 0.25          # max share of wall clock spent reading
KEEPWARM_PAUSE_GRACE = 900    # seconds of continuous pause before giving up
KEEPWARM_COLD_FACTOR = 2.0    # pass counts as "from disk" above this * baseline
KEEPWARM_COLD_RATE = 1.0      # ... and only below this GiB/s; RAM does not read
                              # this slowly, spinning disks never read this fast.
                              # Scales with file size, unlike a fixed duration:
                              # a 500 MiB episode off a platter is done in seconds.
KEEPWARM_COLD_GIVEUP = 3      # consecutive cold passes before giving up
GIB = 1024 ** 3
MAX_API_ERRORS = 5            # consecutive /Sessions failures before giving up
SESSION_CACHE_TTL = 5         # seconds a /Sessions snapshot is reused; keep it well
                              # below keepwarm_interval, it only exists to coalesce
                              # workers that happen to wake up together
SESSION_ACTIVE_WITHIN = 600   # activeWithinSeconds query parameter
GATE_ATTEMPTS = 5             # /Sessions retries before refusing to precache
GATE_DELAY = 1.0              # seconds between those retries
ADOPT_ATTEMPTS = 6            # /Sessions retries at startup (we may beat Jellyfin up)
ADOPT_DELAY = 5.0             # seconds between those retries


# ==========================
# LOGGING
# ==========================
# We run under systemd, so stdout IS the log: journald adds the timestamp and
# reads the "<N>" prefix as the syslog priority (SyslogLevelPrefix= defaults to
# true and strips it again). flush=True because stdout on a pipe is block
# buffered and would otherwise show up in chunks, minutes late.
LOG_ERR = 3
LOG_WARNING = 4
LOG_INFO = 6


def log(msg, level=LOG_INFO):
    print(f"<{level}>{msg}", flush=True)


def fmt_secs(seconds):
    return f"{seconds:.1f}s"


def describe_budget(entry, runtime):
    if entry.max_runtime is None:
        return "no budget, session-bound (media runtime unknown)"
    return f"budget {entry.max_runtime}s (media runtime {int(runtime // 60)}min)"


def _int(raw, fallback, name):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        log(f"Invalid value for {name}: {raw!r} — using {fallback}", LOG_WARNING)
        return fallback


def _float(raw, fallback, name):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log(f"Invalid value for {name}: {raw!r} — using {fallback}", LOG_WARNING)
        return fallback
    if value <= 0:
        log(f"{name} must be > 0, got {value} — using {fallback}", LOG_WARNING)
        return fallback
    return value


def _bool(raw, fallback, name):
    value = str(raw).strip().lower()
    if value in ("1", "yes", "true", "on"):
        return True
    if value in ("0", "no", "false", "off"):
        return False
    log(f"Invalid value for {name}: {raw!r} — using {fallback}", LOG_WARNING)
    return fallback


class Config:
    def __init__(self):
        cp = configparser.ConfigParser()
        cp["jf-precache"] = dict(DEFAULTS)
        loaded_from = None
        for path in CONFIG_PATHS:
            if path and os.path.isfile(path):
                cp.read(path)
                loaded_from = path
                break
        sec = cp["jf-precache"]
        # Environment overrides win over the file, for containers/systemd drop-ins.
        self.jf_url = os.environ.get("JF_URL", sec.get("jf_url"))
        self.api_key = os.environ.get("JF_API_KEY", sec.get("api_key"))
        self.bs = os.environ.get("JF_BS", sec.get("bs"))
        self.runs = _int(os.environ.get("JF_RUNS", sec.get("runs")), 3, "runs")
        self.webhook = _bool(
            os.environ.get("JF_WEBHOOK", sec.get("webhook")), True, "webhook"
        )
        self.listen_host = os.environ.get("JF_LISTEN_HOST", sec.get("listen_host"))
        self.listen_port = _int(
            os.environ.get("JF_LISTEN_PORT", sec.get("listen_port")), 9000, "listen_port"
        )
        self.poll_interval = _int(
            os.environ.get("JF_POLL_INTERVAL", sec.get("poll_interval")), 0, "poll_interval"
        )
        self.trickplay = _bool(
            os.environ.get("JF_TRICKPLAY", sec.get("trickplay")), False, "trickplay"
        )
        self.keepwarm = _bool(
            os.environ.get("JF_KEEPWARM", sec.get("keepwarm")), True, "keepwarm"
        )
        self.keepwarm_interval = _int(
            os.environ.get("JF_KEEPWARM_INTERVAL", sec.get("keepwarm_interval")),
            180, "keepwarm_interval",
        )
        self.keepwarm_runtime_factor = _float(
            os.environ.get("JF_KEEPWARM_RUNTIME_FACTOR", sec.get("keepwarm_runtime_factor")),
            1.5, "keepwarm_runtime_factor",
        )
        self.require_active_session = _bool(
            os.environ.get("JF_REQUIRE_SESSION", sec.get("require_active_session")),
            True, "require_active_session",
        )
        self.api_timeout = _int(
            os.environ.get("JF_API_TIMEOUT", sec.get("api_timeout")), 5, "api_timeout"
        )
        self.loaded_from = loaded_from

        if sec.get("log") or os.environ.get("JF_LOG"):
            log(
                "The 'log' option (JF_LOG) is obsolete and ignored — jf-precache "
                "logs to stdout now, see 'journalctl -u jf-precache'.",
                LOG_WARNING,
            )
        if sec.get("keepwarm_max_runtime") or os.environ.get("JF_KEEPWARM_MAX_RUNTIME"):
            log(
                "The 'keepwarm_max_runtime' option (JF_KEEPWARM_MAX_RUNTIME) is "
                "obsolete and ignored — the budget is now keepwarm_runtime_factor "
                "times the media's own runtime.",
                LOG_WARNING,
            )


CFG = Config()

WARM = {}                        # path -> Warm; dedupe is per FILE, not per session
WARM_LOCK = threading.Lock()
SHUTDOWN = threading.Event()
CHILDREN = set()                 # running dd/tar, so shutdown can terminate them
CHILDREN_LOCK = threading.Lock()


# ==========================
# JELLYFIN API
# ==========================
def norm_id(value):
    # The API returns dashed UUIDs, webhook templates may render them without
    # dashes depending on the version — compare normalised.
    return str(value).replace("-", "").lower() if value else ""


def api_get(path, params=None):
    """GET against the Jellyfin API. -> (status, json); (None, None) if unreachable."""
    query = {"api_key": CFG.api_key}
    if params:
        query.update(params)
    try:
        r = requests.get(f"{CFG.jf_url}{path}", params=query, timeout=CFG.api_timeout)
    except Exception as e:
        log(f"ERROR calling {path}: {e}", LOG_ERR)
        return None, None
    if r.status_code >= 400:
        return r.status_code, None
    try:
        return r.status_code, r.json()
    except ValueError as e:
        log(f"ERROR parsing response from {path}: {e}", LOG_ERR)
        return r.status_code, None


def ticks_to_secs(ticks):
    """Jellyfin reports durations in 100-nanosecond ticks."""
    try:
        return int(ticks) / 10_000_000 if ticks else None
    except (TypeError, ValueError):
        return None


def get_media_info(item_id, media_source_id=None):
    """Resolve the file. -> (path, runtime_seconds); (None, None) on failure."""
    status, data = api_get(f"/Items/{item_id}/PlaybackInfo")
    if not data:
        log(f"ERROR fetching media path for {item_id} (status {status})", LOG_ERR)
        return None, None
    sources = data.get("MediaSources") or []
    if not sources:
        return None, None

    chosen = sources[0]
    # An item can carry several versions (4K + 1080p); the session tells us which
    # one is actually playing.
    if media_source_id:
        for src in sources:
            if norm_id(src.get("Id")) == norm_id(media_source_id):
                chosen = src
                break
        else:
            log(
                f"MediaSource {media_source_id} not in PlaybackInfo — using the first one",
                LOG_WARNING,
            )
    return chosen.get("Path"), ticks_to_secs(chosen.get("RunTimeTicks"))


_SESSIONS = {"at": 0.0, "state": None}
_SESSIONS_LOCK = threading.Lock()


def sessions_invalidate():
    with _SESSIONS_LOCK:
        _SESSIONS["state"] = None


def get_sessions(max_age=SESSION_CACHE_TTL):
    """Cached /Sessions snapshot. -> ("ok", list) | ("denied", None) | ("unknown", None).

    Several workers waking up at the same time share one request; holding the
    lock across the call is what makes them coalesce.
    """
    with _SESSIONS_LOCK:
        cached = _SESSIONS["state"]
        if cached and time.monotonic() - _SESSIONS["at"] < max_age:
            return cached
        status, data = api_get("/Sessions", {"activeWithinSeconds": SESSION_ACTIVE_WITHIN})
        if status in (401, 403):
            state = ("denied", None)
        elif status is None or data is None:
            state = ("unknown", None)
        else:
            state = ("ok", data)
        _SESSIONS["at"] = time.monotonic()
        _SESSIONS["state"] = state
        return state


def session_state(item_ids):
    """Is one of these items playing? -> (state, media_source_id).

    state is playing | paused | gone | unknown (API unreachable) | denied (bad key).
    """
    kind, sessions = get_sessions()
    if kind != "ok":
        return kind, None

    paused_msid = None
    found_paused = False
    for session in sessions:
        now_playing = session.get("NowPlayingItem") or {}
        if norm_id(now_playing.get("Id")) not in item_ids:
            continue
        play_state = session.get("PlayState") or {}
        if play_state.get("IsPaused"):
            found_paused = True
            paused_msid = play_state.get("MediaSourceId")
            continue
        return "playing", play_state.get("MediaSourceId")

    if found_paused:
        return "paused", paused_msid
    return "gone", None


def await_session(item_id):
    """Precache gate: is this item really playing? -> (ok, media_source_id).

    Fails open when Jellyfin cannot answer — the worst case is then the old
    behaviour of precaching unconditionally.
    """
    item_ids = {norm_id(item_id)}
    for attempt in range(GATE_ATTEMPTS):
        state, media_source_id = session_state(item_ids)
        if state in ("playing", "paused"):
            return True, media_source_id
        if state == "unknown":
            log("Session check failed (Jellyfin unreachable) — precaching anyway", LOG_WARNING)
            return True, None
        if state == "denied":
            log("Session check rejected (401/403) — check api_key; precaching anyway", LOG_WARNING)
            return True, None
        if attempt + 1 < GATE_ATTEMPTS and not SHUTDOWN.is_set():
            SHUTDOWN.wait(GATE_DELAY)
            sessions_invalidate()
    return False, None


# ==========================
# READ HELPERS (read-only, always of=/dev/null)
# ==========================
def spawn(cmd, stdin=None, stdout=subprocess.DEVNULL):
    p = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL)
    with CHILDREN_LOCK:
        CHILDREN.add(p)
    return p


def reap(p):
    rc = p.wait()
    with CHILDREN_LOCK:
        CHILDREN.discard(p)
    return rc


def read_file_once(path):
    """One sequential dd pass over the file. -> seconds, or None on failure."""
    started = time.monotonic()
    try:
        rc = reap(spawn(["/usr/bin/dd", f"if={path}", "of=/dev/null", f"bs={CFG.bs}"]))
    except Exception as e:
        log(f"ERROR movie dd: {e}", LOG_ERR)
        return None
    if rc != 0:
        # A read we terminated ourselves on shutdown is not a failure.
        if not SHUTDOWN.is_set():
            log(f"ERROR movie dd exited with {rc}: {path}", LOG_ERR)
        return None
    return time.monotonic() - started


def trickplay_dir(movie_path):
    dirname = os.path.dirname(movie_path)
    basename = os.path.splitext(os.path.basename(movie_path))[0]
    return os.path.join(dirname, basename + ".trickplay")


def read_trickplay_once(tp_dir):
    """tar stream + dd = ideal sequential read of many small files. -> seconds | None."""
    started = time.monotonic()
    tar = None
    try:
        tar = spawn(["tar", "cf", "-", tp_dir], stdout=subprocess.PIPE)
        dd = spawn(["/usr/bin/dd", "of=/dev/null", f"bs={CFG.bs}"], stdin=tar.stdout)
        # Hand the read end over to dd completely, otherwise tar never sees EOF —
        # and without wait() it would stay behind as a zombie holding an fd.
        tar.stdout.close()
        rc_dd = reap(dd)
        rc_tar = reap(tar)
        tar = None
    except Exception as e:
        log(f"ERROR trickplay cache: {e}", LOG_ERR)
        return None
    finally:
        # Only reached with tar still set if spawning dd blew up — otherwise tar
        # would sit forever on a pipe nobody reads.
        if tar is not None:
            if tar.stdout is not None and not tar.stdout.closed:
                tar.stdout.close()
            tar.terminate()
            reap(tar)
    if rc_dd != 0 or rc_tar != 0:
        if not SHUTDOWN.is_set():
            log(f"ERROR trickplay read (tar {rc_tar}, dd {rc_dd}): {tp_dir}", LOG_ERR)
        return None
    return time.monotonic() - started


# ==========================
# WARM REGISTRY (dedupe per file)
# ==========================
def cap_runtime(runtime):
    """How long may we keep this file warm at most? -> seconds, or None.

    A multiple of the media's own length. A fixed number has no sensible value:
    six hours is absurd for a 20 minute episode and too short for an extended
    cut. The factor leaves room for pausing and rewinding.

    None when Jellyfin reports no runtime (live TV, odd containers) — then the
    session decides alone, which it does reliably enough: /Sessions is queried
    with activeWithinSeconds, so the ghost session of a crashed client drops out
    by itself, and a long pause ends the loop anyway.
    """
    if not runtime or runtime <= 0:
        return None
    return int(runtime * CFG.keepwarm_runtime_factor)


class Warm:
    """One entry per media file, however many sessions are playing it."""

    def __init__(self, path, item_id, runtime=None):
        self.path = path
        self.item_ids = {norm_id(item_id)}
        self.wake = threading.Event()
        self.epoch = 0
        self.started = time.monotonic()
        self.baseline = None          # duration of the last precache pass (warm by definition)
        self.tp_missing_logged = False
        self.thread = None
        try:
            self.size = os.path.getsize(path)
        except OSError:
            self.size = 0
        self.runtime = runtime        # media length in seconds, if Jellyfin told us
        self.max_runtime = cap_runtime(runtime)


def warm_register(path, item_id, runtime=None):
    """Create or refresh the entry for this file. -> (entry, created)."""
    with WARM_LOCK:
        entry = WARM.get(path)
        if entry is not None:
            entry.item_ids.add(norm_id(item_id))
            # A second item on the same file may be the longer one (different
            # edition); never shrink an already granted budget. None means
            # "unbounded, session decides" and already is the widest budget.
            if runtime and runtime > (entry.runtime or 0):
                entry.runtime = runtime
                if entry.max_runtime is not None:
                    entry.max_runtime = max(entry.max_runtime, cap_runtime(runtime))
            entry.epoch += 1
            entry.wake.set()
            return entry, False
        entry = Warm(path, item_id, runtime)
        WARM[path] = entry
        entry.thread = threading.Thread(target=warm_worker, args=(entry,), daemon=True)
        entry.thread.start()
        return entry, True


def warm_release(entry, epoch_seen, reason):
    """Drop the entry — unless it was re-registered while we decided to quit."""
    with WARM_LOCK:
        if entry.epoch != epoch_seen:
            return False
        if WARM.get(entry.path) is entry:
            del WARM[entry.path]
    log(f"Keep-warm stopped ({reason}): {entry.path}")
    return True


def warm_drop(entry):
    with WARM_LOCK:
        if WARM.get(entry.path) is entry:
            del WARM[entry.path]


def warm_snapshot(entry):
    with WARM_LOCK:
        return entry.epoch, set(entry.item_ids)


def warm_covers(item_id):
    key = norm_id(item_id)
    with WARM_LOCK:
        return any(key in entry.item_ids for entry in WARM.values())


def warm_wake(item_id):
    """Make the worker re-check liveness now instead of sleeping out the interval."""
    key = norm_id(item_id)
    sessions_invalidate()
    with WARM_LOCK:
        for entry in WARM.values():
            if key in entry.item_ids:
                entry.wake.set()


def warm_shutdown(timeout=10):
    SHUTDOWN.set()
    with WARM_LOCK:
        entries = list(WARM.values())
    for entry in entries:
        entry.wake.set()
    with CHILDREN_LOCK:
        children = list(CHILDREN)
    for p in children:
        try:
            p.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    for entry in entries:
        if entry.thread is not None:
            entry.thread.join(timeout=max(0.0, deadline - time.monotonic()))


# ==========================
# WORKER: precache -> keep-warm -> exit
# ==========================
def read_trickplay(entry):
    """Warm <name>.trickplay too. Off by default: once those images live on an
    SSD, pulling them into the ARC only costs space the media file wants.

    Jellyfin generates them asynchronously, so the directory may only show up
    mid-playback — hence the check on every pass, not once."""
    if not CFG.trickplay:
        return None
    tp_dir = trickplay_dir(entry.path)
    if not os.path.isdir(tp_dir):
        if not entry.tp_missing_logged:
            log(f"No trickplay directory: {tp_dir}")
            entry.tp_missing_logged = True
        return None
    entry.tp_missing_logged = False
    return read_trickplay_once(tp_dir)


def precache(entry):
    """The initial CFG.runs sequential passes that promote the blocks into MFU."""
    for i in range(CFG.runs):
        if SHUTDOWN.is_set():
            return
        duration = read_file_once(entry.path)
        if duration is None:
            return
        entry.baseline = duration
        log(f"Prefetch movie {i + 1}/{CFG.runs} in {fmt_secs(duration)}: {entry.path}")
    duration = read_trickplay(entry)
    if duration is not None:
        log(f"Trickplay cached in {fmt_secs(duration)}: {trickplay_dir(entry.path)}")


def is_cold(entry, duration):
    """Did this pass come off the pool instead of out of the cache?

    Two conditions, because neither alone is trustworthy: the pass has to be much
    slower than the last precache run (which was warm by definition — that part
    adapts to the machine), *and* slower than something RAM could plausibly
    deliver. The absolute half is a throughput, not a duration: a 20 GiB remux
    and a 500 MiB episode take wildly different times to read either way.
    """
    if entry.baseline is None or duration <= 0 or not entry.size:
        return False
    rate = entry.size / GIB / duration
    return rate < KEEPWARM_COLD_RATE and duration > KEEPWARM_COLD_FACTOR * entry.baseline


def keepwarm_pass(entry):
    """One refresh pass. -> (movie_seconds, total_seconds) or (None, None)."""
    movie = read_file_once(entry.path)
    if movie is None:
        return None, None
    trickplay = read_trickplay(entry)
    total = movie + (trickplay or 0.0)
    detail = (
        f" (movie {fmt_secs(movie)} + trickplay {fmt_secs(trickplay)})"
        if trickplay is not None
        else ""
    )
    log(f"Keep-warm pass in {fmt_secs(total)}{detail}: {entry.path}")
    return movie, total


def keepwarm_loop(entry):
    interval = CFG.keepwarm_interval
    paused_since = None
    api_errors = 0
    cold_streak = 0
    log(f"Keep-warm started (every {interval}s): {entry.path}")

    while not SHUTDOWN.is_set():
        # Order matters: clear before checking, wait after — the other way round
        # swallows a wake() that arrives while we are deciding.
        entry.wake.clear()
        epoch, item_ids = warm_snapshot(entry)

        reason = None
        do_pass = False

        if entry.max_runtime and time.monotonic() - entry.started > entry.max_runtime:
            reason = "max runtime reached"
        elif not os.path.exists(entry.path):
            reason = "path gone"
        else:
            state, _ = session_state(item_ids)
            if state == "playing":
                if paused_since is not None:
                    log(f"Resumed — keep-warm continues: {entry.path}")
                paused_since = None
                api_errors = 0
                do_pass = True
            elif state == "paused":
                api_errors = 0
                if paused_since is None:
                    paused_since = time.monotonic()
                    log(f"Paused — suspending keep-warm: {entry.path}")
                elif time.monotonic() - paused_since > KEEPWARM_PAUSE_GRACE:
                    reason = "paused too long"
            elif state == "gone":
                reason = "playback ended"
            elif state == "denied":
                reason = "Jellyfin rejected the api_key (401/403)"
            else:
                # Unreachable: fail open and keep reading, but not forever — a dead
                # Jellyfin must not leave us hammering the pool until the runtime cap.
                api_errors += 1
                if api_errors >= MAX_API_ERRORS:
                    reason = f"Jellyfin unreachable ({api_errors} attempts)"
                else:
                    do_pass = True

        if reason is not None:
            if warm_release(entry, epoch, reason):
                return
            # Re-registered while we were deciding to quit: playback started again,
            # so carry on with a clean slate.
            log(f"Keep-warm resumed (new playback): {entry.path}")
            paused_since = None
            api_errors = 0
            cold_streak = 0
            continue

        sleep = interval
        if do_pass:
            movie, total = keepwarm_pass(entry)
            if movie is None:
                cold_streak = 0
            else:
                cold_streak = cold_streak + 1 if is_cold(entry, movie) else 0
                if cold_streak >= KEEPWARM_COLD_GIVEUP:
                    slow = f"reads keep coming from disk ({fmt_secs(movie)}) — ARC too small?"
                    if warm_release(entry, epoch, slow):
                        return
                    cold_streak = 0
                # Duty cycle: never spend more than KEEPWARM_DUTY of the wall clock
                # reading. A warm pass comes out of RAM and just uses the interval;
                # a pass that hits the pool stretches the sleep instead of competing
                # with the very stream it is supposed to help.
                sleep = max(interval, total * (1 - KEEPWARM_DUTY) / KEEPWARM_DUTY)

        entry.wake.wait(sleep)

    warm_drop(entry)


def warm_worker(entry):
    try:
        precache(entry)
        if CFG.keepwarm and not SHUTDOWN.is_set():
            keepwarm_loop(entry)
        else:
            warm_drop(entry)
    except Exception as e:
        log(f"ERROR in warm worker for {entry.path}: {e}", LOG_ERR)
        warm_drop(entry)


# ==========================
# PRECACHE WORKFLOW
# ==========================
def run_precache(item_id):
    media_source_id = None
    if CFG.require_active_session:
        ok, media_source_id = await_session(item_id)
        if not ok:
            log(f"No active session for item {item_id} — not precaching", LOG_WARNING)
            return

    path, runtime = get_media_info(item_id, media_source_id)
    if not path:
        log(f"ERROR: no media path resolved for item {item_id}", LOG_ERR)
        return

    log(f"Resolved media path: {path}")
    entry, created = warm_register(path, item_id, runtime)
    if created:
        log(f"Keep-warm {describe_budget(entry, runtime)}")
    else:
        log(f"Already warming {path} — duplicate webhook for item {item_id} ignored")


def discover_playback(what):
    """Register everything /Sessions currently reports as playing.

    -> number of files newly taken on, or None if Jellyfin could not be asked.
    Shared by the startup adoption and the polling loop.
    """
    sessions_invalidate()
    kind, sessions = get_sessions()
    if kind != "ok":
        return None

    found = 0
    for session in sessions:
        now_playing = session.get("NowPlayingItem") or {}
        item_id = now_playing.get("Id")
        # Already covered: skip before PlaybackInfo, that call is the expensive
        # part and polling would otherwise repeat it for every session, forever.
        if not item_id or warm_covers(item_id):
            continue
        play_state = session.get("PlayState") or {}
        path, runtime = get_media_info(item_id, play_state.get("MediaSourceId"))
        if not path:
            log(f"{what}: no media path for running item {item_id}", LOG_WARNING)
            continue
        # The session carries the runtime too — use it if PlaybackInfo did not.
        runtime = runtime or ticks_to_secs(now_playing.get("RunTimeTicks"))
        entry, created = warm_register(path, item_id, runtime)
        if created:
            found += 1
            log(f"{what}: {path} ({describe_budget(entry, runtime)})")
    return found


def adopt_running_playback():
    """Pick up playback that is already running when we start.

    A restart mid-movie would otherwise leave that file cold until the next
    PlaybackStart webhook — and there is none, because playback never stopped.
    We may also come up before Jellyfin does, so an unreachable API is retried
    rather than treated as "nothing playing".
    """
    for attempt in range(ADOPT_ATTEMPTS):
        if SHUTDOWN.is_set():
            return
        found = discover_playback("Startup: adopting playback already in progress")
        if found is not None:
            if not found:
                log("Startup: no playback in progress")
            return
        if attempt + 1 >= ADOPT_ATTEMPTS:
            log("Startup: cannot check for running playback (Jellyfin unreachable)",
                LOG_WARNING)
            return
        SHUTDOWN.wait(ADOPT_DELAY)


def poll_loop():
    """Ask /Sessions periodically instead of (or alongside) waiting for webhooks.

    Slower off the mark than a webhook — up to one interval passes before a new
    playback is noticed, and that is exactly when precaching is worth the most.
    As a companion to the webhook it costs one request per interval and catches
    playbacks whose webhook never arrived.
    """
    log(f"Polling /Sessions every {CFG.poll_interval}s")
    while not SHUTDOWN.is_set():
        SHUTDOWN.wait(CFG.poll_interval)
        if SHUTDOWN.is_set():
            return
        discover_playback("Poll: new playback")


# ==========================
# WEBHOOK SERVER
# ==========================
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

            item_id = data.get("ItemId", None)
            if not item_id:
                log("Webhook error: no ItemId", LOG_ERR)
                self.send_response(200)
                self.end_headers()
                return

            # NotificationType only exists if the user put it into the plugin's
            # payload template — never require it. Liveness comes from /Sessions
            # either way; a stop event just saves us waiting out the interval.
            ntype = data.get("NotificationType")
            if ntype == "PlaybackStop" or (ntype == "PlaybackProgress" and warm_covers(item_id)):
                log(f"Webhook {ntype} for ItemId: {item_id}")
                warm_wake(item_id)
                self.send_response(200)
                self.end_headers()
                return

            log(f"Webhook received for ItemId: {item_id}")

            # Precache in the background (reply immediately).
            threading.Thread(target=run_precache, args=(item_id,), daemon=True).start()

            self.send_response(200)
            self.end_headers()
        except Exception as e:
            log(f"ERROR in do_POST: {e}", LOG_ERR)
            self.send_response(500)
            self.end_headers()

    def log_message(self, *args):
        # Silence the default stderr access log; we do our own logging.
        pass


# ==========================
# SERVER START
# ==========================
def stop_server(server):
    warm_shutdown()
    if server is not None:
        # Must not run on the serve_forever() thread — that deadlocks.
        server.shutdown()


def main():
    if not CFG.api_key:
        log(
            "FATAL: no api_key configured. Copy jf-precache.conf.example to "
            "/etc/jf-precache/jf-precache.conf and set api_key.",
            LOG_ERR,
        )
        raise SystemExit(
            "jf-precache: no api_key configured (see /etc/jf-precache/jf-precache.conf)"
        )

    if not CFG.webhook and CFG.poll_interval <= 0:
        log(
            "FATAL: webhook is disabled and poll_interval is 0 — nothing would "
            "ever trigger a precache. Enable one of them.",
            LOG_ERR,
        )
        raise SystemExit("jf-precache: neither webhook nor polling is enabled")

    server = None
    if CFG.webhook:
        server = ThreadingHTTPServer((CFG.listen_host, CFG.listen_port), Hook)

    def on_signal(signum, _frame):
        log(f"Received signal {signum} — shutting down")
        threading.Thread(target=stop_server, args=(server,), daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    src = CFG.loaded_from or "defaults/env"
    keepwarm = (
        f"every {CFG.keepwarm_interval}s, budget {CFG.keepwarm_runtime_factor}x runtime"
        if CFG.keepwarm
        else "disabled"
    )
    trigger = f"webhook on {CFG.listen_host}:{CFG.listen_port}" if CFG.webhook else "no webhook"
    if CFG.poll_interval > 0:
        trigger += f" + polling every {CFG.poll_interval}s"
    log(
        f"jf-precache started ({trigger}; config: {src}, runs: {CFG.runs}, "
        f"keep-warm: {keepwarm}, trickplay: {'on' if CFG.trickplay else 'off'})"
    )

    # Off the main thread: the webhook endpoint must be up immediately, and this
    # may sit in a retry loop waiting for Jellyfin.
    threading.Thread(target=adopt_running_playback, daemon=True).start()
    if CFG.poll_interval > 0:
        threading.Thread(target=poll_loop, daemon=True).start()

    if server is not None:
        server.serve_forever()
    else:
        # Polling only: nothing to serve. Wait in slices so the signal handler
        # gets a turn regardless of how the interpreter treats an endless wait.
        while not SHUTDOWN.wait(1.0):
            pass
    log("jf-precache stopped")


if __name__ == "__main__":
    main()
