# HEX-V2 (STM32F429) USB Recovery Tool

USB de-brick / recovery tool for STM32F429-based HEX-V2 (`0483:A0CB`) interfaces via the stock USB HID bootloader—no SWD/JTAG hardware or case opening required.

In case you got a cheap HEX-V2 clone with a sticker saying "B03-ST (429)" and used a VIILoader.exe version higher than V08.23.1, your interface probably got bricked (short blue blink on power-up and then nothing). This tool might help you recover it.  

You have to provide your own firmware image (`firmware.bin`) to flash onto the device. You might be able to find it [here](https://www.hackster.io/PsySc0rpi0n/inside-the-hex-vii-vcds-clone-a-full-stm32f429-memory-dump-287b7f) or else extract it from a working device. Be careful if you use a firmware dump from a different source.

## How it works

This tool interacts with the stock USB HID bootloader of the STM32F429 microcontroller on the HEX-V2 interface. It allows you to hold the bootloader in RAM and flash a new firmware image without requiring SWD/JTAG hardware or opening the device case. The process involves:

1. Probing the USB device to check its enumeration mode.
2. Verifying the exploit and holding the bootloader in RAM.
3. Flashing the provided firmware image to the device, with optional protection for the bootloader sectors.

## Prerequisites

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. **Windows:** Bind the **WinUSB** driver to USB device `0483:A0CB` using [Zadig](https://zadig.akeo.ie/).

## Usage

1. **Check USB enumeration mode:**
   ```bash
   python hexv2_flash.py probe
   ```
   *(If the device enumerates cyclically every ~0.7s, add `--chase` to subsequent commands.)*

2. **Verify exploit / hold bootloader in RAM:**
   ```bash
   python hexv2_flash.py test --sc hold [--chase]
   ```

3. **Flash firmware image:**
   ```bash
   python hexv2_flash.py flash --image <firmware.bin> [--chase]
   ```
   By default, Sectors 0–4 (`0x08000000`–`0x0801FFFF`, bootloader + LKT stub) are protected and only Sectors 5–7 (`0x08020000`+) are written. Pass `--overwrite-bootloader` to flash the entire 512 KB image.

## Rebuilding `flasher.bin` (Optional)

A prebuilt `flasher.bin` is included. To rebuild from source:

```bash
arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -Os -ffreestanding -fno-builtin -nostdlib -T hexv2_flasher.ld hexv2_flasher.c -o flasher.elf
arm-none-eabi-objcopy -O binary flasher.elf flasher.bin
```

## Credits
- [drive2.ru post](https://www.drive2.ru/b/722056708737732189/) ([Google Translate link](https://www-drive2-ru.translate.goog/b/722056708737732189/?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=de&_x_tr_pto=wapp)): Original source describing the HEX-V2 clones and bricking issue.
- [Hackster.io post](https://www.hackster.io/PsySc0rpi0n/inside-the-hex-vii-vcds-clone-a-full-stm32f429-memory-dump-287b7f): Source for the firmware image and memory dump of the HEX-V2 clone.