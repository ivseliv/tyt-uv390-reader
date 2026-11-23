/* Stock MD-UV380/390 normal-mode codeplug transactions (qdmr, TyTInterface).
   The erase geometry is 64 KiB; the smaller transfer size is not an erase unit.
   This header is private to read_usb.c, so the CLI remains a single C translation unit. */
enum { SECTOR_PAGES = 64, GUARDS = 6, SECTOR_COUNT = 11,
       STOCK_BLOCKS = GUARDS + SECTOR_PAGES * SECTOR_COUNT };
static const uint32_t guard_addresses[GUARDS] = {0x2000, 0xec00, 0xf000, 0xf400, 0xf800, 0xfc00};
static const uint32_t sector_addresses[SECTOR_COUNT] = {
    0x010000, 0x030000, 0x110000, 0x120000, 0x130000, 0x140000,
    0x150000, 0x160000, 0x170000, 0x180000, 0x190000
};

struct stock_page {
    unsigned char before[BLOCK_SIZE], after[BLOCK_SIZE];
};
struct stock_plan {
    struct stock_page pages[STOCK_BLOCKS];
    bool touched[SECTOR_COUNT];
};

static uint32_t page_address(unsigned index)
{
    if (index < GUARDS)
        return guard_addresses[index];
    unsigned page = index - GUARDS;
    return sector_addresses[page / SECTOR_PAGES] + (page % SECTOR_PAGES) * BLOCK_SIZE;
}

