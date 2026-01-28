---
name: maxmsp
description: Critical MaxMSP MCP rules for object placement, gotchas, and tool usage. Invoke before creating/modifying Max patches.
user-invocable: true
disable-model-invocation: false
---

# MANDATORY CHECKLIST - Follow Before EVERY Action

## BEFORE placing ANY object:
```
0. Consider whether the task at hand is best served by using a subpatcher.
1. Call get_avoid_rect_position() FIRST
2. Use returned [left, top, right, bottom] to calculate position
3. Place at y = bottom + 50 (minimum)
```
**NEVER skip this step. NEVER guess positions.**

## Required acknowledgment flags:

**Math objects** (`+`, `-`, `*`, `/`, `%`, `pow`, `scale`) and **pack/pak/unpack**:
- JSON strips `.0` from numbers! Use STRING args: `["0", "127", "0", "25."]`
- Strings with `.` are converted to floats, without `.` to ints
- For unpack, use `["f", "f", "f"]` (type specifier)
- Set `int_mode=True` if integer truncation is intentional
- Exception: `scale` with output range ≤ 2 auto-detects float intent

**dial** - use instead of live.dial, requires `@size`:
- Float 0-1: `['@size', 1, '@floatoutput', 1]`
- Bipolar -1 to 1: `['@min', -1, '@size', 2, '@floatoutput', 1, '@mode', 6]`
- Int 0-127: `['@size', 127]`
- **Max @size 255** - larger creates unusable UI; use `flonum`/`number` instead (bypass with `extend=True`)
- `live.dial` rejected (bypass with `use_live_dial=True`)

**trigger/t** - fires RIGHT-TO-LEFT:
- Set `trigger_rtl=True` to acknowledge
- `[t b f]` sends `f` FIRST, then `b`

**coll** - data doesn't persist unless embedded:
- Always include `@embed 1` in args: `['mycoll', '@embed', 1]`

---

# Signal Flow Rules

**Auto-summing**: MSP inlets automatically sum all incoming signals. Never use `+~` just to combine signals - connect them both to the same inlet instead.

**Delay feedback**: Connect feedback directly to `tapin~`, never through a mixer first.

---

# Placement Rules

After calling `get_avoid_rect_position()` → `[left, top, right, bottom]`:
- First object: `[left, bottom + 50]`
- Same row: x += previous_width + 25
- New row: x = left, y += 50

---

# Subpatchers

**Use subpatchers** for: effects, voices, drums, sequencers, mixers, modulation.

```
create_subpatcher([x, y], "varname", "display_name")
enter_subpatcher("varname")
  add inlet/outlet objects
  build internal logic
exit_subpatcher()
connect from parent
```

---

# MCP Tools

| Tool | Purpose |
|------|---------|
| `get_avoid_rect_position()` | **REQUIRED** before placing |
| `add_max_object(pos, type, var, args)` | Create object |
| `move_object(var, x, y)` | Reposition |
| `recreate_with_args(var, args)` | Change creation-time args |
| `get_object_connections(var)` | Get all connections |
| `create_subpatcher(pos, var, name)` | Create p object |
| `enter_subpatcher(var)` / `exit_subpatcher()` | Navigate subpatchers |

**line~ messages** - must have EVEN number of values (pairs):
- `[0, 0, 1, 500, 0, 500]` for instant→0, ramp→1 in 500ms, →0 in 500ms
- Odd count rejected (set `not_line_msg=True` to bypass)

---

# Message Boxes

- Use numbers: `[200, 0, 50]`
- NOT strings: `["200", "0", "50"]` (creates literal quotes)
- Fixed 70px width (user adjusts)
