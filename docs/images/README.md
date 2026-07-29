# Screenshots for the README

## In use

| File | Where |
|---|---|
| `titleleft.jpeg` | Top of the README, left — the real arm on a bench. |
| `titleright.png` | Top of the README, right — the same arm in the 3D view, one servo red. |

The pair is a two-column HTML table, so keep replacements at roughly the same
aspect ratio or one column will tower over the other.

The README links these by **absolute** `raw.githubusercontent.com` URL, not by
relative path. PyPI renders the same README on the project page but does not
resolve relative links, so a relative path shows a broken image to everyone
arriving from `pip`. Keep new images absolute for the same reason — and note the
URLs pin `main`, so an image only appears once it is pushed.

## Still open

Three slots, each marked in `README.md` by an `IMAGE PLACEHOLDER` comment. Drop
the file here under the name below, then delete the two comment markers around it
so the `![...]` line renders.

| File | Shot |
|---|---|
| `terminal-pass.png` | The terminal after a clean run — all six servos green, `✓ PASS` at the bottom. |
| `viz-all-green.png` | The 3D view once stage 2 finishes: six solid green servos on the ghosted body, instruction panel beside it. |
| `viz-motion-sweep.png` | The 3D view mid-sweep, with the encoder position plots moving. |

PNG, and crop to the window rather than the whole desktop — GitHub scales them to
the column width, so a full-screen capture makes the terminal text unreadable.
