#include "common.h"

/*
 * MT8516 stage-1 runs immediately after the BROM handler hijack.  Do not use
 * the hardware UART here: its clock/pin mux is not guaranteed at BROM stage,
 * and waiting for its TX-ready bit before restoring the BROM USB path wedges
 * the payload.  Stage 1 is deliberately USB-only.
 */

int main(void) {
    /* Restore the BROM USB TX function overwritten by the v1 handler hijack. */
    int (*(*usbdl_ptr))(void) = (void *)0xd2e4;
    *(volatile uint32_t *)(usbdl_ptr[0] + 8) = (uint32_t)usbdl_ptr[2];

    /* Complete the pending control transfer, then announce over USB CDC. */
    send_usb_response(1, 0, 1);
    send_dword(0xA1A2A3A4);

    while (1) {
        uint32_t magic = recv_dword();
        if (magic != 0xf00dd00d) {
            continue;
        }

        switch (recv_dword()) {
        case 0x4000: {
            uint32_t address = recv_dword();
            uint32_t size = recv_dword();
            send_dword(recv_data(address, size, 0) == 0 ? 0xD0D0D0D0 : 0xF0F0F0F0);
            break;
        }
        case 0x4001: {
            void (*jump_address)(void) = (void *)recv_dword();
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
