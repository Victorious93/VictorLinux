# Benchmark & auto-tune

One short benchmark picks the settings that run the selected model fastest **on this PC** and saves them as a profile.

## Arch edition: Control Center → Tune
- Stop the model, then press **Run benchmark**. `llama-bench` runs once with a small grid:
  up to 3 GPU-layer values (the memory plan, a quarter fewer, and "all" when the plan already offloads nearly everything)
  × up to 3 thread counts (physical cores, half of them, logical CPUs). 256-token prompt, 64 generated tokens, 2 repeats.
- Winner: fastest generation, then fastest prompt, then fewer GPU layers.
- Context is not a speed setting. The profile stores the largest context that fits in memory, and at every start it is
  capped again by what fits right now.
- At start the profile is used only where you left settings on automatic (GPU layers -1, threads 0, context auto) and
  only for the same backend. Turn it off with the checkbox. **Forget** removes one or all profiles for this PC.

## Debian edition: `victor-ctl tune run|status|forget [--all]`, Tk tab **Tune**
Debian runs every layer on the GPU when one is usable and keeps the context you set, so only **threads** are tuned.
The profile is used while `threads` is 0. Disable with `victor-ctl llama config --set autotune_apply=0`.

## Per-PC profiles
Saved in `config/autotune.json` (Arch) / `settings/autotune.json` (Debian), mode 0600, keyed by a PC fingerprint
(SHA-256 of CPU model, GPU ids and RAM size, first 16 hex digits) and the model file name + size. A stick moved to
another PC never applies the first PC's numbers. Nothing leaves the PC.
