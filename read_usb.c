#include <libusb.h>

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <windows.h>
#else
#include <time.h>
#endif

/* USB sequence and codeplug offsets reference qdmr v0.15.1. */
enum {
    VENDOR = 0x0483, PRODUCT = 0xdf11, DFU_INTERFACE = 0,
    BLOCK_SIZE = 1024, TIMEOUT_MS = 2000,
    DFU_DNLOAD = 1, DFU_UPLOAD = 2, DFU_GETSTATUS = 3, DFU_GETSTATE = 5, DFU_ABORT = 6,
    STATE_IDLE = 2, STATE_COMMAND_SYNC = 3, STATE_BUSY = 4,
    STATE_COMMAND_IDLE = 5, STATE_UPLOAD_IDLE = 9,
};

static const char product_name[] = "Digital Radio in USB mode";
static const char uv390_name[] = "MD-UV390";
static const char uv380_name[] = "MD-UV380";

/* End addresses are exclusive. The optional key banks come last in the stream. */
static const struct { uint32_t start, end; } regions[] = {
    {0x002084, 0x002087}, /* DMR ID */
    {0x00ec20, 0x01edf0}, /* RX groups, zones, scan lists */
    {0x031000, 0x03eac0}, /* A/B zone extensions */
    {0x110000, 0x197e40}, /* channels and contacts */
    {0x002000, 0x00200c}, /* timestamp and CPS version */
    {0x002040, 0x002068}, /* startup text */
    {0x002080, 0x002098}, /* public flags and timers */
    {0x0020ab, 0x0020ac},
    {0x0020b0, 0x0020f7}, /* name, additional radio IDs, menu */
    {0x002102, 0x002106}, {0x002111, 0x002112}, {0x002114, 0x002140},
    {0x002180, 0x005800}, /* messages before the shared block */
    {0x005c00, 0x005f80}, /* emergency systems after the shared block */
    {0x02ef00, 0x02ef80}, /* VFO A and B */
    {0x03ec40, 0x03ed40}, /* GPS systems */
};

/* All four slices fit in the single 1024-byte DFU block at 0x5800. */
static const struct { uint32_t start, end; } shared_regions[] = {
    {0x005800, 0x0059c0}, /* remaining message data */
    {0x005a70, 0x005c00}, /* emergency settings and first systems */
    {0x0059c0, 0x005a40}, /* Enhanced, raw slot order */
    {0x005a50, 0x005a70}, /* Basic, raw slot order */
};
enum { PRIVACY_REGIONS = 2 };

static bool check_usb(int result, const char *operation)
{
    if (result >= 0)
        return true;
    fprintf(stderr, "%s: %s (%d)\n", operation, libusb_error_name(result), result);
    return false;
}

static bool check_length(int result, uint16_t expected, const char *operation)
{
    if (!check_usb(result, operation))
        return false;
    if (result == expected)
        return true;
    fprintf(stderr, "%s: expected %u bytes, received %d\n", operation, expected, result);
    return false;
}

static void pause_ms(long milliseconds)
{
#ifdef _WIN32
    Sleep((DWORD)milliseconds);
#else
    struct timespec delay = {.tv_sec = milliseconds / 1000,
                             .tv_nsec = (milliseconds % 1000) * 1000000};
    nanosleep(&delay, NULL);
#endif
}

static bool prepare_output(void)
{
#ifdef _WIN32
    /* Windows CRT text mode expands LF bytes in binary codeplug records. */
    if (_setmode(_fileno(stdout), _O_BINARY) < 0) {
        perror("binary USB output");
        return false;
    }
#endif
    return true;
}

static bool idle(libusb_device_handle *radio)
{
    for (unsigned attempt = 0; attempt < 8; ++attempt) {
        unsigned char state = 0;
        int received = libusb_control_transfer(radio, 0xa1, DFU_GETSTATE, 0,
                                                DFU_INTERFACE, &state, 1, TIMEOUT_MS);
        if (!check_length(received, 1, "DFU_GETSTATE"))
            return false;
        switch (state) {
        case STATE_IDLE:
            return true;
        case STATE_COMMAND_SYNC:
        case STATE_COMMAND_IDLE:
        case STATE_UPLOAD_IDLE:
            if (!check_length(libusb_control_transfer(radio, 0x21, DFU_ABORT, 0,
                                                      DFU_INTERFACE, NULL, 0, TIMEOUT_MS),
                              0, "DFU_ABORT"))
                return false;
            break;
        case STATE_BUSY:
            pause_ms(100);
            break;
        default:
            fprintf(stderr, "Unexpected DFU state %u. Reconnect the radio in normal mode.\n", state);
            return false;
        }
    }
    fprintf(stderr, "Timed out waiting for DFU idle.\n");
    return false;
}

