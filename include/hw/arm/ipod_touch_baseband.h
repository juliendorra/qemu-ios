#ifndef IPOD_TOUCH_BASEBAND_H
#define IPOD_TOUCH_BASEBAND_H

#include "qemu/osdep.h"
#include "chardev/char.h"
#include "qom/object.h"

/*
 * Infineon S-Gold2 (PMB8876) baseband stub for the iPhone 2G (M68AP).
 *
 * The baseband is attached to UART1 and speaks plain AT commands (see
 * openiboot radio-pmb8876/radio.c). The vibrator has no GPIO of its own on
 * the M68AP: it hangs off the baseband and is driven with at+xdrv=4,...
 * commands. This stub acknowledges every command so the guest's radio
 * bring-up does not stall waiting for "OK", and reports an empty baseband
 * NVRAM.
 */

#define TYPE_CHARDEV_SGOLD2 "chardev-sgold2"

struct SGold2State {
    Chardev parent;

    char line[256];
    int line_len;
    uint8_t outbuf[1024];
    int outlen;
    bool xcallstat_enabled;
};
typedef struct SGold2State SGold2State;

DECLARE_INSTANCE_CHECKER(SGold2State, SGOLD2_CHARDEV, TYPE_CHARDEV_SGOLD2)

#endif
