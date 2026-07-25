#ifndef IPOD_TOUCH_CONSOLE_TAP_H
#define IPOD_TOUCH_CONSOLE_TAP_H

#include "qemu/osdep.h"

/*
 * A read-only tap on the guest's serial console, used by the S5L8900 machines
 * to react to things the guest ANNOUNCES about itself.
 *
 * It exists because of the TVOut swap-device workaround: that hack has to know
 * the address of a kernel heap object, and the kernel prints that address
 * ("AppleMBX: Added swap device: AppleH1TVOut  id: c09c8400"). Deriving the
 * address from the guest's own announcement beats hard-coding a per-build
 * constant, which drifts silently between kernels and boards.
 *
 * The tap is inert unless a machine installs it, never modifies the byte
 * stream, and is called with the BQL held from the UART transmit path.
 */
void ipod_touch_console_tap_install(void (*fn)(const char *line));
void ipod_touch_console_tap_byte(uint8_t ch);

#endif /* IPOD_TOUCH_CONSOLE_TAP_H */
