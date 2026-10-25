ENTRY(flasher_entry)
SECTIONS
{
  . = 0x20018000;
  .text : { KEEP(*(.entry)) *(.park) *(.text*) *(.rodata*) . = ALIGN(4); }
  .bss (NOLOAD) : { __bss_start = .; *(.bss*) *(COMMON) . = ALIGN(4); __bss_end = .; }
  __flasher_end = .;
}
ASSERT(__flasher_end <= 0x2001A800, "flasher too big for SRAM2 staging window")
