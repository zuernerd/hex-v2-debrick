/* Resident flash-writer for the HEX-VII recovery procedure.
   Linked at 0x20018000 (SRAM2 — executable on I/S-bus while Flash is busy).
   Runs inside OTG_FS_IRQHandler context on the device.
   All commands are read from the 12-byte block at 0x2000CA48 directly.
   Any flash/CRC error = deliberate hang = host-side transfer timeout. */
typedef volatile unsigned int vu32;
typedef unsigned int u32;
typedef unsigned char u8;

#define FLASH_KEYR (*(vu32*)0x40023C04u)
#define FLASH_SR   (*(vu32*)0x40023C0Cu)
#define FLASH_CR   (*(vu32*)0x40023C10u)
#define CR_LOCK    (1u<<31)
#define SR_BSY     (1u<<16)
#define SR_ERRMSK  0x000001F2u   /* OPERR|WRPERR|PGAERR|PGPERR|PGSERR|RDERR */
#define CR_PG      (1u<<0)
#define CR_SER     (1u<<1)
#define CR_MER1    (1u<<2)
#define CR_SNB_M   (0x1Fu<<3)
#define CR_MER2    (1u<<15)
#define CR_STRT    (1u<<16)

#define CHUNK       ((vu32*)0x2000CA48u)
#define LOADER_DEST (*(vu32*)0x20014CE0u)
#define SYST_CSR    (*(vu32*)0xE000E010u)
#define SCB_AIRCR   (*(vu32*)0xE000ED0Cu)

extern u32 __bss_start[];
extern u32 __bss_end[];
extern u8  __flasher_end[];
static u32 erased_mask, vaddr, vlen, parked, sbase;

static void die(void){ for(;;){} }
static void wq(void){ while (FLASH_SR & SR_BSY) {} }
static u32 rd_msp(void){ u32 v; __asm__ volatile("mrs %0, msp":"=r"(v)); return v; }
static u32 rd_psp(void){ u32 v; __asm__ volatile("mrs %0, psp":"=r"(v)); return v; }

__attribute__((naked, used, section(".park")))
void park_label(void){ __asm__ volatile("b ."); }

/* Belt-and-suspenders: LOADER_ASM already patches Boot_MainInit (0x20001FA0)
   and Boot_MainLoop (0x20001FC4) in SRAM1 to 'b .'. This also redirects any
   hardware exception frame (even PC, xPSR T-bit set & IPSR==0) whose return
   address points into Flash or Bootloader SRAM2 into park_label. */
static void park_stacks(void)
{
    *(vu32*)0x20001FA0u = 0xE7FEE7FEu;
    *(vu32*)0x20001FC4u = 0xE7FEE7FEu;
    *(vu32*)0x200015B4u = 0xE7FEE7FEu;
    *(vu32*)0x20009EACu = 0xE7FEE7FEu;
    *(vu32*)0x1000B0B0u = 0u;
    for (int pass = 0; pass < 2; pass++) {
        u32 lo = pass ? rd_psp() : rd_msp();
        if (!lo || lo < 0x20001000u || lo > 0x20017000u) continue;
        u32 start = pass ? (lo - 0x100u) : lo;
        for (vu32 *p = (vu32*)start; p + 1 < (vu32*)0x20017000u; p++) {
            u32 w = p[0], x = p[1];
            int in_flash = (w >= 0x08000000u && w < 0x08100000u);
            int in_sram2 = (w >= 0x2001C000u && w < 0x2001E000u);
            if (((w & 1u) == 0u) && (in_flash || in_sram2)
                && (x & 0x01000000u) && ((x & 0x000001FFu) == 0u))
                p[0] = ((u32)park_label) & ~1u;
        }
    }
}

static u32 crc32(const unsigned char *p, u32 n){
    u32 c = 0xFFFFFFFFu;
    while (n--) { c ^= *p++; for (int i=0;i<8;i++) c = (c>>1) ^ (0xEDB88320u & (0u-(c&1u))); }
    return ~c;
}
static u32 sector_of(u32 a){
    if (a < 0x08010000u) return (a >> 14) & 3u;
    if (a < 0x08020000u) return 4u;
    if (a < 0x08100000u) return 5u  + ((a - 0x08020000u) >> 17);
    if (a < 0x08110000u) return 12u + ((a - 0x08100000u) >> 14);
    if (a < 0x08120000u) return 16u;
    return 17u + ((a - 0x08120000u) >> 17);
}
static u32 sector_end(u32 s){
    if (s < 4u)  return 0x08004000u + (s << 14);
    if (s == 4u) return 0x08020000u;
    if (s < 12u) return 0x08020000u + ((s - 4u)  << 17);
    if (s < 16u) return 0x08104000u + ((s - 12u) << 14);
    if (s == 16u) return 0x08120000u;
    return 0x08120000u + ((s - 16u) << 17);
}

