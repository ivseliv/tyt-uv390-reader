/* Exercise the USB reader against the stock read transaction without a device. */
#include <assert.h>
#include <libusb.h>

static int fake_transfer(libusb_device_handle *, uint8_t, uint8_t, uint16_t, uint16_t,
                         unsigned char *, uint16_t, unsigned);
#define libusb_control_transfer fake_transfer
#define main real_reader_main
#include "../read_usb.c"
#undef main
#undef libusb_control_transfer

static unsigned char response[UNIVERSAL_BLOCKS * BLOCK_SIZE];
static unsigned state = STATE_IDLE;
static unsigned pages, acknowledgements;
static const char *scenario;
static bool started, ended;

static int fake_transfer(libusb_device_handle *device, uint8_t dir, uint8_t request,
                         uint16_t index, uint16_t iface, unsigned char *data,
                         uint16_t length, unsigned timeout)
{
    assert(!device && !index && !iface && timeout == TIMEOUT_MS);
    switch (request) {
    case DFU_DNLOAD:
        assert(dir == 0x21 && state == STATE_IDLE && length == 2);
        if (!started) {
            assert(data[0] == 0xda && data[1] == 0x01);
            started = true;
            if (strcmp(scenario, "deny") == 0)
                return LIBUSB_ERROR_ACCESS;
        } else {
            assert(data[0] == 0x91 && data[1] == 0x05 && !ended);
            ended = true;
            if (strcmp(scenario, "end-out-stall") == 0)
                return LIBUSB_ERROR_PIPE;
        }
        state = STATE_COMMAND_IDLE;
        return length;
    case DFU_UPLOAD:
        assert(dir == 0xa1 && started && !ended && state == STATE_IDLE);
        assert(length == BLOCK_SIZE && pages < UNIVERSAL_BLOCKS && acknowledgements == pages);
        memcpy(data, response + pages * BLOCK_SIZE, BLOCK_SIZE);
        ++pages;
        state = STATE_UPLOAD_IDLE;
        return strcmp(scenario, "short") == 0 && pages == 7 ? length - 1 : length;
    case DFU_GETSTATUS:
        assert(dir == 0xa1 && length == 6);
        if (strcmp(scenario, "begin-stall") == 0 && !pages)
            return LIBUSB_ERROR_PIPE;
        if (ended && strcmp(scenario, "end-status-stall") == 0)
            return LIBUSB_ERROR_PIPE;
        if (ended && strcmp(scenario, "end-disconnect") == 0)
            return LIBUSB_ERROR_NO_DEVICE;
        if (ended && strcmp(scenario, "end-timeout") == 0)
            return LIBUSB_ERROR_TIMEOUT;
        memset(data, 0, length);
        data[4] = (unsigned char)state;
        return length;
    case DFU_GETSTATE:
        assert(dir == 0xa1 && length == 1);
        data[0] = (unsigned char)state;
        return length;
    case DFU_ABORT:
        assert(dir == 0x21 && length == 0);
        if (state == STATE_UPLOAD_IDLE)
            ++acknowledgements;
        state = STATE_IDLE;
        return 0;
    default:
        assert(0);
        return LIBUSB_ERROR_INVALID_PARAM;
    }
}

int main(int argc, char **argv)
{
    assert(argc == 2);
#ifdef _WIN32
    assert(_setmode(_fileno(stdin), _O_BINARY) >= 0);
#endif
    assert(prepare_output());
    scenario = argv[1];
    assert(fread(response, 1, sizeof(response), stdin) == sizeof(response));
    bool ok = read_universal_metadata(NULL, uv390_name);
    bool failed_start = strcmp(scenario, "deny") == 0 || strcmp(scenario, "begin-stall") == 0;
    bool successful = strcmp(scenario, "success") == 0 || strcmp(scenario, "end-disconnect") == 0
                      || strcmp(scenario, "end-status-stall") == 0;
    assert(ok == successful);
    assert(started && ended == !failed_start && acknowledgements == pages);
    assert(pages == (failed_start ? 0 : strcmp(scenario, "short") == 0 ? 7 : UNIVERSAL_BLOCKS));
    return ok ? 0 : 1;
}
