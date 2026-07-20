# Host Keyboard Input Plan

## Status

Future design work. This is intentionally not part of the DNS/proxy change.

The S5L8900 machines currently reserve the unmodified host keys `H` and `P`
for the emulated Home and Power buttons. Mapping every printable host key to a
tap on iPhone OS 1's visible software keyboard would make text entry easier,
but a naive implementation would make the letters `h` and `p` impossible to
type and silently break the existing shortcuts.

## Preferred UX direction

Add an explicit, user-visible **Host Typing Mode** instead of guessing whether
the guest keyboard is visible from framebuffer pixels. Expose it as a checked
application menu item with a non-printing keyboard shortcut, and show a small
temporary on-screen notice when the mode changes. This makes the active key
behavior discoverable and prevents an accidental mode change from looking like
lost input.

- Normal mode retains the current `H` = Home and `P` = Power behavior.
- A non-printing shortcut toggles host typing mode.
- In typing mode, printable host keys synthesize taps on the corresponding
  iPhone OS 1 software-keyboard keys.
- In typing mode, dedicated non-text keys such as `F1` and `F2` provide Home
  and Power so all device controls remain reachable.
- `Escape` leaves typing mode. This provides an obvious recovery path even if
  the user does not remember the toggle shortcut.
- The toggle and control keys should be configurable and documented in the
  application UI or help text.
- The implementation must cover letters, Shift/caps, numbers, symbols,
  delete, space, and Return/Go before it is enabled by default.

## Rejected shortcuts

- Always translating printable keys: conflicts with the existing `H` and `P`
  controls.
- Detecting the guest keyboard from framebuffer pixels: theme-, animation-,
  orientation-, and localization-dependent.
- Permanently moving Home and Power without a compatibility mode: needlessly
  breaks established emulator controls.
- Automatically entering typing mode after a screen tap: the emulator cannot
  reliably know whether the guest accepted the tap or displayed a keyboard.

## Questions to resolve before implementation

- Choose a toggle chord that QEMU receives consistently on macOS and that does
  not collide with common guest or application shortcuts.
- Decide whether the coordinate maps cover only the bundled US keyboard first
  or whether localization must be complete before release.
- Define behavior for key repeat, dead keys, composed characters, paste, and
  hardware layouts that do not match the visible guest layout.
- Decide how the checked menu state and temporary on-screen notice are exposed
  by both the standalone QEMU window and the packaged app.

## Validation

- Existing Home/Power behavior is unchanged when typing mode is off.
- `h` and `p` type correctly when typing mode is on.
- Home and Power remain available in typing mode.
- All supported iPhone OS 1 keyboard layouts have explicit coordinate maps;
  unknown layouts fail closed instead of tapping arbitrary screen positions.
