#include "hw/arm/ipod_touch_lcd_panel.h"
#include "hw/arm/ipod_touch_lcd.h"

static uint32_t ipod_touch_lcd_panel_transfer(SSIPeripheral *dev, uint32_t value)
{
    IPodTouchLCDPanelState *s = IPOD_TOUCH_LCD_PANEL(dev);

    /* MIPI DCS 0x10 is Sleep In. The guest sends it from
     * AppleMerlotLCD::_lcdEnable(false), well before the PMU finally removes
     * application-processor power. RAM may still contain status-bar pixels,
     * but a sleeping physical panel no longer scans them out. */
    if (!s->cur_cmd && value == 0x10 && s->lcd) {
        /* If the device was interactive when the panel slept (a lock, not a
         * boot overlay), a later Sleep Out may reopen input immediately. */
        s->lcd->relight_input_fast = s->lcd->input_ready;
        s->lcd->panel_off = true;
        s->lcd->input_ready = false;
        s->lcd->input_ready_frames = 0;
        s->lcd->invalidate = 1;
        fprintf(stderr, "[LCD] Merlot panel entered sleep\n");
        return 0;
    }

    /* MIPI DCS 0x11 is Sleep Out. During the lock phase the guest stays
     * fully awake and relights the panel from AppleMerlotLCD::_lcdEnable(1)
     * when Power/Home is pressed; without this the OS wakes but the host
     * surface stays black. */
    if (!s->cur_cmd && value == 0x11 && s->lcd) {
        s->lcd->panel_off = false;
        s->lcd->invalidate = 1;
        if (s->lcd->relight_input_fast) {
            s->lcd->relight_input_fast = false;
            s->lcd->input_ready = true;
        }
        fprintf(stderr, "[LCD] Merlot panel woke from sleep\n");
        return 0;
    }

    if(!s->cur_cmd && (value == 0x95 || value == 0xDA || value == 0xDB || value == 0xDC)) {
        // this is a command -> set it
        s->cur_cmd = value;
        return 0x0;
    }

    if(s->cur_cmd) {
        uint32_t res = 0;
        switch(s->cur_cmd) {
        case 0x95:
            res = 0x1;
            break;
        case 0xDA:
            res = 0x71;
            break;
        case 0xDB:
            res = 0xC2;
            break;
        case 0xDC:
            res = 0x0;
            break;
        default:
            break;
        }

        s->cur_cmd = 0;
        return res;
    }
    
    return 0x0;
}

static void ipod_touch_lcd_panel_realize(SSIPeripheral *d, Error **errp)
{

}

static void ipod_touch_lcd_panel_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    k->realize = ipod_touch_lcd_panel_realize;
    k->transfer = ipod_touch_lcd_panel_transfer;
}

static const TypeInfo ipod_touch_lcd_panel_type_info = {
    .name = TYPE_IPOD_TOUCH_LCD_PANEL,
    .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(IPodTouchLCDPanelState),
    .class_init = ipod_touch_lcd_panel_class_init,
};

static void ipod_touch_lcd_panel_register_types(void)
{
    type_register_static(&ipod_touch_lcd_panel_type_info);
}

type_init(ipod_touch_lcd_panel_register_types)
