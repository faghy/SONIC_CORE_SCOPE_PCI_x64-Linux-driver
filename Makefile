# Makefile for Creamware / Sonic Core Pulsar 2 Linux ALSA Driver

obj-m += snd-pulsar.o
snd-pulsar-objs := pulsar_core.o pulsar_pcm.o pulsar_hwdep.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD := $(shell pwd)

default:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
