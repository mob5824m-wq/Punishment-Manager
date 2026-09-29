# CubeClash

A pixelated 3D voxel sandbox that runs in the browser — **Creative** and **Survival** modes,
destructible/placeable blocks, blocky mobs, a day/night cycle with night waves, screen shake,
particles, a local high-score table, and full keyboard + touch controls.

## Run

Any static file server works (ES modules need http, not `file://`):

```bash
cd game
python3 -m http.server 8080
# open http://localhost:8080
```

three.js is loaded from a CDN (jsDelivr → unpkg → esm.sh fallback chain), so the first load
needs a network connection. No build step, no dependencies to install.

## Controls

| Action | Keyboard / Mouse | Touch |
| --- | --- | --- |
| Move | `W A S D` | left stick |
| Look | mouse (pointer lock) | drag anywhere on the right |
| Jump / fly up | `Space` | ▲ |
| Descend (fly) | `Shift` | — |
| Mine / attack | hold LMB | hold ⛏ or tap the world |
| Place block | RMB | ＋ |
| Select block | `1`–`8`, wheel, `Q`/`E` | ◀ ▶ or tap the hotbar |
| Toggle fly (creative) | `F` | ✈ |
| Pause | `P` / `Esc` | ❚❚ |
| Restart (on game over) | `R` | button |

## Scoring

* Mine a block: **+2** (`+25` glow, `+40` obsidian)
* Place a block: **+1** (creative `+2`)
* Clash a mob: **+25** Clashling, **+60** Spiker, **+140** Brute, **+10** Cubit
* Survive a night: **+60 × wave**

Top 8 runs are kept in `localStorage` under `cubeclash.highscores.v1`.

## Layout

```
game/
  index.html      shell, HUD, screens, CDN bootstrap
  style.css       pixel UI theme
  src/blocks.js   block registry + procedurally painted 64×64 texture atlas
  src/world.js    terrain generation, chunk meshing with baked AO, DDA raycast
  src/player.js   player physics (auto step-up, coyote time, jump buffer) + input
  src/mobs.js     blocky mobs, AI, animation, ray picking
  src/fx.js       particle pool, screen shake, WebAudio blips
  src/main.js     game states, loop, scoring, waves, day/night, juice
```

## Performance notes

The scene renders at a fraction of the display resolution into a canvas upscaled with
`image-rendering: pixelated` — that is both the art direction and the reason it holds 60fps on
phones. If the measured framerate drops below 45, the pixel scale automatically increases.
World geometry is one merged mesh per 16×40×16 chunk (~76k triangles for the whole 96×96 world),
rebuilt only for the chunks touched by a block edit (~7ms).
