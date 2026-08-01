# jf-precache

A RAM cache for Jellyfin playback: when playback starts, this small webhook
service prefetches the media file by reading it sequentially a few times, so
it lands in the filesystem cache in RAM — the **ZFS ARC** on ZFS, the regular
**Linux page cache** everywhere else (ext4, XFS, Btrfs, …) — and keeps it warm
while you watch. Seeking and scrubbing are then served instantly from RAM
instead of the spinning hard drives.

Two to three sequential read passes promote the blocks into the
"frequently used" part of the cache (the ARC's **MFU** list on ZFS, the active
LRU list in the page cache), which is the last thing evicted under memory
pressure.

One sizing rule applies to both backends: **everything playing in parallel has
to fit into RAM at the same time.** The cache can only keep what fits — if two
streams together exceed it, the keep-warm passes start coming from disk again,
and the service detects that and gives the file up rather than hammering the
disks (see below).

## How it works

1. On "Playback Start", Jellyfin sends a webhook (JSON with `ItemId`) to
   `http://127.0.0.1:9000`. If you would rather not set up the plugin, set
   `poll_interval` instead and let the service query the `Sessions` API.
2. `jf-precache` checks via the `Sessions` API that the item is actually
   playing, and resolves the file path via `PlaybackInfo`.
3. The file is read sequentially `runs` times with `dd`, promoting the blocks
   into the frequently-used part of the cache.
   Optionally (`trickplay = yes`) the `.trickplay` directory is streamed through
   `tar | dd` as well — off by default, because those images often live on an
   SSD anyway and would only take up cache space the media file needs.
4. **Keep-warm:** while playback is running, the file is re-read once every
   `keepwarm_interval` seconds. Without this, the beginning of a long movie
   loses its recency over the runtime and gets evicted from the cache under
   memory pressure — exactly when you skip back.
   If `trickplay` is on, it is included in every pass.
5. End of playback, a long pause, or a crashed client stop the keep-warm.

On startup, the service queries `/Sessions` once and adopts playback that is
already running. Otherwise a file would stay cold after a restart mid-movie —
there is no `PlaybackStart` webhook for it anymore, since playback never
stopped. If Jellyfin is not up yet, this is retried a few times.

Whether something is still playing is decided by the **`Sessions` API**, not by
the webhook. So it does not matter which events the webhook plugin sends, and a
missing stop event (client crash, network) does not keep the service reading.
The same check prevents a template that also fires on stop or progress events
from triggering a full precache every time. If an item exists in several
versions (4K + 1080p under one item ID), the session also reveals which one is
actually playing.

Deduplication is **per file**: several sessions on the same file result in one
warming job, not several. Duplicate webhooks are no-ops.

Two built-in brakes keep the warming from loading the disks instead of
relieving them: a pass that obviously came from disk (file larger than the cache)
stretches the wait so that at most ~25% of wall-clock time is spent reading —
and after three such passes in a row the file is given up on. "From disk" here
means: slower than 1 GiB/s **and** clearly slower than the last precache pass,
which is warm by definition. Both are throughputs rather than fixed second
thresholds, because a 20 GiB remux and a 500 MiB episode cannot be caught by
the same duration.

How long a file is kept warm is tied to the **runtime of the media**: 1.5x by
default. A 45-minute episode gets a good hour, a 3.5-hour movie a good five.
There is no fixed upper bound — it would have no sensible value for both cases.
If Jellyfin reports no runtime (live TV), the session alone decides when to
stop.

## Cache backend: page cache or ZFS ARC

No configuration is needed to pick one — the service just reads files, and
whatever filesystem they live on decides where they get cached.

On **non-ZFS filesystems** it works out of the box: the page cache grabs all
otherwise-free RAM without any tuning. It is, however, purely opportunistic —
there is no floor, no priority knob, and anything can evict it at any time.
Inside a container (LXC/Docker) it also counts against the cgroup memory
limit: a container capped at 2 GiB can never cache more than 2 GiB of movie.

The **ZFS ARC is the recommended backend** because it is the only one that can
be made to *hold on* to the pre-warmed data: it has a configurable ceiling and
floor, its eviction priority relative to the page cache is tunable, and on a
Proxmox host it is not subject to any container's memory limit. The trade-off:
those knobs default to values that work against this service, so they need to
be set once — see below.

## ZFS: making the ARC actually keep what was pre-warmed

Pre-warming is useless if the ARC gives the blocks right back under the next
bit of memory pressure. Four knobs decide this — all on the **host**, not in
the container: with Proxmox/LXC the container only reads, the ARC belongs to
the host.

| Knob | Recommendation | Why |
|---|---|---|
| `zfs_arc_max` | as high as you can afford | the file has to fit in the first place |
| `zfs_arc_shrinker_limit` | `0` | see below — OOM risk otherwise |
| `zfs_arc_shrinker_seeks` | `> 4`, e.g. `6` | priority over the page cache |
| MGLRU (`lru_gen/enabled`) | `0` | otherwise the MFU list is worthless |

**`zfs_arc_shrinker_limit`** caps how many pages the shrinker may free per
allocation attempt. It sounds like a brake against ARC collapse, but it is the
opposite: if the kernel cannot shrink the ARC fast enough under pressure, it
runs out of memory. `zfs(4)` says "To reduce OOM risk, this limit is applied
for kswapd reclaims only." `0` means no limit and is the default nowadays (as
documented in `zfs(4)` for OpenZFS 2.4); older setups often had a value like
`10000` here.

**`zfs_arc_shrinker_seeks`** is the relative cost of an ARC eviction.
`zfs(4)`: "Bigger values make ARC more precious and evictions smaller […]
**Value of 4 means parity with page cache.**" The default of `2` therefore
makes the ARC *cheaper* to evict than the page cache. If you want the
pre-warmed media data to stay, you have to go above 4; 6 has worked well.

