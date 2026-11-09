# blender — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#blender--blender-vse-layout).

Retiming/speed-strip behaviour is documented alongside the `<speed>` marker in
[intervals.md](intervals.md#keepspeedoverlaycut-markers).

## Proxies, built by ffmpeg (`blender/proxy.py`)

Blender builds a strip's proxy only when a person selects it and presses
Rebuild Proxy (there is no "build every proxy" in the UI), and its builder
encodes on the CPU. The proxy is an ordinary video file, so the `blender`
stage writes it itself **before** Blender starts (`stages._build_proxies`),
when `blender.use_proxy` and `blender.proxy_build` (both default true), one
`proxy_<proxy_size>.avi` per source. Blender finds it where it looks and uses
it; `proxy.use_overwrite` stays false, so it never rebuilds over it.

What Blender 5.2.2 writes — **measured, not documented** by Blender (its manual
covers the settings, not the file):

- `<source dir>/BL_proxy/<source file name>/proxy_<N>.avi` (per-strip storage),
  wherever the `.blend` lives — beside the **original** media, which is what
  the stage hands Blender;
- an **MP4** container holding **H.264** (High, yuv420p) despite the `.avi`
  name — the MJPEG/AVI proxies of older Blender are gone — no B-frames,
  keyframe interval 10 (x264 `keyint=10`), x264 crf 25 at the default
  quality 50, **no audio**; Blender 5.x has no timecode index files, so the
  video is all there is;
- the source's frame rate and **exact timestamps** — a variable-rate phone
  clip stays variable, a first frame after 0 stays there (a frame with a
  duplicate timestamp is dropped) — hence `-copyts -fps_mode passthrough
  -enc_time_base demux`: without them ffmpeg moves the first frame to 0 and
  snaps every timestamp to a 1/frame-rate grid; the frame **unrotated**, with the
  source's display-matrix rotation copied as metadata (so `-noautorotate`);
- `N` percent of the source size, `int(W * N / 100)` rounded **up** to even
  (1918x1078 at 75% → 1438x808, at 25% → 480x270).

Blender's x264 settings, read from the stream's SEI: `veryfast` plus
`-tune fastdecode` (`cabac=0`, deblocking off, `weightp=0`) — chosen for a
cheap decode, which is what a proxy is for.

`proxy.encode_cmd()` reproduces that on two independent axes:

- `blender.proxy_decode`: `cuda` (default) — NVDEC (`-hwaccel cuda
  -hwaccel_output_format cuda`, which works for any codec, unlike the
  per-codec `hevc_cuvid`; measured identical speed) and `scale_cuda` — or
  `cpu`.
- `blender.proxy_encoder`: `libx264` (default) with Blender's exact settings,
  frames brought down by `hwdownload`; or `h264_nvenc` (`p1`, `-cq 34 -b:v 0`,
  `-coder cavlc`), which reads the CUDA frames directly and names no
  `-pix_fmt` (that demands a CPU conversion of a CUDA surface and fails).

Why that default — measured on 60 s of a 1080p60 HEVC phone clip (RTX 3060,
16 cores; decode = single-thread decode of the proxy, the scrubbing cost):

| decode + encode | build | size | decode | SSIM |
|---|---|---|---|---|
| cpu + libx264 (Blender's own) | 9.3 s | 66 MB | 4.3 s | 0.971 |
| **cuda + libx264** | **6.5 s** | 66 MB | **4.3 s** | 0.971 |
| cuda + nvenc cq 34 cavlc | 5.4 s | 57 MB | 6.3 s | 0.971 |
| cuda + nvenc cq 25 (cabac) | 4.9 s | 146 MB | 15.2 s | 0.987 |

cuda + libx264 writes Blender's file 1.4x faster. NVENC builds a little
faster still but its proxy scrubs ~1.5x slower (no deblocking switch; CAVLC
only narrows the gap), and the build happens once while scrubbing happens all
edit long. At the same `-cq` NVENC spends far more bits than crf 25 does;
`cq 34` is where its SSIM meets crf 25's.

**`tests/blender/test_proxy_matches_blender.py`** has real Blender build a
proxy of a rotated clip and compares every stream property, the keyframe
positions and the x264 settings with ours, and the timestamps on a
variable-rate clip (cpu + libx264, since CI has no GPU) — it is the test that
fails when a Blender upgrade changes the format.

- **Why bother.** Blender's own builder is not slow (a 45 s 1080p60 HEVC
  phone clip: 7.6 s at 100%, 5.1 s at 25% on 16 cores). The gain is that
  every source gets its proxy with no clicking, plus NVDEC for the decode,
  which is the bottleneck (6.5 min clip: 67 s cpu → 33 s with the GPU). The proxy itself pays
  for scrubbing: that clip has a keyframe every 60 frames of HEVC, the proxy
  every 10 frames of H.264 (169 MB → 39 MB at 100%, 5.8 MB at 25%).
- **Host ffmpeg**, like host `blender`: the whisperx image has no NVDEC/NVENC (its
  compose service grants `capabilities: [gpu]`, not `video`) and mounts only
  the input dir, while the proxy must sit beside each original.
- A proxy is skipped while it is at least as new as its source
  (`is_fresh()`); a replaced source rebuilds it. The encode goes to
  `proxy_<N>.nagare_part.avi` and is renamed, so Blender never reads half a file.
- Any failing GPU attempt (no GPU, driver, NVENC session limit) is retried
  once as cpu + libx264 — Blender's own build; when that fails too, or ffprobe cannot read the source, the
  stage **warns and carries on** — Blender plays the original without a proxy.
- `scene.set_preview_proxy_size()` sets every sequencer space's
  `proxy_render_size` to `PROXY_<proxy_size>`: the Video_Editing template
  previews at `PROXY_100`, so any other size would look for a file never built.
- Not verified headlessly: that the preview *displays* the file (no GL in a
  background Blender). Check it by hand: overwrite a proxy with a solid-colour
  clip of the same shape and open the `.blend` — the preview turns that colour.

- `place_strips()` opens the source once as a muted template video+sound pair on high channels and duplicates it — a `new_movie()` per interval would open an FFmpeg decoder per strip (measured: 300 intervals, 16s that way vs. 0.8s by duplicating). Two things keep that duplication from going quadratic, because `sequencer.duplicate`/`connect` both cost more per call the more strips the scene already holds:
  - **The template pair is connected once, before any duplication** (`sequencer.connect`), and every copy inherits a connection to *its own* audio copy — never to the seed's. So one connect op covers all pairs instead of one per interval (verified for 4000 pairs), and `_duplicate_pairs()` uses that inherited connection to pair each new video with its own sound rather than trusting collection order.
  - **`allocate_pairs()` grows the pool by doubling**: each round duplicates the whole pool so far (clipped to what is still needed), reaching N pairs in `ceil(log2(N)) + 1` operator calls instead of N. Every remaining per-interval step is plain property assignment — no operator, no full-collection selection scan.
  Measured on a real 1080p60 source (whole stage, wall clock, one caption per interval): 400 intervals 2.1s → 0.9s, 800 7.7s → 1.1s, 1600 41.8s → 2.6s, 4000 (>10 min, timed out) → 15.3s. Old and new placement produce identical strip data (positions, offsets, durations, channels, mute, connections). Two costs are untouched and dominate at the extremes: a `<speed>` range still needs a per-strip `retiming_segment_speed_set` op pair (800 intervals, half retimed: 17.8s), and `place_captions()` scans the whole timeline map per caption — negligible at 1600 (1.2s) but 7.1s of that 4000-interval figure.
  Degenerate (`end <= start`) intervals are dropped before allocation but still consume their index, so `keep_NNNN` names stay aligned with the interval list.
- `place_overlays()` consumes `{start, duration, text}` entries: the start is mapped through `tl_map` (speed-aware, `break` on the interval containing it) and the strip length is `sec_to_frames(duration, fps)` — timeline frames are already post-cut and post-retime, so a stated duration *is* the viewer's reading time regardless of what was removed inside the window. The length is clamped to `max(tl_end)` of that source's map (a long duration near the end must not overlap the next source's strips), and an anchor on no keep interval is skipped with a warning. The intervals stage snaps an off-keep anchor forward to its line's first surviving moment (`snap_overlay_starts`), so this warning now means the line survived nowhere (or the JSON was hand-edited) — it is deliberately kept as the last line of defence. See [intervals.md](intervals.md#keepspeedoverlaycut-markers) for the marker itself and the snap rule.
- `place_speed_marks()` (when `blender.speed_mark.enabled`, default true) badges each speed range with `template.format(factor=...)` (default `"x{factor}"`) on `SPEED_MARK_CHANNEL = 3` — the lowest of the three text channels (`3` < `CAPTION_CHANNEL=4` < `OVERLAY_CHANNEL=5`), so a badge yields to overlapping captions/overlays.
- Caption/overlay/speed-mark style is `blender.caption_style.*` overlaid with per-feature `overlay_style`/`speed_mark` overrides; `resolve_speed_mark_style()` in `blender_cli.py` merges the style and strips non-style keys. Five layout keys (`font_size`/`alignment_x`/`anchor_y`/`location_x`/`location_y`, in `timeline._MAPPED_STYLE_KEYS`) have per-feature defaults applied explicitly by each placement function. **Every other style key is forwarded verbatim to the Blender TextStrip attribute of the same name** via `timeline.apply_text_style(text_strip, style)` (shared by `place_captions`/`place_overlays`/`place_speed_marks`) — so any `bpy.types.TextStrip` attribute (`shadow_color`/`shadow_offset`/`shadow_blur`/`shadow_angle`, `box_margin`/`box_roundness`, the older `color`/`use_shadow`/`use_outline`/`outline_*`/`use_box`/`box_color`/`wrap_width`, ...) is configurable with no code change. A key that isn't a valid strip attribute raises `AttributeError` on assignment (Blender RNA rejects unknown properties) and is logged-and-skipped. The one exception is **`font`**: the TextStrip `font` attribute is a `VectorFont` datablock (not a path string), so `apply_text_style` special-cases it via `timeline._load_font(path)` — the value must be an **absolute** filepath that exists; it's loaded with `bpy.data.fonts.load(path, check_existing=True)` and assigned. An invalid (non-absolute/non-str) or missing path is a **hard error** (`ValueError`/`FileNotFoundError`), failing the blender stage rather than silently falling back to the default font. The other exception is **colour**: a key named `color` or `*_color` (`is_color_key()` — a suffix rule, so a future TextStrip colour attribute needs no code change) whose value is a **string** is parsed as hex by `blender/color.py::parse_hex_color()` (`#RRGGBB`/`#RRGGBBAA`, 3/4-digit shorthand, optional `#`, case-insensitive, alpha defaults to `1.0`); a list is still forwarded verbatim. A malformed hex string is a **hard error** for the same reason `font` is — a wrong colour in a finished render is invisible, it just looks like a design decision. This exists because **Blender's clipboard lies about these values**: the TextStrip colour properties are `PROP_COLOR_GAMMA` (the RNA value *is* the sRGB/display value, which is why the verbatim list path shows exactly the numbers the picker's RGB sliders show), but `but_copy_color()` linearises a COLOR_GAMMA button on Ctrl-C (`srgb_to_linearrgb_v3_v3`) and `but_paste_color()` un-linearises on Ctrl-V — so copy/paste round-trips **inside** Blender while the text on the clipboard is not the RNA value, and pasting it into config renders visibly darker (mid grey `0.5` copies as `0.214`; alpha is untouched because the conversion is `_v3_v3`, which is the tell-tale). The colour picker's **Hex** field has no such conversion for a gamma button (`interface_region_color_picker.cc` skips it when `is_color_gamma_picker`), so hex is exactly `round(channel * 255)` and is the copy-paste-safe route. Guarded by `tests/blender/test_text_style.py` and `tests/blender/test_color.py`.
- Strip placement clamps each source frame range to the clip length; a 1-frame end overshoot at the clip tail (sec→frame rounding) is clamped silently at debug level; anything larger still warns (`blender/frames.py::clamp_frames`).
- **The placement cursor advances by the duration Blender really built**, read back off the strip (`timeline._strip_duration`) after retiming — never by a predicted one. `retiming_segment_speed_set` computes the retimed length itself, and a cursor that falls short of it starts the next strip *inside* its predecessor; Blender resolves that overlap by shunting the strip to a free channel (onto the speed-badge/caption channels) rather than refusing the assignment, and the timeline's total length is unaffected because Phase D re-pins every strip to its intended start — which is how it goes unnoticed. `build_timeline_map()` still needs a prediction (it runs before any strip exists), so both it and the placement loop use the shared `frames.retimed_frame_count()`, which rounds a half frame **away from zero** the way Blender's `round_fl_to_int` does. Python's `round()` rounds half to even, making `132 / 8 = 16.5` sixteen frames against Blender's seventeen: in a real 3-region 8x run, 11 of 260 strips landed on `.5`, the 5 that rounded down each left the following video+audio pair on channels 3 and 4, and nothing was logged. A built duration that still disagrees with the prediction is warned about — the timeline map, and so every caption/overlay in that range, is laid out against the prediction and would be off by the difference.
- Phase D also warns for any strip not on the channel it was assigned (`VIDEO_CHANNEL = 1` / `SOUND_CHANNEL = 2`), whatever the cause: a displaced strip is otherwise visible only by opening the `.blend` by hand.
- Multiple sources concatenate onto one timeline via repeated `--source`/`--intervals` and `start_cursor`/`idx_offset`. `scene.load_source_metadata()` falls back to `scene.FALLBACK_FPS` (30.0) when a source carries no usable fps — a module constant, not a config key, since an fps the project actually wants is `blender.render.fps`, which overrides the scene regardless of what was measured.
- **Render/output settings** (`blender.render`, empty by default) are written onto the scene by `render_settings.apply_render_settings(scene.render, cfg)`, so the `.blend` opens ready to render instead of needing the encoder/resolution/audio set by hand every time. Every key is forwarded 1:1 to the `scene.render` RNA attribute of the same name (`bpy.types.RenderSettings`) and a **dict** value recurses into the sub-struct of that name — which is why `image_settings` and `ffmpeg` need no special case, and why any render setting Blender exposes is reachable with no code change. Keys are applied in the **order written** (YAML mapping order survives `yaml.safe_load`), which matters where one setting widens another's choices: on Blender 5.x `image_settings.file_format` does not offer `FFMPEG` at all until `media_type: VIDEO` is set. Unknown **key** → `AttributeError` → logged and skipped (as `apply_text_style` does); invalid **value** (a misspelt enum member) → propagates as a hard error, because a codec silently other than the one asked for is a defect found only by playing the file. `filepath` is handed over untouched, so Blender's `//` (relative to the `.blend`, `..` included — the `.blend` sits in `output/blender/`, so `//../renders/x.mp4` is `output/renders/x.mp4`) works as it does in the UI; `~` is deliberately not expanded, since Blender does not read it that way. The module imports no `bpy` — every operation is a getattr/setattr on whatever object it is handed — so the whole contract is tested against `__slots__` fakes that reject an unknown attribute exactly as RNA does (`tests/blender/test_render_settings.py`), the same reason `frames.py` holds the placement arithmetic.
- **Ordering in `_build`:** the config is applied *after* the source-derived `fps`/`fps_base`/`resolution_x`/`resolution_y` (so a stated value overrides what was measured) and *before* `effective_fps = scene.render.fps / scene.render.fps_base` is read back (so an overridden fps carries through every frame computation the stage does). `fps` given without `fps_base` resets `fps_base` to `1.0`: the scene otherwise carries the first source's pulldown (`1.001` for 29.97) and `fps: 30` would quietly stay 29.97 — an override that does not override. The reset is top-level only; no render sub-struct has an `fps_base` to reach for.

## Its own warnings (`blender_warnings.json`)

The clamp and build-length warnings this stage logs are the only sign that a
requested interval did not fit the clip, or that Blender built a strip a
different length than `retimed_frame_count` predicted (which means the captions
in that range were laid out against the wrong map). They print into the same
stream as Blender's unrelated `bl_pkg`/`cattrs` extension tracebacks that the
operator prompt tells the operator to ignore, so in practice they scroll past.

`blender/warnings_file.py` (no bpy import) therefore captures them:
`main()` wraps the whole build in `capture_warnings()` — a WARNING-level
handler on the root logger, so it catches the module-level `logging.warning(...)`
calls `blender.timeline` makes throughout — and writes
`output/blender/blender_warnings.json` (`{"warnings": [...]}`) in a `finally`,
so a failed build still leaves what preceded it. The list is written **even when
empty**: a stale file from a previous run would otherwise report warnings this
scene never produced. The finished-cut report reads it back
(see [cut_report.md](cut_report.md)).

## Iterating the manifest

The placement loop walks the segments of `intervals/timeline.json` (`--manifest`)
rather than the sources. Absent, it falls back to the per-source loop, so a
project built before the order existed still resumes at `--from-stage blender`.
Each segment is that source's intervals JSON sliced to its window by
`frames.slice_intervals_data`. Scene fps/resolution come from whatever plays
first; the `.blend` filename stays the first source in **shooting** order, so a
reorder does not rename the project file.

See [`order.md`](order.md).
