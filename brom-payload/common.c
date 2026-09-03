#include "common.h"

#include "debug.h"

/*
 * All payload I/O goes through the BROM's USB download function table at
 * 0xd2e4.  The previous build hardcoded five helper entry points from the
 * MT8163 tree; two of them (0xd1ff/0xd1cb) do not exist as dword helpers on
 * this BROM build and crash the payload on first call.  The table slots were
 * verified directly against the 8167 bootrom dump and match the primitives
 * mtkclient's generic stage1 resolves by pattern search (usbdl_put_data
 * 0x5eb3, usbdl_get_data 0x5e99).
 */

usbdl_io_t usbdl_put_data = 0;
usbdl_io_t usbdl_get_data = 0;

void brom_usb_init(void) {
    volatile uint32_t *table = (volatile uint32_t *)0xd2e4;

    /* usbdl_put_data = table[2] (0x5eb3, odd = Thumb); get_data = table[1]. */
    usbdl_put_data = (usbdl_io_t)table[2];
    usbdl_get_data = (usbdl_io_t)table[1];
}

void send_dword(uint32_t value) {
    uint32_t out = __builtin_bswap32(value);
    usbdl_put_data(&out, 4);
}

uint32_t recv_dword(void) {
    uint32_t in = 0;
    usbdl_get_data(&in, 4);
    return __builtin_bswap32(in);
}

int send_data(const void *buf, uint32_t len) {
    return usbdl_put_data((void *)buf, len);
}

int recv_data(void *buf, uint32_t len) {
    return usbdl_get_data(buf, len);
}