static bool finish_command(libusb_device_handle *radio, const char *label, bool universal)
{
    /* GETSTATUS advances the DFU command state. GETSTATE alone does not. */
    for (unsigned attempt = 0; attempt < 15; ++attempt) {
        unsigned char status[6];
        int received = libusb_control_transfer(radio, 0xa1, DFU_GETSTATUS, 0,
                                                DFU_INTERFACE, status, sizeof(status), TIMEOUT_MS);
        if (!check_length(received, sizeof(status), "DFU_GETSTATUS")) {
            fprintf(stderr, "%s: status request failed.\n", label);
            return false;
        }
        bool ready = status[4] == STATE_IDLE || status[4] == STATE_COMMAND_IDLE;
        if (status[0] != 0 || (!ready && status[4] != STATE_COMMAND_SYNC && status[4] != STATE_BUSY)) {
            fprintf(stderr, "%s: unexpected DFU status %u, state %u\n", label, status[0], status[4]);
            return false;
        }
        if (ready && !universal)
            return idle(radio);
        unsigned delay = status[1] | (unsigned)status[2] << 8 | (unsigned)status[3] << 16;
        if (delay > 2000) {
            fprintf(stderr, "%s: unexpected DFU poll delay %u ms\n", label, delay);
            return false;
        }
        if (!ready && (!delay || (!universal && delay < 100)))
            delay = 100;
        if (delay)
            pause_ms(delay);
        if (ready)
            return idle(radio);
    }
    fprintf(stderr, "%s: timed out waiting for DFU status\n", label);
    return false;
}

enum session_command {
    ENTER_READ_SESSION, REQUEST_MODEL, SELECT_ADDRESS_ZERO,
    READ_UNIVERSAL_TABLE, END_UNIVERSAL_READ
};

static bool session_command(libusb_device_handle *radio, enum session_command command)
{
    /* These are block-zero control messages. No codeplug data is sent to the radio. */
    unsigned char enter[] = {0x91, 0x01};
    unsigned char model[] = {0xa2, 0x01};
    unsigned char address[] = {0x21, 0, 0, 0, 0};
    unsigned char universal_read[] = {0xda, 0x01};
    unsigned char universal_end[] = {0x91, 0x05};
    unsigned char *payload;
    uint16_t size;
    const char *label;
    switch (command) {
    case ENTER_READ_SESSION:
        payload = enter; size = sizeof(enter); label = "enter programming session";
        break;
    case REQUEST_MODEL:
        payload = model; size = sizeof(model); label = "request model";
        break;
    case SELECT_ADDRESS_ZERO:
        payload = address; size = sizeof(address); label = "select address zero";
        break;
    case READ_UNIVERSAL_TABLE:
        payload = universal_read; size = sizeof(universal_read); label = "read Universal Privacy";
        break;
    case END_UNIVERSAL_READ:
        payload = universal_end; size = sizeof(universal_end); label = "finish Universal Privacy read";
        break;
    default:
        return false;
    }
    int sent = libusb_control_transfer(radio, 0x21, DFU_DNLOAD, 0,
                                       DFU_INTERFACE, payload, size, TIMEOUT_MS);
    if (!check_length(sent, size, label))
        return false;
    if (command == END_UNIVERSAL_READ) {
        /* 91 05 reboots the radio. CPS does not require it to still be
           enumerated after this OUT transfer, which must succeed in full. */
        unsigned char status[6];
        int received = libusb_control_transfer(radio, 0xa1, DFU_GETSTATUS, 0,
                                                DFU_INTERFACE, status, sizeof(status), TIMEOUT_MS);
        if (received == LIBUSB_ERROR_PIPE || received == LIBUSB_ERROR_NO_DEVICE) {
            fprintf(stderr, "Universal Privacy: USB interface reset after accepted end command (%s).\n",
                    libusb_error_name(received));
            return true;
        }
        if (!check_length(received, sizeof(status), "finish Universal Privacy read: DFU_GETSTATUS"))
            return false;
        if (status[0] != 0) {
            fprintf(stderr, "finish Universal Privacy read: DFU status %u.\n", status[0]);
            return false;
        }
        return true;
    }
    return finish_command(radio, label, command == READ_UNIVERSAL_TABLE);
}

