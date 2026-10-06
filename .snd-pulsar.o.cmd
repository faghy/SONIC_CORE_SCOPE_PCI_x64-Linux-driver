savedcmd_/home/faghy/puksar2/linux-driver/snd-pulsar.o := x86_64-linux-gnu-ld -m elf_x86_64 -z noexecstack --no-warn-rwx-segments   -r -o /home/faghy/puksar2/linux-driver/snd-pulsar.o @/home/faghy/puksar2/linux-driver/snd-pulsar.mod  ; ./tools/objtool/objtool --hacks=jump_label --hacks=noinstr --hacks=skylake --ibt --orc --retpoline --rethunk --sls --static-call --uaccess --prefix=16  --link  --module /home/faghy/puksar2/linux-driver/snd-pulsar.o

/home/faghy/puksar2/linux-driver/snd-pulsar.o: $(wildcard ./tools/objtool/objtool)
