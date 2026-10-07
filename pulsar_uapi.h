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

/*
 * Audio route, set by the userspace loader once the DSP graph is loaded (docs/pcm_streaming.md 5.2).
 * Playback slot s (0x180..0x1ff) feeds DSP DM 0xC000 + 2*s; capture slot s (0x40..0x17f) is the
 * DSP sync-output comm slot (A - 0xC000) / 2. The ALSA PCM device appears after the first valid route.
 */
#define PULSAR_MAX_CHANNELS    8

struct pulsar_pcm_route {
	__u32 rate;                               /* word clock, e.g. 44100 */
	__u32 block;                              /* frames per IRQ: 64..1024, power of two */
	__u16 play_slot[PULSAR_MAX_CHANNELS];
	__u16 cap_slot[PULSAR_MAX_CHANNELS];
	__u32 play_channels;
	__u32 cap_channels;
	__u32 flags;                              /* reserved, 0 */
};

#define PULSAR_IOCTL_MAGIC     'P'
#define PULSAR_IOCTL_GET_INFO  _IOR(PULSAR_IOCTL_MAGIC, 0x01, struct pulsar_info)
#define PULSAR_IOCTL_SET_ROUTE _IOW(PULSAR_IOCTL_MAGIC, 0x02, struct pulsar_pcm_route)

#endif /* _PULSAR_UAPI_H_ */
