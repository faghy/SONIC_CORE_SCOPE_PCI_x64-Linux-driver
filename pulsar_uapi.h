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

/*
 * Mixer controls backed by DSP values (e.g. the LINVOL "Vol" input of a module). Each channel is a
 * 2-word sync value slot in DSP data memory; the kernel writes a 1.31 gain into both words.
 * Range -60..0 dB in 0.5 dB steps, minimum = mute.
 */
#define PULSAR_MAX_CONTROLS    4

struct pulsar_ctl_desc {
	char name[44];                            /* ALSA control name, e.g. "PCM Playback Volume" */
	__u32 dsp;
	__u32 channels;                           /* 1 or 2 */
	__u32 addr[2];                            /* DM address of each channel's value slot */
	__s32 init_cdb;                           /* initial gain in 0.01 dB (-6000..0) */
};

struct pulsar_controls {
	__u32 count;
	struct pulsar_ctl_desc ctl[PULSAR_MAX_CONTROLS];
};

/* Raw host->DSP message frame(s) for the command FIFO, serialized with the kernel's own SetValue */
#define PULSAR_MSG_MAX_WORDS   62

struct pulsar_msg {
	__u32 count;
	__u32 words[PULSAR_MSG_MAX_WORDS];
};

#define PULSAR_IOCTL_MAGIC     'P'
#define PULSAR_IOCTL_GET_INFO  _IOR(PULSAR_IOCTL_MAGIC, 0x01, struct pulsar_info)
#define PULSAR_IOCTL_SET_ROUTE _IOW(PULSAR_IOCTL_MAGIC, 0x02, struct pulsar_pcm_route)
#define PULSAR_IOCTL_SET_CONTROLS _IOW(PULSAR_IOCTL_MAGIC, 0x03, struct pulsar_controls)
#define PULSAR_IOCTL_SEND_MSG  _IOW(PULSAR_IOCTL_MAGIC, 0x04, struct pulsar_msg)

/*
 * Host-RAM delay lines = SCOPE's "PC delay atoms" (docs/pc_delay.md, kernel side pulsar_delay.c).
 * write_slot: capture slot (A - 0xC000) / 2 of the DSP sync output that feeds the delay's input.
 * tap_slot:   playback slots; the DSP inputs fed by tap k are linked to DM 0xC000 + 2 * tap_slot[k].
 * Delays are in samples at the current rate (the Sim2k pad value after base.dll's fs/48000 scaling).
 * Needs a valid PCM route (block size). All delays are freed when the hwdep device is closed.
 */
#define PULSAR_DELAY_MAX_TAPS  16

#define PULSAR_DELAY_4K        0      /* "PC Master 4k Delay":  0x1000-word ring, 8 taps, 194..4096     */
#define PULSAR_DELAY_32K       1      /* "PC Master 32k Delay": 0x8000-word ring, 8 taps, 194..32768    */
#define PULSAR_DELAY_256K      2      /* "PC 256k Delay": 1 output, 194..0x3fc00 (host copy beyond 2 blocks) */
#define PULSAR_DELAY_ER        3      /* "PC Early Reflection": 1 output, 16 taps with gains (host computed) */

struct pulsar_delay_alloc {
	__u32 kind;                               /* PULSAR_DELAY_* */
	__u32 write_slot;                         /* 0x40..0x17f */
	__u32 ntaps;                              /* playback slots: 4K/32K 1..8, 256K/ER 1 */
	__u16 tap_slot[PULSAR_DELAY_MAX_TAPS];    /* in: 0 = kernel picks; out: slots used (0x180..0x1ff) */
	__s32 delay[PULSAR_DELAY_MAX_TAPS];       /* initial delays (ER: the 16 reflections) */
	__u32 handle;                             /* out */
	__u32 reserved[3];
};

#define PULSAR_DELAY_P_DELAY   0      /* value = samples; clamped as scScope.sys does */
#define PULSAR_DELAY_P_SOURCE  1      /* value = BAR SRAM dword 0x800..0x1fff written by a DSP async output
					 (export header 0x63E00000 | dword), polled every block; 0 = host */
#define PULSAR_DELAY_P_GAIN    2      /* ER: gain of reflection `index`, 1.31 */
#define PULSAR_DELAY_P_NTAPS   3      /* ER: number of reflections 0..16 */

struct pulsar_delay_param {
	__u32 handle;
	__u32 param;                              /* PULSAR_DELAY_P_* */
	__u32 index;                              /* tap (ER: reflection) */
	__s32 value;
};

#define PULSAR_IOCTL_DELAY_ALLOC _IOWR(PULSAR_IOCTL_MAGIC, 0x10, struct pulsar_delay_alloc)
#define PULSAR_IOCTL_DELAY_FREE  _IOW(PULSAR_IOCTL_MAGIC, 0x11, __u32)
#define PULSAR_IOCTL_DELAY_PARAM _IOW(PULSAR_IOCTL_MAGIC, 0x12, struct pulsar_delay_param)

#endif /* _PULSAR_UAPI_H_ */