static bool read_block(libusb_device_handle *radio, uint16_t block,
                       unsigned char *buffer, uint16_t size)
{
    int received = libusb_control_transfer(radio, 0xa1, DFU_UPLOAD, block,
                                            DFU_INTERFACE, buffer, size, TIMEOUT_MS);
    return check_length(received, size, "DFU_UPLOAD");
}

/* Stock CPS 2.42 reads the Universal table through a cursor on block zero.
   It acknowledges each successful upload, including the last, with DFU_ABORT. */
static bool finish_universal_block(libusb_device_handle *radio)
{
    unsigned char status[6];
    int received = libusb_control_transfer(radio, 0xa1, DFU_GETSTATUS, 0, DFU_INTERFACE,
                                           status, sizeof(status), TIMEOUT_MS);
    if (!check_length(received, sizeof(status), "Universal Privacy DFU_GETSTATUS"))
        return false;
    unsigned delay = status[1] | (unsigned)status[2] << 8 | (unsigned)status[3] << 16;
    if (status[0] != 0 || (status[4] != STATE_IDLE && status[4] != STATE_UPLOAD_IDLE)
        || delay > TIMEOUT_MS) {
        fprintf(stderr, "Universal Privacy: unexpected DFU status %u, state %u, delay %u.\n",
                status[0], status[4], delay);
        return false;
    }
    if (delay)
        pause_ms(delay);
    return check_length(libusb_control_transfer(radio, 0x21, DFU_ABORT, 0, DFU_INTERFACE,
                                                NULL, 0, TIMEOUT_MS), 0, "Universal Privacy DFU_ABORT")
           && idle(radio);
}

enum { UNIVERSAL_BLOCKS = 20, UNIVERSAL_SLOTS = 255, UNIVERSAL_NAME_BYTES = 32 };

static unsigned char returned_value_status(const unsigned char *entry, size_t capacity)
{
    /* The width belongs to the selected type; padding cannot establish a value. */
    static const unsigned char widths[] = {0, 5, 8, 16, 32, 9, 64, 64};
    unsigned type = entry[0];
    if (type >= sizeof(widths) || widths[type] == 0 || widths[type] > capacity)
        return 0;
    const unsigned char *value = entry + 4 + UNIVERSAL_NAME_BYTES;
    bool zero = true, erased = true;
    for (size_t i = 0; i < widths[type]; ++i) {
        zero &= value[i] == 0;
        erased &= value[i] == 0xff;
    }
    return zero ? 1 : erased ? 2 : 3;
}

static bool read_universal_metadata(libusb_device_handle *radio, const char *model)
{
    if (!session_command(radio, READ_UNIVERSAL_TABLE)) {
        fprintf(stderr, "Universal Privacy: start failed, 0/%d blocks confirmed.\n", UNIVERSAL_BLOCKS);
        return false;
    }
    unsigned char table[UNIVERSAL_BLOCKS * BLOCK_SIZE];
    size_t confirmed = 0;
    for (; confirmed < UNIVERSAL_BLOCKS; ++confirmed) {
        if (!read_block(radio, 0, table + confirmed * BLOCK_SIZE, BLOCK_SIZE) || !finish_universal_block(radio)) {
            fprintf(stderr, "Incomplete Universal Privacy table at block %zu: %zu/%d blocks confirmed.\n",
                    confirmed + 1, confirmed, UNIVERSAL_BLOCKS);
            break;
        }
    }
    bool complete = confirmed == UNIVERSAL_BLOCKS;
    if (complete || idle(radio)) {
        if (!session_command(radio, END_UNIVERSAL_READ)) {
            fprintf(stderr, "Universal Privacy: end failed, %zu/%d blocks confirmed.\n", confirmed, UNIVERSAL_BLOCKS);
            complete = false;
        }
    }
    /* Type, UTF-16LE name, and status. The radio's value bytes and padding
       never become part of the helper's stdout protocol. */
    unsigned char rows[UNIVERSAL_SLOTS][1 + UNIVERSAL_NAME_BYTES + 1];
    if (complete) {
        size_t offset = 0;
        for (size_t i = 0; i < UNIVERSAL_SLOTS; ++i) {
            const unsigned char *entry = table + offset;
            size_t capacity = i < 16 ? 64 : 32;
            rows[i][0] = entry[0];
            memcpy(rows[i] + 1, entry + 4, UNIVERSAL_NAME_BYTES);
            rows[i][1 + UNIVERSAL_NAME_BYTES] = returned_value_status(entry, capacity);
            offset += 4 + UNIVERSAL_NAME_BYTES + capacity;
        }
    }
    volatile unsigned char *raw = table;
    for (size_t i = 0; i < sizeof(table); ++i)
        raw[i] = 0;
    return complete && fwrite(model, 1, 8, stdout) == 8
           && fwrite(rows, 1, sizeof(rows), stdout) == sizeof(rows) && fflush(stdout) == 0;
}

