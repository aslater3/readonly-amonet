#ifndef _COMMON_H_
#define _COMMON_H_

#include <inttypes.h>

/*
 * BROM USB download function table (verified against the hwcode-0x8167 BROM
 * at 0xd2e4; see kamakiri_v1.py and the bootrom dump analysis):
 *   usbdl_ptr[0]  0x1029a4  pointer used as ptr_send by the v1 hijack
 *   usbdl_ptr[2]  0x5eb3    usbdl_put_data(buf, len)  (Thumb)
 *   usbdl_ptr[1]  0x5e99    usbdl_get_data(buf, len)  (Thumb)
 * The addresses below are resolved once at runtime by brom_usb_init().
 */

typedef int (*usbdl_io_t)(void *buf, uint32_t len);

extern usbdl_io_t usbdl_put_data;
extern usbdl_io_t usbdl_get_data;

void brom_usb_init(void);

/* byte-oriented helpers shared by stage1 and stage2 */
void send_dword(uint32_t value);
uint32_t recv_dword(void);
int send_data(const void *buf, uint32_t len);
int recv_data(void *buf, uint32_t len);

#endif