**MGLRU must be off.** Otherwise `kswapd` reclaims regardless of recency, and
the MFU promotion from the repeated read passes — the entire premise of this
service — comes to nothing.

Make it permanent:

```bash
# /etc/modprobe.d/zfs.conf
options zfs zfs_arc_max=53687091200 zfs_arc_shrinker_seeks=6

# /etc/tmpfiles.d/zfs-mglru.conf
w- /sys/kernel/mm/lru_gen/enabled - - - - 0
```

**Both only take effect after a reboot** — and with root-on-ZFS the ZFS module
lives in the initramfs, which has to be rebuilt first, otherwise `modprobe.d`
never applies:

```bash
mkinitcpio -P                  # Arch
update-initramfs -u -k all     # Debian / Proxmox
reboot
```

Without a reboot only part of it works: `zfs_arc_max`, `zfs_arc_shrinker_limit`
and MGLRU can be written to `/sys/…` at runtime. `zfs_arc_shrinker_seeks` is
**read-only** there and can only be set when the module loads — there is no way
around the reboot.

Check that it took effect:

```bash
grep . /sys/module/zfs/parameters/zfs_arc_{max,shrinker_limit,shrinker_seeks}
cat /sys/kernel/mm/lru_gen/enabled          # 0x0000 = off
arcstat 5                                  # hit rate during playback
```

If `arc_no_grow` stays set permanently, or the ARC never grows beyond a small
fraction despite free RAM, one of these four settings is wrong.

None of this section applies to non-ZFS filesystems.

## Configuration

The Jellyfin API key has to be entered in the config file. The package
installs the template directly to `/etc/jf-precache/jf-precache.conf`.

The key is used for `PlaybackInfo` **and** `Sessions`; both are read-only
calls, a regular admin API key is all it takes.

The file is marked as pacman `backup`: on an upgrade, a locally modified config
is not overwritten — a `.pacnew` is placed next to it instead, so migrate new
options via `pacdiff`. If they are missing from an old config, the built-in
defaults apply and the service keeps running.

Search order: `$JF_PRECACHE_CONFIG` → `/etc/jf-precache/jf-precache.conf` →
`~/.config/jf-precache/jf-precache.conf`. Every value can additionally be
overridden by an environment variable (`JF_URL`, `JF_API_KEY`, `JF_BS`,
`JF_RUNS`, `JF_WEBHOOK`, `JF_LISTEN_HOST`, `JF_LISTEN_PORT`,
`JF_POLL_INTERVAL`, `JF_TRICKPLAY`, `JF_KEEPWARM`, `JF_KEEPWARM_INTERVAL`,
`JF_KEEPWARM_RUNTIME_FACTOR`, `JF_REQUIRE_SESSION`, `JF_API_TIMEOUT`).

## Service

```bash
sudo systemctl enable --now jf-precache
journalctl -fu jf-precache         # everything; -p err for errors only
```

Logging goes to stdout and thus into the journal — there is no separate log
file (and accordingly nothing to rotate).

On a package upgrade the service restarts itself: the package ships a pacman
hook that marks the unit as `needs-restart`, whereupon systemd's
`enqueue-marked` hook restarts it. A deliberately stopped service stays stopped
(`enqueue-marked` behaves like `try-restart`). If playback is running at that
moment, it is picked up again via the `Sessions` API after the restart.

## Trigger: webhook or polling

Both can be toggled independently; at least one has to be on, otherwise the
service refuses to start.

| | `webhook = yes` (default) | `poll_interval = 30` |
|---|---|---|
| Reaction | immediately on playback start | up to one interval later |
| Effort | set up the plugin | nothing, just the API key |
| Cost | none | one request per interval |

The webhook is the faster variant, and the start of playback is exactly when
pre-warming is worth the most — skip ahead right away and you hit a cold file
otherwise. That is why it stays the default.

Polling is the alternative for anyone who does not want to configure the plugin
(`webhook = no`), but it also works **in addition** as a safety net: it catches
playbacks whose webhook got lost or whose payload template is broken. Nothing
gets precached twice, the per-file dedupe takes care of that.

## Jellyfin webhook

Install the **Webhooks** plugin, point a "Generic Destination" at
`http://127.0.0.1:9000`, notification type *Playback Start*, payload as JSON
with at least `ItemId`.

Optional but recommended: include `NotificationType` in the template and enable
*Playback Stop* as well. The keep-warm then ends immediately instead of at the
next interval. It is not required — without these fields the service notices
the end via the `Sessions` API.

## Limitations

The service only pays off when the source is **significantly slower than
RAM** — spinning disks, a busy pool, network storage. On an NVMe you will
barely measure a difference: there the filesystem already delivers several
GiB/s cold, more than scrubbing ever needs. If your media lives on an SSD, you
do not need this.

If the storage mount hangs (network filesystem in D state), the running `dd`
is uninterruptible; neither timeout nor runtime budget applies then.

## Dependencies

- `python` (≥ 3.9), `python-requests`
- `coreutils` (`dd`), `tar`

## Building the package (Arch)

The repo ships a `PKGBUILD` that builds straight from the checkout:

```bash
git clone https://github.com/Erdnusschokolade/jf-precache.git
cd jf-precache
makepkg -si
```

On other distributions it is enough to copy `jf-precache.py` to `/usr/bin/`
and `jf-precache.service` to `/etc/systemd/system/`; the pacman hook
`10-jf-precache-mark-for-restart.hook` is Arch-specific and has no function
elsewhere.

## License

MIT — see [LICENSE](LICENSE).