static const char *identify(libusb_device_handle *radio)
{
    unsigned char identity[64];
    if (!idle(radio) || !session_command(radio, ENTER_READ_SESSION)
        || !session_command(radio, REQUEST_MODEL))
        return NULL;
    int size = libusb_control_transfer(radio, 0xa1, DFU_UPLOAD, 0, DFU_INTERFACE,
                                       identity, sizeof(identity), TIMEOUT_MS);
    if (!check_usb(size, "read model identity") || !idle(radio))
        return NULL;
    /* qdmr uses the same codeplug layout for the factory firmware of these models. */
    if (size >= (int)sizeof(uv390_name) && memcmp(identity, uv390_name, sizeof(uv390_name)) == 0)
        return uv390_name;
    if (size >= (int)sizeof(uv380_name) && memcmp(identity, uv380_name, sizeof(uv380_name)) == 0)
        return uv380_name;
    fprintf(stderr, "Expected an MD-UV380/MD-UV390 identity, received %d bytes.\n", size);
    return NULL;
}

static bool output_u32(uint32_t value)
{
    unsigned char bytes[] = {value, value >> 8, value >> 16, value >> 24};
    return fwrite(bytes, 1, sizeof(bytes), stdout) == sizeof(bytes);
}

static bool read_regions(libusb_device_handle *radio, bool include_privacy)
{
    if (!session_command(radio, SELECT_ADDRESS_ZERO))
        return false;

    unsigned char buffer[BLOCK_SIZE];
    uint32_t cached_base = UINT32_MAX;
    for (size_t region = 0; region < sizeof(regions) / sizeof(regions[0]); ++region) {
        uint32_t start = regions[region].start, end = regions[region].end;
        if (!output_u32(start) || !output_u32(end - start))
            return false;
        for (uint32_t base = start - start % BLOCK_SIZE; base < end; base += BLOCK_SIZE) {
            if (base != cached_base && !read_block(radio, (uint16_t)(base / BLOCK_SIZE + 2), buffer, sizeof(buffer))) {
                fprintf(stderr, "Reading address 0x%06" PRIx32 " failed.\n", base);
                return false;
            }
            cached_base = base;
            uint32_t from = base < start ? start - base : 0;
            uint32_t to = end - base < BLOCK_SIZE ? end - base : BLOCK_SIZE;
            if (fwrite(buffer + from, 1, to - from, stdout) != to - from)
                return false;
        }
    }
    const uint32_t shared_base = shared_regions[0].start;
    if (!read_block(radio, (uint16_t)(shared_base / BLOCK_SIZE + 2), buffer, sizeof(buffer))) {
        /* Keep the completed settings, and never use bytes from a failed or short upload. */
        (void)idle(radio);
        return output_u32(UINT32_MAX) && output_u32(0) && fflush(stdout) == 0;
    }
    size_t count = sizeof(shared_regions) / sizeof(shared_regions[0]) - (include_privacy ? 0 : PRIVACY_REGIONS);
    for (size_t region = 0; region < count; ++region) {
        uint32_t start = shared_regions[region].start, length = shared_regions[region].end - start;
        if (!output_u32(start) || !output_u32(length) ||
            fwrite(buffer + (start - shared_base), 1, length, stdout) != length)
            return false;
    }
    return idle(radio) && fflush(stdout) == 0;
}

static uint8_t dfu_interface_string_index(libusb_device *device)
{
    struct libusb_config_descriptor *config = NULL;
    if (!check_usb(libusb_get_active_config_descriptor(device, &config), "USB configuration"))
        return 0;
    uint8_t index = 0;
    for (unsigned i = 0; i < config->bNumInterfaces; ++i) {
        for (int alt = 0; alt < config->interface[i].num_altsetting; ++alt) {
            const struct libusb_interface_descriptor *iface = &config->interface[i].altsetting[alt];
            if (iface->bInterfaceNumber == DFU_INTERFACE && iface->bAlternateSetting == 0
                && iface->bInterfaceClass == 0xfe && iface->bInterfaceSubClass == 1)
                index = iface->iInterface;
        }
    }
    libusb_free_config_descriptor(config);
    return index;
}

