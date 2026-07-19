#ifndef IPOD_TOUCH_MV8686_H
#define IPOD_TOUCH_MV8686_H

#include "qemu/osdep.h"
#include "qemu/timer.h"

/* Behavioral model of the Marvell 88W8686-family SDIO Wi-Fi card found in
 * the N45AP. The card side is deliberately separate from the S5L8900 host
 * controller model (Wi-Fi plan stage 2): this struct never touches MMIO.
 *
 * Address map facts come from the guest trace and, structurally, from the
 * Linux Libertas if_sdio driver; the guest remains authoritative. */

#define MV8686_CIS0_ADDR     0x1000
#define MV8686_CIS1_ADDR     0x2000
#define MV8686_IOPORT_ADDR   0x10000

#define MV8686_OCR           0x300000 /* 3.2-3.4V window */
#define MV8686_RCA           0x0001

#define MV8686_MAX_PKT       2048

typedef struct MV8686Packet {
    struct MV8686Packet *next;
    uint32_t len;                  /* includes the 4-byte SDIO header */
    uint8_t data[MV8686_MAX_PKT];
} MV8686Packet;

/* firmware bring-up progress. The Apple AppleMRVL868x driver loads a
 * bootstrapper ("helper"), reads the EEPROM through it, then loads the
 * main firmware. The EEPROM step is Apple-specific and absent in the
 * Linux libertas driver. */
typedef enum {
    MV8686_DL_HELPER = 0,   /* expecting [le32 size][data] helper chunks */
    MV8686_EEPROM_CMD,      /* helper booted; expecting the EEPROM request */
    MV8686_EEPROM_READ,     /* EEPROM response staged for the host to read */
    MV8686_DL_MAIN,         /* expecting raw main-firmware chunks */
    MV8686_FW_READY,        /* mailbox is live */
} MV8686DlState;

/* the driver's EEPROM read request is a fixed 16 bytes */
#define MV8686_EEPROM_CMD_LEN   16
#define MV8686_EEPROM_LEN       2048

typedef struct MV8686State {
    /* fn0: CCCR (0x00-0xFF) and FBR1 (0x100-0x1FF) register files */
    uint8_t cccr[0x100];
    uint8_t fbr1[0x100];
    /* fn1 control registers (Libertas-style mailbox window) */
    uint8_t fn1[0x100];
    bool selected;   /* CMD7 issued */
    bool io_reset;   /* last CMD52 write to CCCR 0x06 had RES set */

    uint8_t mac[6];
    uint16_t cmd_seq;             /* last guest command sequence number */
    bool radio_on;
    bool associated;
    bool deep_sleep;
    bool dnld_pending;
    QEMUTimer *wake_timer;

    /* A backend such as slirp can synchronously answer the guest's first
     * DHCP request before its ASSOCIATE command reaches the mailbox. */
    uint8_t deferred_frame[MV8686_MAX_PKT - 24];
    size_t deferred_frame_len;

    MV8686DlState dl_state;
    uint32_t helper_bytes;
    uint32_t main_bytes;

    /* EEPROM image staged for the readEEPROM handshake */
    uint8_t eeprom[MV8686_EEPROM_LEN];
    uint32_t eeprom_len;

    /* queue of card-to-host packets (cmd responses, events, rx data) */
    MV8686Packet *rx_head, *rx_tail;

    /* host notification hook, provided by the controller: called when the
     * card interrupt state may have changed */
    void (*set_card_irq)(void *opaque, int level);
    void *irq_opaque;

    /* stage-5 hook: transmit an Ethernet frame toward the net backend */
    void (*send_frame)(void *opaque, const uint8_t *frame, size_t len);
    void *net_opaque;
} MV8686State;

void mv8686_reset(MV8686State *c);
void mv8686_cleanup(MV8686State *c);

/* Execute an SD/SDIO command without data. Returns the 32-bit response
 * word for RESP0 (raw response bits 39:8, as the S5L8900 exposes it). */
uint32_t mv8686_exec_cmd(MV8686State *c, uint8_t cmd_idx, uint32_t arg);

/* CMD53 data transfer. For writes, buf holds the payload from the guest;
 * for reads the card fills buf. Returns true on success. */
bool mv8686_io_rw_extended(MV8686State *c, bool write, uint8_t fn,
                           uint32_t addr, uint8_t *buf, uint32_t len);

/* Deliver an Ethernet frame from the net backend to the guest. */
void mv8686_receive_frame(MV8686State *c, const uint8_t *frame, size_t len);

#endif
