/* Memory-backed model of the stock sector erase/program interface.
   Even if a production address check regresses, this executable has no USB path. */
#include <assert.h>
#include <libusb.h>
static int fake_transfer(libusb_device_handle *, uint8_t, uint8_t, uint16_t, uint16_t,
                         unsigned char *, uint16_t, unsigned);
#define libusb_control_transfer fake_transfer
#define main unused_reader_main
#include "../read_usb.c"
#undef main
#undef libusb_control_transfer

static unsigned char flash[0x1a0000];
static unsigned state = STATE_IDLE;
static unsigned erases, programs, uploads, reboots;
static uint32_t next_address;
static bool selected, armed, awaiting_status;
static const char *scenario;

static bool simulation(const char *name)
{
    return strcmp(scenario, name) == 0;
}

static bool safe_sector(uint32_t a)
{
    return (a == 0x10000 || a == 0x30000 ||
            (a >= 0x110000 && a < 0x1a0000)) && a % 65536 == 0;
}

static int fake_transfer(libusb_device_handle *device, uint8_t dir, uint8_t request,
                         uint16_t index, uint16_t iface, unsigned char *data,
                         uint16_t length, unsigned timeout)
{
    assert(!device && !iface && timeout == TIMEOUT_MS);
    if (awaiting_status)
        assert(request == DFU_GETSTATUS);
    switch (request) {
    case DFU_DNLOAD:
        assert(dir == 0x21 && state == STATE_IDLE);
        if (index == 0) {
            if (length == 5 && data[0] == 0x21) {
                assert(little_u32(data + 1) == 0);
                selected = true;
            } else if (length == 2 && data[0] == 0x91 && data[1] == 1) {
                armed = true;
            } else if (length == 5 && data[0] == 0x41) {
                next_address = little_u32(data + 1);
                assert(armed && safe_sector(next_address));
                armed = selected = false;
                ++erases;
                if (simulation("refuse-erase"))
                    return LIBUSB_ERROR_ACCESS;
                memset(flash + next_address, 0xff, 65536);
            } else {
                assert(length == 2 && data[0] == 0x91 && data[1] == 5 && programs && !reboots);
                ++reboots;
            }
        } else {
            uint32_t addr = (index - 2u) * 1024;
            assert(index >= 2 && length == BLOCK_SIZE && selected && erases && !armed);
            assert(addr == next_address && safe_sector(addr & ~65535u));
            ++programs;
            if (simulation("short-program") && programs == 6)
                return BLOCK_SIZE - 1;
            memcpy(flash + addr, data, BLOCK_SIZE);
            next_address += 1024;
        }
        state = STATE_COMMAND_IDLE;
        awaiting_status = true;
        return length;
    case DFU_UPLOAD: {
        uint32_t addr = (index - 2u) * 1024;
        assert(dir == 0xa1 && index >= 2 && length == BLOCK_SIZE && selected);
        assert(state == STATE_IDLE || state == STATE_UPLOAD_IDLE);
        assert(addr == 0x2000 || (addr >= 0xec00 && addr < 0x10000) || safe_sector(addr & ~65535u));
        ++uploads;
        if (simulation("short-read") && uploads == 710)
            return length - 1;
        if (simulation("late-stale") && uploads == 711)
            flash[addr + 15] ^= 0x80;
        if (simulation("second-stale") && erases == 1 && addr == 0x110000)
            flash[addr + 7] ^= 1;
        if (simulation("bad-readback") && programs == 64 && addr == 0x1fc00)
            flash[addr + 1023] ^= 1;
        memcpy(data, flash + addr, length);
        state = STATE_UPLOAD_IDLE;
        awaiting_status = true;
        return length;
    }
    case DFU_GETSTATUS:
        assert(dir == 0xa1 && !index && length == 6 && awaiting_status);
        awaiting_status = false;
        memset(data, 0, length);
        data[4] = (unsigned char)state;
        if (simulation("bad-status") && programs == 1)
            data[0] = 1;
        if (simulation("end-disconnect") && reboots)
            return LIBUSB_ERROR_NO_DEVICE;
        return length;
    case DFU_GETSTATE:
        assert(dir == 0xa1 && !index && length == 1);
        data[0] = (unsigned char)state;
        return length;
    case DFU_ABORT:
        assert(dir == 0x21 && !index && !length);
        state = STATE_IDLE;
        return 0;
    default:
        assert(0);
        return LIBUSB_ERROR_INVALID_PARAM;
    }
}

int main(int argc, char **argv)
{
    assert(argc == 2 && prepare_output() && prepare_plan_input());
    scenario = argv[1];
    /* Unrelated bytes inside and outside a rewritten sector; CRT-sensitive values. */
    flash[0x2001] = 0xee;
    flash[0xec01] = 0xdd;
    flash[0x2084] = 26;
    flash[0x2085] = 10;
    flash[0x1fd11] = 26;
    flash[0x1fd12] = 10;
    flash[0x110020] = 'C';
    flash[0x140004] = 'Q';
    flash[0x18860] = 'S';
    const unsigned char binding[12] = {1,2,2,4,1};
    bool ok;
    if (simulation("baseline"))
        ok = read_write_baseline(NULL, uv390_name, binding);
    else {
        if (simulation("stale"))
            flash[0x19fffe] ^= 1;
        fputs("WRITE_PREFLIGHT\n", stderr);
        ok = apply_settings(NULL, uv390_name, binding, stdin);
        /* After the complete sector is programmed the untouched bytes must survive. */
        if (ok)
            assert(flash[0x1fd11] == 26 && flash[0x1fd12] == 10);
    }
    fprintf(stderr, "MOCK_RESULT %u %u %u %u\n", erases, programs, uploads, reboots);
    return ok ? 0 : 1;
}