static bool is_normal_mode(libusb_device_handle *radio, uint8_t index)
{
    unsigned char layout[100] = {0};
    const char prefix[] = "@Internal Flash";
    int size = libusb_get_string_descriptor_ascii(radio, index, layout, sizeof(layout) - 1);
    if (!check_usb(size, "read DFU layout"))
        return false;
    if (strncmp((const char *)layout, prefix, sizeof(prefix) - 1) == 0) {
        const char *suffix = (const char *)layout + sizeof(prefix) - 1;
        while (*suffix == ' ')
            ++suffix;
        if (strcmp(suffix, "/0x08000000/03*016Kg") == 0)
            return true;
    }
    fprintf(stderr, "Expected the stock firmware USB layout. Power on normally.\n");
    return false;
}

enum read_mode { READ_SETTINGS, DETECT, READ_SETTINGS_WITH_PRIVACY, READ_UNIVERSAL_METADATA };

static bool run(libusb_device **devices, enum read_mode mode)
{
    libusb_device *target = NULL;
    struct libusb_device_descriptor descriptor;
    uint8_t product_index = 0;
    for (size_t i = 0; devices[i]; ++i) {
        if (libusb_get_device_descriptor(devices[i], &descriptor) != 0
            || descriptor.idVendor != VENDOR || descriptor.idProduct != PRODUCT)
            continue;
        if (target) {
            fprintf(stderr, "Found multiple USB 0483:df11 devices. Connect one radio.\n");
            return false;
        }
        target = devices[i];
        product_index = descriptor.iProduct;
    }
    if (!target) {
        fprintf(stderr, "USB 0483:df11 not found.\n");
        return false;
    }
    uint8_t interface_index = dfu_interface_string_index(target);
    if (!interface_index) {
        fprintf(stderr, "USB device does not expose the expected DFU interface.\n");
        return false;
    }

    libusb_device_handle *radio = NULL;
    if (!check_usb(libusb_open(target, &radio), "open USB device"))
        return false;
    unsigned char product[64];
    int size = libusb_get_string_descriptor_ascii(radio, product_index, product, sizeof(product));
    bool success = false;
    if (!check_usb(size, "read USB product"))
        goto close;
    if (size != sizeof(product_name) - 1 || memcmp(product, product_name, (size_t)size) != 0) {
        fprintf(stderr, "USB product is not Digital Radio in USB mode.\n");
        goto close;
    }
    if (!is_normal_mode(radio, interface_index))
        goto close;
    if (!check_usb(libusb_claim_interface(radio, DFU_INTERFACE), "claim DFU interface"))
        goto close;
    const char *model = identify(radio);
    if (model) {
        fprintf(stderr, "USB 0483:df11 identified as %s\n", model);
        if (mode == READ_UNIVERSAL_METADATA)
            success = read_universal_metadata(radio, model);
        else
            success = mode == DETECT || (fwrite(model, 1, 8, stdout) == 8
                                         && read_regions(radio, mode == READ_SETTINGS_WITH_PRIVACY));
    }
    libusb_release_interface(radio, DFU_INTERFACE);
close:
    libusb_close(radio);
    return success;
}

int main(int argc, char **argv)
{
    if (!prepare_output())
        return 1;
    enum read_mode mode = READ_SETTINGS;
    if (argc == 2 && strcmp(argv[1], "--detect") == 0)
        mode = DETECT;
    else if (argc == 2 && strcmp(argv[1], "--include-privacy") == 0)
        mode = READ_SETTINGS_WITH_PRIVACY;
    else if (argc == 2 && strcmp(argv[1], "--universal-privacy") == 0)
        mode = READ_UNIVERSAL_METADATA;
    else if (argc != 1) {
        fprintf(stderr, "Usage: %s [--detect | --include-privacy | --universal-privacy]\n", argv[0]);
        return 2;
    }

    libusb_context *context = NULL;
    if (!check_usb(libusb_init(&context), "initialize libusb"))
        return 1;
    libusb_device **devices = NULL;
    ssize_t count = libusb_get_device_list(context, &devices);
    bool success = check_usb((int)count, "enumerate USB") && run(devices, mode);
    if (devices)
        libusb_free_device_list(devices, 1);
    libusb_exit(context);
    return success ? 0 : 1;
}