static uint32_t little_u32(const unsigned char *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static void usb_binding(libusb_device *device, unsigned char result[12])
{
    memset(result, 0, 12);
    result[0] = libusb_get_bus_number(device);
    result[1] = libusb_get_device_address(device);
    unsigned char ports[9];
    int count = libusb_get_port_numbers(device, ports, (int)sizeof(ports));
    if (count > 0 && count <= (int)sizeof(ports)) {
        result[2] = (unsigned char)count;
        memcpy(result + 3, ports, (size_t)count);
    }
}

static bool stock_read(libusb_device_handle *radio, unsigned index, unsigned char data[BLOCK_SIZE])
{
    unsigned char status[6];
    uint32_t address = page_address(index);
    if (!read_block(radio, (uint16_t)(address / BLOCK_SIZE + 2), data, BLOCK_SIZE) ||
        !check_length(libusb_control_transfer(radio, 0xa1, DFU_GETSTATUS, 0, DFU_INTERFACE,
                                              status, sizeof(status), TIMEOUT_MS), sizeof(status),
                      "stock read status")) {
        fprintf(stderr, "Failed to read stock page 0x%06" PRIx32 ".\n", address);
        return false;
    }
    unsigned delay = status[1] | (unsigned)status[2] << 8 | (unsigned)status[3] << 16;
    if (status[0] || (status[4] != STATE_IDLE && status[4] != STATE_UPLOAD_IDLE) || delay > TIMEOUT_MS) {
        fprintf(stderr, "Unexpected read status %u, state %u, delay %u at 0x%06" PRIx32 ".\n",
                status[0], status[4], delay, address);
        return false;
    }
    if (delay)
        pause_ms(delay);
    /* The programming session needs only DMR ID and RX groups from sector zero. */
    if (index == 0) {
        memset(data, 0, 0x84);
        memset(data + 0x87, 0, BLOCK_SIZE - 0x87);
    } else if (index == 1) {
        memset(data, 0, 32);
    }
    return true;
}

static bool read_write_baseline(libusb_device_handle *radio, const char *model,
                                const unsigned char binding[12])
{
    if (!session_command(radio, SELECT_ADDRESS_ZERO) ||
        fwrite("TYTBAS01", 1, 8, stdout) != 8 || fwrite(model, 1, 8, stdout) != 8 ||
        fwrite(binding, 1, 12, stdout) != 12 || !output_u32(STOCK_BLOCKS))
        return false;
    unsigned char data[BLOCK_SIZE];
    for (unsigned i = 0; i < STOCK_BLOCKS; ++i) {
        if (!stock_read(radio, i, data) || !output_u32(page_address(i)) ||
            fwrite(data, 1, sizeof(data), stdout) != sizeof(data))
            return false;
    }
    return idle(radio) && fflush(stdout) == 0;
}

/* Reserved bits on an occupied record survive edits. Changes to their stock
   defaults are possible when creating a record, and can be undone exactly. */
static unsigned char planned_byte(const struct stock_plan *plan, unsigned sector,
                                  uint32_t address, bool after)
{
    unsigned offset = address - sector_addresses[sector];
    /* A contact / scan record may straddle a sector boundary. All such
       boundaries here lie inside the consecutive table range. */
    const struct stock_page *page = &plan->pages[GUARDS + sector * SECTOR_PAGES + offset / BLOCK_SIZE];
    return after ? page->after[offset % BLOCK_SIZE] : page->before[offset % BLOCK_SIZE];
}

static bool empty_slot(const struct stock_plan *plan, unsigned sector, uint32_t name, bool after)
{
    unsigned char a = planned_byte(plan, sector, name, after);
    unsigned char b = planned_byte(plan, sector, name + 1, after);
    return a == b && (a == 0 || a == 0xff);
}

static bool allowed_change(const struct stock_plan *plan, unsigned sector, uint32_t address,
                           unsigned char before, unsigned char after)
{
    if ((address >= 0x10000 && address < 0x18860) || (address >= 0x31000 && address < 0x3eac0))
        return true; /* RX groups (54-250), zones and their A/B channel lists. */
    uint32_t offset, name;
    unsigned char mask, initial;
    if (address >= 0x18860 && address < 0x1edf0) {
        offset = (address - 0x18860) % 104;
        if (offset < 38 || offset >= 42)
            return true;
        static const unsigned char scan_defaults[4] = {0xf1, 0x14, 0x08, 0xff};
        mask = 0;
        initial = scan_defaults[offset - 38];
        name = address - offset;
    } else if (address >= 0x110000 && address < 0x13ee00) {
        offset = (address - 0x110000) % 64;
        if (offset >= 32)
            return true;
        /* Channel fields exported by rdt_editor and cps_channel. */
        static const unsigned char masks[32] = {
            0x9f,0xff,0xff,0x8b,0xd3,0xf4,0xff,0xff,0x3f,0xff,0xff,0xff,0xff,0x1f,0xff,0xff,
            0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xf3,0x1f
        };
        static const unsigned char defaults[32] = {
            0x61,0x14,0,0xe0,0x24,0xc0,0,0,4,0,0,0,0,0,0,1,
            0,0,0,0x40,0,0,0,0x40,0xff,0xff,0xff,0xff,0,0,0xff,0xff
        };
        mask = masks[offset];
        initial = defaults[offset];
        name = address - offset + 32;
    } else if (address >= 0x140000 && address < 0x197e40) {
        offset = (address - 0x140000) % 36;
        if (offset != 3)
            return true;
        mask = 0x23;
        initial = 0xc0;
        name = address + 1;
    } else {
        return false;
    }
    unsigned reserved = (unsigned)(before ^ after) & (unsigned char)~mask;
    if (!reserved)
        return true;
    bool was_empty = empty_slot(plan, sector, name, false);
    bool now_empty = empty_slot(plan, sector, name, true);
    return was_empty != now_empty && (((now_empty ? before : after) ^ initial) & reserved) == 0;
}

static bool parse_plan(FILE *input, struct stock_plan *plan, const char *model,
                       const unsigned char binding[12])
{
    unsigned char header[32];
    if (fread(header, 1, sizeof(header), input) != sizeof(header) || memcmp(header, "TYTWRT01", 8) ||
        memcmp(header + 8, model, 8) || memcmp(header + 16, binding, 12) ||
        little_u32(header + 28) != STOCK_BLOCKS) {
        fprintf(stderr, "Plan is invalid or the USB attachment/model changed. Read a new preview.\n");
        return false;
    }
    for (unsigned i = 0; i < STOCK_BLOCKS; ++i) {
        unsigned char address[4];
        struct stock_page *page = &plan->pages[i];
        if (fread(address, 1, 4, input) != 4 || little_u32(address) != page_address(i) ||
            fread(page->before, 1, BLOCK_SIZE, input) != BLOCK_SIZE ||
            fread(page->after, 1, BLOCK_SIZE, input) != BLOCK_SIZE) {
            fprintf(stderr, "Truncated plan or unexpected page address.\n");
            return false;
        }
    }
    if (fgetc(input) != EOF || ferror(input)) {
        fprintf(stderr, "Invalid plan length.\n");
        return false;
    }
    for (unsigned i = 0; i < STOCK_BLOCKS; ++i) {
        const struct stock_page *page = &plan->pages[i];
        if (memcmp(page->before, page->after, BLOCK_SIZE) == 0)
            continue;
        for (unsigned j = 0; j < BLOCK_SIZE; ++j) {
            if (page->before[j] == page->after[j])
                continue;
            uint32_t address = page_address(i) + j;
            if (i < GUARDS || !allowed_change(plan, (i - GUARDS) / SECTOR_PAGES, address,
                                             page->before[j], page->after[j])) {
                fprintf(stderr, "Plan changes a protected field at 0x%06" PRIx32 ".\n", address);
                return false;
            }
        }
        plan->touched[(i - GUARDS) / SECTOR_PAGES] = true;
    }
    return true;
}

static bool compare_pages(libusb_device_handle *radio, const struct stock_plan *plan,
                          unsigned start, unsigned count, bool after)
{
    unsigned char data[BLOCK_SIZE];
    for (unsigned i = start; i < start + count; ++i) {
        if (!stock_read(radio, i, data))
            return false;
        if (memcmp(data, after ? plan->pages[i].after : plan->pages[i].before, BLOCK_SIZE)) {
            fprintf(stderr, "%s at page 0x%06" PRIx32 ".\n",
                    after ? "Read-back mismatch" : "Radio contents changed since preview", page_address(i));
            return false;
        }
    }
    return idle(radio);
}

static bool program_sector(libusb_device_handle *radio, struct stock_plan *plan, unsigned sector,
                           bool *erase_attempted)
{
    unsigned start = GUARDS + sector * SECTOR_PAGES;
    uint32_t base = sector_addresses[sector];
    if (!compare_pages(radio, plan, start, SECTOR_PAGES, false) ||
        !session_command(radio, ENTER_READ_SESSION))
        return false;
    pause_ms(100);
    unsigned char erase[] = {0x41, (unsigned char)base, (unsigned char)(base >> 8),
                             (unsigned char)(base >> 16), (unsigned char)(base >> 24)};
    *erase_attempted = true;
    fprintf(stderr, "WRITE_START 0x%06" PRIx32 "\n", base);
    fflush(stderr);
    if (!check_length(libusb_control_transfer(radio, 0x21, DFU_DNLOAD, 0, DFU_INTERFACE, erase,
                                              sizeof(erase), TIMEOUT_MS), sizeof(erase), "erase stock sector") ||
        !finish_command(radio, "erase stock sector", false) || !session_command(radio, SELECT_ADDRESS_ZERO))
        return false;
    for (unsigned i = start; i < start + SECTOR_PAGES; ++i) {
        uint16_t block = (uint16_t)(page_address(i) / BLOCK_SIZE + 2);
        int sent = libusb_control_transfer(radio, 0x21, DFU_DNLOAD, block, DFU_INTERFACE,
                                           plan->pages[i].after, BLOCK_SIZE, TIMEOUT_MS);
        if (!check_length(sent, BLOCK_SIZE, "program stock page") ||
            !finish_command(radio, "program stock page", false)) {
            fprintf(stderr, "Programming page 0x%06" PRIx32 " failed.\n", page_address(i));
            return false;
        }
    }
    if (!compare_pages(radio, plan, start, SECTOR_PAGES, true))
        return false;
    fprintf(stderr, "WRITE_VERIFIED 0x%06" PRIx32 "\n", base);
    return true;
}

static bool apply_settings(libusb_device_handle *radio, const char *model,
                           const unsigned char binding[12], FILE *input)
{
    struct stock_plan *plan = calloc(1, sizeof(*plan));
    if (!plan) {
        perror("allocate stock plan");
        return false;
    }
    bool ok = parse_plan(input, plan, model, binding) && session_command(radio, SELECT_ADDRESS_ZERO)
              && compare_pages(radio, plan, 0, STOCK_BLOCKS, false);
    unsigned verified = 0;
    for (unsigned sector = 0; ok && sector < SECTOR_COUNT; ++sector) {
        if (!plan->touched[sector])
            continue;
        bool erase_attempted = false;
        ok = program_sector(radio, plan, sector, &erase_attempted);
        if (ok) {
            ++verified;
        } else if (erase_attempted) {
            fprintf(stderr, "Sector 0x%06" PRIx32 " may be incomplete. Retain the backup. "
                            "%u preceding sectors verified.\n", sector_addresses[sector], verified);
        } else if (verified) {
            fprintf(stderr, "%u preceding sectors already verified. Retain the backup.\n", verified);
        }
    }
    if (ok && verified) {
        ok = session_command(radio, REBOOT_RADIO);
        if (!ok)
            fprintf(stderr, "All %u changed sectors verified. The final radio restart is unconfirmed.\n", verified);
    }
    if (ok)
        fprintf(stderr, "WRITE_COMPLETE %u\n", verified);
    free(plan);
    return ok;
}