__attribute__((section(".entry"), used, noinline))
void flasher_entry(void)
{
    u32 w = CHUNK[0];
    if ((w >> 16) != 0xC0DEu) return;
    u32 cmd = w & 0xFFFFu, arg = CHUNK[1];

    if (cmd == 1u) {                               /* install: clear BSS */
        for (u32 *p = __bss_start; p < __bss_end; ) *p++ = 0u;
        return;
    }
    if (cmd < 0x10u) return;
    switch (cmd) {
    case 0x10:                                     /* INIT */
        SYST_CSR = 0u;                             /* stop SysTick */
        if (!parked) { park_stacks(); parked = 1u; }
        /* Redirect any default 0x080001B7 vectors in SRAM1 VTOR (0x20000000) to park_label */
        for (int i = 16; i < 98; i++) {
            if (((vu32*)0x20000000u)[i] == 0x080001B7u)
                ((vu32*)0x20000000u)[i] = ((u32)park_label) | 1u;
        }
        __asm__ volatile("dsb":::"memory"); __asm__ volatile("isb");
        if (FLASH_CR & CR_LOCK) { FLASH_KEYR = 0x45670123u; FLASH_KEYR = 0xCDEF89ABu; }
        wq();
        FLASH_SR = SR_ERRMSK;                      /* clear any stale error flags */
        FLASH_CR = (FLASH_CR & ~(CR_PG|CR_SER|CR_MER1|CR_MER2|CR_SNB_M)) | (2u<<8);
        erased_mask = 0u;
        sbase = (((u32)__flasher_end) + 3u) & ~3u;
        LOADER_DEST = sbase;
        break;
    case 0x11: {                                   /* PROG staging -> arg */
        u32 n = (LOADER_DEST - sbase) & ~3u;
        u32 a = arg & ~3u, off = 0;
        while (off < n) {
            u32 s = sector_of(a + off);
            u32 e = sector_end(s), lim = a + n; if (e > lim) e = lim;
            __asm__ volatile("cpsid i":::"memory");
            if (!(erased_mask & (1u << s))) {
                wq();
                FLASH_CR = (FLASH_CR & ~(CR_PG|CR_SER|CR_MER1|CR_MER2|CR_SNB_M)) | CR_SER | (s<<3);
                FLASH_CR |= CR_STRT;
                wq();
                FLASH_CR &= ~CR_SER;
                if (FLASH_SR & SR_ERRMSK) { __asm__ volatile("cpsie i"); die(); }
                erased_mask |= 1u << s;
            }
            for (; a + off < e; off += 4) {
                u32 v = *(vu32*)(sbase + off);
                if (v != 0xFFFFFFFFu) {
                    wq(); FLASH_CR |= CR_PG;
                    *(vu32*)(a + off) = v;
                    wq(); FLASH_CR &= ~CR_PG;
                }
                if ((FLASH_SR & SR_ERRMSK) || *(vu32*)(a + off) != v) {
                    __asm__ volatile("cpsie i"); die();
                }
            }
            __asm__ volatile("cpsie i":::"memory");
        }
        LOADER_DEST = sbase;
        break;
    }
    case 0x12:                                     /* ERASE(arg = sector) */
        __asm__ volatile("cpsid i":::"memory");
        wq();
        FLASH_CR = (FLASH_CR & ~(CR_PG|CR_SER|CR_MER1|CR_MER2|CR_SNB_M)) | CR_SER | ((arg&0x1Fu)<<3);
        FLASH_CR |= CR_STRT;
        wq();
        FLASH_CR &= ~CR_SER;
        if (FLASH_SR & SR_ERRMSK) { __asm__ volatile("cpsie i"); die(); }
        erased_mask |= 1u << (arg & 0x1Fu);
        __asm__ volatile("cpsie i":::"memory");
        break;
    case 0x13: vaddr = arg; break;                 /* VADDR */
    case 0x18: vlen = arg; break;                  /* VLEN */
    case 0x14: if (crc32((const unsigned char*)vaddr, vlen) != arg) die(); break; /* VGO */
    case 0x15: __asm__ volatile("dsb":::"memory"); SCB_AIRCR = 0x05FA0004u; for(;;){} break;
    default: break;
    }
}
