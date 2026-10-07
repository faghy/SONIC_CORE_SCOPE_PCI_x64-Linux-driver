/* SPDX-License-Identifier: GPL-2.0-or-later WITH Linux-syscall-note */
/*
 * Userspace interface of snd-pulsar's hwdep device (/dev/snd/hwC<n>D0).
 *
 * As on Windows (scScope.sys IOCTL 0x1d2048), the DSP boot is driven from
 * userspace: mmap() the hwdep device at offset 0 to get the whole 4 MB BAR0.
 */

#ifndef _PULSAR_UAPI_H_
#define _PULSAR_UAPI_H_

#include <linux/types.h>
#include <linux/ioctl.h>

struct pulsar_info {
	__u32 board_raw_id;
	__u32 board_rev;
	__u32 bar_len;
	__u32 irq_count;
	__u32 last_int_status;
	__u32 reserved[3];
};

#define PULSAR_IOCTL_MAGIC     'P'
#define PULSAR_IOCTL_GET_INFO  _IOR(PULSAR_IOCTL_MAGIC, 0x01, struct pulsar_info)

#endif /* _PULSAR_UAPI_H_ */
