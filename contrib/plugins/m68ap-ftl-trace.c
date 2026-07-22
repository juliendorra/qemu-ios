/*
 * M68AP AppleNANDFTL clean-open trace helper.
 *
 * This is an opt-in diagnostic plugin.  It does not affect normal emulation.
 * It records the executed translation-block path through the iPhone1,1 4A102
 * kernel's stripped FTL_Open and exits as soon as that path either succeeds or
 * falls back to FTLRestore.
 *
 * The 4A102 kernel currently has an unrelated IOIpodUSBDevice start crash
 * which can win the startup race before the NAND workloop calls FTL_Open.  By
 * default the plugin makes that one driver's start method return false before
 * touching hardware.  This guest-memory edit is diagnostic only; pass
 * skip-usb-start=false to disable it.  The original bytes are verified before
 * the write and no firmware file is modified.
 */

#include "qemu/osdep.h"

#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

#define KERNEL_MIN              UINT64_C(0xc0000000)
#define KERNEL_MAX              UINT64_C(0xc1000000)
#define FTL_OPEN_START          UINT64_C(0xc047302c)
#define FTL_OPEN_END            UINT64_C(0xc0473834)
#define FTL_RESTORE_CALL        UINT64_C(0xc0473814)
#define FTL_OPEN_SUCCESS        UINT64_C(0xc0473830)
#define KERNEL_PANIC            UINT64_C(0xc0019790)
#define USB_DEVICE_START_VA     UINT64_C(0xc04cb198)
#define SDIO_START_VA           UINT64_C(0xc04ba0e8)
#define FMC_FUNCTION_CALL       UINT64_C(0xc04ba240)
#define ARM_FUNCTION_WITH       UINT64_C(0xc0158084)
#define ARM_FUNCTION_WITH_CSTR  UINT64_C(0xc015816c)
#define ARM_FUNCTION_WAIT_DONE  UINT64_C(0xc01580e4)
#define ARM_FUNCTION_INIT_FAIL  UINT64_C(0xc015813e)
#define GPIO_REGISTER_CALL      UINT64_C(0xc0492704)
#define GPIO_REGISTER_RETURN    UINT64_C(0xc0492708)
#define FUNCTION_SET_PROPERTY   UINT64_C(0xc01581da)
#define FUNCTION_SET_RETURN     UINT64_C(0xc01581dc)
#define WAIT_FOR_SERVICE        UINT64_C(0xc01351de)
#define GET_EXISTING_SERVICES   UINT64_C(0xc0134d40)
#define SERVICE_CANDIDATE       UINT64_C(0xc0134da0)
#define SERVICE_MATCHED         UINT64_C(0xc0134dc8)
#define SERVICE_ITER_RELEASE    UINT64_C(0xc0134dfa)
#define GET_EXISTING_RETURN     UINT64_C(0xc0134e10)
#define ROOT_DOMAIN_VTABLE      UINT32_C(0xc019a750)
#define TAGGED_RELEASE_STORED   UINT64_C(0xc0122e04)
#define TAGGED_RELEASE_UPDATED  UINT64_C(0xc0122e48)
#define FTL_SCAN_READ_RETURN    UINT64_C(0xc0473100)
#define FTL_SCAN_VALIDATE       UINT64_C(0xc0473128)
#define FTL_SCAN_SELECT         UINT64_C(0xc0473140)
#define FTL_SCAN_DONE           UINT64_C(0xc0473164)
#define FTL_COPY_READ_RETURN    UINT64_C(0xc04731f0)
#define FTL_COPY_SPARE_CHECK    UINT64_C(0xc0473204)
#define FTL_COPY_ACCEPT         UINT64_C(0xc0473220)
#define FTL_COPY_REJECT         UINT64_C(0xc0473238)
#define FTL_CONTEXT_VERDICT     UINT64_C(0xc0473294)
#define BSD_MOUNTROOT_RESULT    UINT64_C(0xc01a3ec8)
#define VFS_MOUNT_CANDIDATE     UINT64_C(0xc007bcb2)
#define VFS_MOUNT_RETURN        UINT64_C(0xc007bcdc)
#define VFS_MOUNT_EXHAUSTED     UINT64_C(0xc007bcbe)
#define HFS_MOUNT_RESULT        UINT64_C(0xc00da1ea)
#define HFS_HEADER_READ_RESULT  UINT64_C(0xc00d965c)
#define HFS_MOUNTFS_RESULT      UINT64_C(0xc00d98b6)
#define HFS_MOUNTFS_START       UINT64_C(0xc00ddd24)
#define HFS_MOUNTFS_END         UINT64_C(0xc00de5da)
#define BT_OPEN_ARGUMENTS       UINT64_C(0xc00e3b04)
#define BT_OPEN_GETBLOCK_RESULT UINT64_C(0xc00e3b80)
#define BT_OPEN_READ_CALL       UINT64_C(0xc00e3b8a)
#define BT_OPEN_READ_RESULT     UINT64_C(0xc00e3b90)
#define BT_OPEN_HEADER_RESULT   UINT64_C(0xc00e3bcc)
#define VERIFY_HEADER_START     UINT64_C(0xc00e5490)
#define VERIFY_HEADER_END       UINT64_C(0xc00e555c)
#define VERIFY_HEADER_FAILURE   UINT64_C(0xc00e54c2)
#define BUF_BREAD_FLAGS         UINT64_C(0xc00716e0)
#define BUF_BREAD_STRATEGY      UINT64_C(0xc0071710)
#define BUF_BREAD_STRATEGY_DONE UINT64_C(0xc0071716)
#define VNOP_STRATEGY_ENTRY     UINT64_C(0xc008a0c0)
#define BUF_DEVICE_STRATEGY_CALL UINT64_C(0xc0071c3e)
#define SPEC_DRIVER_STRATEGY_CALL UINT64_C(0xc008ea68)
#define STORAGE_STRATEGY_START   UINT64_C(0xc045c4d0)
#define STORAGE_STRATEGY_END     UINT64_C(0xc045c776)
#define STORAGE_PROVIDER_READ_CALL UINT64_C(0xc045c734)
#define MEDIA_PROVIDER_READ_CALL  UINT64_C(0xc045aeda)
#define BLOCK_PROVIDER_READ_CALL UINT64_C(0xc045611a)
#define BLOCK_STORAGE_READ_START UINT64_C(0xc045893c)
#define BLOCK_STORAGE_READ_END  UINT64_C(0xc04589fa)
#define BLOCK_STORAGE_SUBMIT_CALL UINT64_C(0xc04589ec)
#define BLOCK_STORAGE_SUBMIT_START UINT64_C(0xc0458cf0)
#define BLOCK_STORAGE_SUBMIT_END UINT64_C(0xc0458dea)
#define BLOCK_ASYNC_SUBMIT_CALL UINT64_C(0xc0458d86)
#define BLOCK_EXECUTE_CALL     UINT64_C(0xc0458fd0)
#define BLOCK_DEVICE_READ_CALL UINT64_C(0xc0456c0a)
#define BLOCK_PHYSICAL_READ_CALL UINT64_C(0xc045597c)
#define BLOCK_DRIVER_READ_CALL UINT64_C(0xc0466b70)
#define FTL_DATA_READ_DIRECT_CALL UINT64_C(0xc046d680)
#define FTL_DATA_READ_DIRECT_RETURN UINT64_C(0xc046d684)
#define FTL_DATA_READ_MAPPED_CALL UINT64_C(0xc046d6d4)
#define FTL_DATA_READ_MAPPED_RETURN UINT64_C(0xc046d6d8)
#define FTL_CORE_READ_START     UINT64_C(0xc046f99c)
#define FTL_CORE_READ_END       UINT64_C(0xc04701c8)
#define FTL_MAP_DECISION        UINT64_C(0xc04773b4)
#define FTL_PAGE_READ_SINGLE    UINT64_C(0xc047735c)
#define FTL_PAGE_READ_RUN       UINT64_C(0xc0477598)
#define FTL_PHYSICAL_BOUNDS     UINT64_C(0xc0476fd4)
#define FTL_PHYSICAL_PROVIDER_CALL UINT64_C(0xc0477050)
#define FTL_PHYSICAL_PROVIDER_RETURN UINT64_C(0xc0477054)
#define FTL_PHYSICAL_COMPLETE  UINT64_C(0xc047721c)
#define ROOTDEV_ADDRESS         UINT32_C(0xc02619b4)
#define ROOTDEVICE_ADDRESS      UINT32_C(0xc02619c0)
#define ROOTVP_ADDRESS          UINT32_C(0xc02619d8)

/* ARM push/add prologue; replacement is mov r0,#0; bx lr. */
static const uint8_t usb_start_original[] = {
    0xf0, 0x40, 0x2d, 0xe9, 0x0c, 0x70, 0x8d, 0xe2,
};
static const uint8_t usb_start_return_false[] = {
    0x00, 0x00, 0xa0, 0xe3, 0x1e, 0xff, 0x2f, 0xe1,
};
static const uint8_t platform_function_original[] = {
    0xb0, 0xb5, 0x05, 0x1c,
};
static const uint8_t platform_function_unavailable[] = {
    0x00, 0x20, 0x70, 0x47,
};

typedef struct TraceBlock {
    uint64_t address;
    char *disassembly;
} TraceBlock;

static bool skip_usb_start = true;
static bool skip_sdio_start;
static bool skip_platform_functions;
static bool stop_at_verdict = true;
static bool stop_at_panic = true;
static bool stop_at_verify_failure;
static bool stabilize_root_domain;
static bool trace_details = true;
static bool root_domain_stabilized;
static bool usb_patch_done;
static bool usb_patch_reported;
static bool sdio_patch_done;
static bool sdio_patch_reported;
static bool platform_function_patch_done;
static bool platform_function_patch_reported;
static bool fmc_function_in_progress;
static bool gpio_registration_in_progress;
static bool function_parent_wait_in_progress;
static bool service_iterator_teardown;
static uint32_t root_domain_object;
static uint32_t mountroot_attempts;
static uint32_t last_mountroot_error = UINT32_MAX;
static uint32_t vfs_mount_calls;
static bool hfs_mountfs_trace_complete;
static bool verify_header_trace_complete;
static bool bt_header_read_in_progress;
static GHashTable *hfs_mountfs_seen;
static GHashTable *verify_header_seen;
static GHashTable *storage_strategy_seen;
static GHashTable *ftl_core_read_seen;
static GPtrArray *trace_blocks;
static struct qemu_plugin_register *reg_r0;
static struct qemu_plugin_register *reg_r1;
static struct qemu_plugin_register *reg_r2;
static struct qemu_plugin_register *reg_r3;
static struct qemu_plugin_register *reg_r4;
static struct qemu_plugin_register *reg_r5;
static struct qemu_plugin_register *reg_r6;
static struct qemu_plugin_register *reg_r8;
static struct qemu_plugin_register *reg_fp;
static struct qemu_plugin_register *reg_sl;
static struct qemu_plugin_register *reg_sp;
static struct qemu_plugin_register *reg_ip;
static struct qemu_plugin_register *reg_lr;
static struct qemu_plugin_register *reg_pc;

static uint32_t read_u32_register(struct qemu_plugin_register *handle)
{
    g_autoptr(GByteArray) value = g_byte_array_new();
    uint32_t result = 0;

    if (handle && qemu_plugin_read_register(handle, value) &&
        value->len >= sizeof(result)) {
        memcpy(&result, value->data, sizeof(result));
    }
    return result;
}

static void plugin_log(const char *message)
{
    qemu_plugin_outs(message);
    qemu_plugin_outs("\n");
}

static bool patch_start_method(uint64_t address, bool *done, bool *reported,
                               const char *name)
{
    g_autoptr(GByteArray) current = g_byte_array_new();
    g_autoptr(GByteArray) patch = g_byte_array_new();

    if (*done) {
        return true;
    }
    if (!qemu_plugin_read_memory_vaddr(address, current,
                                       sizeof(usb_start_original))) {
        return false;
    }
    if (memcmp(current->data, usb_start_original,
               sizeof(usb_start_original)) != 0) {
        if (!*reported) {
            g_autofree char *line = g_strdup_printf(
                "M68AP_FTL_TRACE %s start skip refused: opcode mismatch",
                name);
            plugin_log(line);
            *reported = true;
        }
        return false;
    }

    g_byte_array_append(patch, usb_start_return_false,
                        sizeof(usb_start_return_false));
    if (!qemu_plugin_write_memory_vaddr(address, patch)) {
        return false;
    }

    *done = true;
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE diagnostic %s start skip applied at 0x%08" PRIx64,
        name, address);
    plugin_log(line);
    return true;
}

static void try_patch_unrelated_startups(unsigned int cpu_index, void *userdata)
{
    if (skip_usb_start) {
        patch_start_method(USB_DEVICE_START_VA, &usb_patch_done,
                           &usb_patch_reported, "IOIpodUSBDevice");
    }
    if (skip_sdio_start) {
        patch_start_method(SDIO_START_VA, &sdio_patch_done,
                           &sdio_patch_reported, "AppleS5L8900XSDIO");
    }
    if (skip_platform_functions && !platform_function_patch_done) {
        g_autoptr(GByteArray) current = g_byte_array_new();
        g_autoptr(GByteArray) patch = g_byte_array_new();

        if (qemu_plugin_read_memory_vaddr(
                ARM_FUNCTION_WITH_CSTR, current,
                sizeof(platform_function_original))) {
            if (memcmp(current->data, platform_function_original,
                       sizeof(platform_function_original)) == 0) {
                g_byte_array_append(patch, platform_function_unavailable,
                                    sizeof(platform_function_unavailable));
                if (qemu_plugin_write_memory_vaddr(
                        ARM_FUNCTION_WITH_CSTR, patch)) {
                    platform_function_patch_done = true;
                    plugin_log("M68AP_FTL_TRACE diagnostic platform-function lookup unavailable at 0xc015816c");
                }
            } else if (!platform_function_patch_reported) {
                plugin_log("M68AP_FTL_TRACE platform-function lookup adjustment refused: opcode mismatch");
                platform_function_patch_reported = true;
            }
        }
    }
}

static void trace_block_exec(unsigned int cpu_index, void *userdata)
{
    TraceBlock *block = userdata;
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE block=0x%08" PRIx64 " insn=%s",
        block->address, block->disassembly);
    plugin_log(line);
}

static void trace_verdict_exec(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    const char *verdict = address == FTL_OPEN_SUCCESS ? "success" : "restore";
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE verdict=%s address=0x%08" PRIx64,
        verdict, address);
    plugin_log(line);
    if (stop_at_verdict) {
        exit(address == FTL_OPEN_SUCCESS ? 0 : 2);
    }
}

static void trace_panic_exec(unsigned int cpu_index, void *userdata)
{
    g_autoptr(GByteArray) stack = g_byte_array_new();
    uint32_t r0 = read_u32_register(reg_r0);
    uint32_t sp = read_u32_register(reg_sp);
    uint32_t lr = read_u32_register(reg_lr);
    uint32_t pc = read_u32_register(reg_pc);

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE pre_ftl_panic=0x%08" PRIx32
        " r0=0x%08" PRIx32 " sp=0x%08" PRIx32 " lr=0x%08" PRIx32,
        pc, r0, sp, lr);
    plugin_log(line);
    if (sp && qemu_plugin_read_memory_vaddr(sp, stack, 512)) {
        g_autoptr(GString) dump = g_string_new("M68AP_FTL_TRACE stack=");
        for (size_t i = 0; i < stack->len; i++) {
            g_string_append_printf(dump, "%02x", stack->data[i]);
        }
        plugin_log(dump->str);
    }
    if (stop_at_panic) {
        exit(3);
    }
}

static void trace_diagnostic_point(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t sl;

    if (address == GPIO_REGISTER_CALL) {
        gpio_registration_in_progress = true;
    } else if (address == GPIO_REGISTER_RETURN) {
        /* Log the return before closing this narrow registration window. */
    } else if (address == FMC_FUNCTION_CALL) {
        fmc_function_in_progress = true;
    } else if (!fmc_function_in_progress &&
               !(gpio_registration_in_progress &&
                 (address == FUNCTION_SET_PROPERTY ||
                  address == FUNCTION_SET_RETURN))) {
        return;
    }

    uint32_t regs[] = {
        read_u32_register(reg_r0), read_u32_register(reg_r1),
        read_u32_register(reg_r2), read_u32_register(reg_r3),
        read_u32_register(reg_r4), read_u32_register(reg_r5),
        read_u32_register(reg_r6), read_u32_register(reg_sp),
        read_u32_register(reg_lr),
    };
    sl = read_u32_register(reg_sl);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE diagnostic_point=0x%08" PRIx64
        " r0=0x%08" PRIx32 " r1=0x%08" PRIx32
        " r2=0x%08" PRIx32 " r3=0x%08" PRIx32
        " r4=0x%08" PRIx32 " r5=0x%08" PRIx32
        " r6=0x%08" PRIx32 " sp=0x%08" PRIx32
        " lr=0x%08" PRIx32 " sl=0x%08" PRIx32,
        address, regs[0], regs[1], regs[2], regs[3], regs[4], regs[5],
        regs[6], regs[7], regs[8], sl);
    plugin_log(line);

    if (address == ARM_FUNCTION_WAIT_DONE) {
        function_parent_wait_in_progress = true;
    }

    if (address == FUNCTION_SET_PROPERTY && regs[1]) {
        g_autoptr(GByteArray) symbol = g_byte_array_new();
        g_autoptr(GByteArray) symbol_text = g_byte_array_new();
        uint32_t text_pointer = 0;

        if (qemu_plugin_read_memory_vaddr(regs[1], symbol, 0x14) &&
            symbol->len >= 0x14) {
            memcpy(&text_pointer, symbol->data + 0x10,
                   sizeof(text_pointer));
        }
        if (text_pointer && qemu_plugin_read_memory_vaddr(
                text_pointer, symbol_text, 64)) {
            size_t length = strnlen((char *)symbol_text->data,
                                    symbol_text->len);
            g_autofree char *value = g_strndup(
                (char *)symbol_text->data, length);
            g_autofree char *property_line = g_strdup_printf(
                "M68AP_FTL_TRACE gpio_function_parent_property=%s service=0x%08" PRIx32,
                value, regs[4]);
            plugin_log(property_line);
        }
    }
    if (address == GPIO_REGISTER_RETURN) {
        gpio_registration_in_progress = false;
    }

    if (address == FMC_FUNCTION_CALL && regs[1]) {
        g_autoptr(GByteArray) text = g_byte_array_new();
        if (qemu_plugin_read_memory_vaddr(regs[1], text, 64)) {
            size_t length = strnlen((char *)text->data, text->len);
            g_autofree char *value = g_strndup((char *)text->data, length);
            g_autofree char *text_line = g_strdup_printf(
                "M68AP_FTL_TRACE diagnostic_string=0x%08" PRIx32 " value=%s",
                regs[1], value);
            plugin_log(text_line);
        }
    } else if (address == ARM_FUNCTION_WAIT_DONE) {
        g_autoptr(GByteArray) symbol = g_byte_array_new();
        g_autoptr(GByteArray) symbol_text = g_byte_array_new();
        g_autoptr(GByteArray) data_object = g_byte_array_new();
        g_autoptr(GByteArray) data = g_byte_array_new();
        uint32_t text_pointer = 0, data_pointer = 0, phandle = 0;

        if (sl && qemu_plugin_read_memory_vaddr(sl, symbol, 0x14) &&
            symbol->len >= 0x14) {
            memcpy(&text_pointer, symbol->data + 0x10, sizeof(text_pointer));
        }
        if (text_pointer && qemu_plugin_read_memory_vaddr(
                text_pointer, symbol_text, 64)) {
            size_t length = strnlen((char *)symbol_text->data,
                                    symbol_text->len);
            g_autofree char *value = g_strndup(
                (char *)symbol_text->data, length);
            g_autofree char *symbol_line = g_strdup_printf(
                "M68AP_FTL_TRACE function_parent_symbol=%s", value);
            plugin_log(symbol_line);
        }
        if (regs[6] && qemu_plugin_read_memory_vaddr(
                regs[6], data_object, 0x18) && data_object->len >= 0x0c) {
            memcpy(&data_pointer, data_object->data + 8,
                   sizeof(data_pointer));
        }
        if (data_pointer && qemu_plugin_read_memory_vaddr(
                data_pointer, data, 16) && data->len >= sizeof(phandle)) {
            memcpy(&phandle, data->data, sizeof(phandle));
            g_autofree char *data_line = g_strdup_printf(
                "M68AP_FTL_TRACE function_data=0x%08" PRIx32
                " parent_phandle=0x%08" PRIx32,
                data_pointer, phandle);
            plugin_log(data_line);
        }
    }
}

static uint32_t read_memory_u32(uint32_t address)
{
    g_autoptr(GByteArray) value = g_byte_array_new();
    uint32_t result = 0;

    if (address && qemu_plugin_read_memory_vaddr(address, value,
                                                 sizeof(result)) &&
        value->len >= sizeof(result)) {
        memcpy(&result, value->data, sizeof(result));
    }
    return result;
}

static bool write_memory_u32(uint32_t address, uint32_t value)
{
    g_autoptr(GByteArray) bytes = g_byte_array_new();

    g_byte_array_append(bytes, (const uint8_t *)&value, sizeof(value));
    return qemu_plugin_write_memory_vaddr(address, bytes);
}

static void trace_service_matching(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t sp = read_u32_register(reg_sp);
    uint32_t r4 = read_u32_register(reg_r4);
    uint32_t r8 = read_u32_register(reg_r8);
    uint32_t object = 0;
    const char *event = "point";

    if (!function_parent_wait_in_progress) {
        return;
    }
    if (address == WAIT_FOR_SERVICE) {
        event = "wait_entry";
    } else if (address == GET_EXISTING_SERVICES) {
        event = "enumeration_entry";
    } else if (address == SERVICE_CANDIDATE) {
        event = "candidate";
        object = read_memory_u32(sp);
    } else if (address == SERVICE_MATCHED) {
        event = "matched";
        object = read_memory_u32(sp);
    } else if (address == SERVICE_ITER_RELEASE) {
        event = "iterator_release";
        object = r8;
        service_iterator_teardown = true;
    } else if (address == GET_EXISTING_RETURN) {
        event = "enumeration_return";
        object = r8;
    }

    uint32_t vtable = read_memory_u32(object);
    uint32_t references = read_memory_u32(object + 4);
    uint32_t state = read_memory_u32(object + 0x24);
    uint32_t iterator_references = read_memory_u32(r4 + 4);
    if (address == SERVICE_CANDIDATE && vtable == ROOT_DOMAIN_VTABLE) {
        root_domain_object = object;
    }
    if (address == SERVICE_ITER_RELEASE && stabilize_root_domain &&
        !root_domain_stabilized && root_domain_object) {
        uint32_t root_references = read_memory_u32(root_domain_object + 4);

        if (root_references == UINT32_C(0x00010001) &&
            write_memory_u32(root_domain_object + 4,
                             UINT32_C(0x00020002))) {
            root_domain_stabilized = true;
            g_autofree char *stabilize_line = g_strdup_printf(
                "M68AP_FTL_TRACE diagnostic root-domain stabilization applied"
                " object=0x%08" PRIx32 " references=0x00010001->0x00020002",
                root_domain_object);
            plugin_log(stabilize_line);
        }
    }
    if (!trace_details) {
        return;
    }
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE service_match event=%s pc=0x%08" PRIx64
        " object=0x%08" PRIx32 " vtable=0x%08" PRIx32
        " references=0x%08" PRIx32 " state=0x%08" PRIx32
        " iterator=0x%08" PRIx32 " iterator_references=0x%08" PRIx32,
        event, address, object, vtable, references, state, r4,
        iterator_references);
    plugin_log(line);
}

static void trace_root_domain_release(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t sp;
    uint32_t object;
    uint32_t references;
    const char *event;

    if (!service_iterator_teardown || !root_domain_object) {
        return;
    }

    sp = read_u32_register(reg_sp);
    object = read_memory_u32(sp);
    if (object != root_domain_object) {
        return;
    }

    references = read_memory_u32(object + 4);
    event = address == TAGGED_RELEASE_STORED ? "entry" : "updated";
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE root_domain_release event=%s pc=0x%08" PRIx64
        " object=0x%08" PRIx32 " references=0x%08" PRIx32
        " r1=0x%08" PRIx32 " r2=0x%08" PRIx32
        " r4=0x%08" PRIx32 " r6=0x%08" PRIx32
        " sp=0x%08" PRIx32 " lr=0x%08" PRIx32,
        event, address, object, references,
        read_u32_register(reg_r1), read_u32_register(reg_r2),
        read_u32_register(reg_r4), read_u32_register(reg_r6), sp,
        read_u32_register(reg_lr));
    plugin_log(line);
}

static void trace_ftl_context_edge(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t r0 = read_u32_register(reg_r0);
    uint32_t r4 = read_u32_register(reg_r4);
    uint32_t r5 = read_u32_register(reg_r5);
    uint32_t r8 = read_u32_register(reg_r8);
    uint32_t fp = read_u32_register(reg_fp);
    uint32_t buffer = address >= FTL_COPY_READ_RETURN &&
                      address <= FTL_CONTEXT_VERDICT ? fp : r5;
    uint32_t data = read_memory_u32(buffer);
    uint32_t spare = read_memory_u32(buffer + 4);
    uint32_t age = read_memory_u32(spare);
    uint32_t version = read_memory_u32(data + 0x7f8);
    uint32_t version_not = read_memory_u32(data + 0x7fc);
    uint32_t spare_word_8 = read_memory_u32(spare + 8);

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE ftl_context_edge pc=0x%08" PRIx64
        " r0=0x%08" PRIx32 " r4=0x%08" PRIx32
        " r5=0x%08" PRIx32 " r8=0x%08" PRIx32
        " fp=0x%08" PRIx32 " buffer=0x%08" PRIx32
        " data=0x%08" PRIx32 " spare=0x%08" PRIx32
        " age=0x%08" PRIx32 " spare_word_8=0x%08" PRIx32
        " version=0x%08" PRIx32 " version_not=0x%08" PRIx32,
        address, r0, r4, r5, r8, fp, buffer, data, spare, age,
        spare_word_8, version, version_not);
    plugin_log(line);
}

static void trace_mountroot_result(unsigned int cpu_index, void *userdata)
{
    g_autoptr(GByteArray) rootdevice = g_byte_array_new();
    uint32_t error = read_u32_register(reg_r5);
    uint32_t rootdev = read_memory_u32(ROOTDEV_ADDRESS);
    uint32_t rootvp = read_memory_u32(ROOTVP_ADDRESS);
    g_autofree char *rootdevice_text = NULL;

    mountroot_attempts++;
    if (mountroot_attempts > 16 && error == last_mountroot_error && error) {
        return;
    }
    last_mountroot_error = error;

    if (qemu_plugin_read_memory_vaddr(ROOTDEVICE_ADDRESS, rootdevice, 24)) {
        size_t length = strnlen((char *)rootdevice->data, rootdevice->len);
        rootdevice_text = g_strndup((char *)rootdevice->data, length);
        for (size_t i = 0; i < length; i++) {
            if (!g_ascii_isprint(rootdevice_text[i]) ||
                g_ascii_isspace(rootdevice_text[i])) {
                rootdevice_text[i] = '?';
            }
        }
    } else {
        rootdevice_text = g_strdup("unreadable");
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE mountroot_result attempt=%" PRIu32
        " error=%" PRIu32 " rootdev=0x%08" PRIx32
        " rootvp=0x%08" PRIx32 " rootdevice=%s",
        mountroot_attempts, error, rootdev, rootvp, rootdevice_text);
    plugin_log(line);
}

static void trace_vfs_mount(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t entry = read_u32_register(reg_r4);
    uint32_t callback = read_memory_u32(entry + 0x20);
    uint32_t error = read_u32_register(reg_r0);
    g_autoptr(GByteArray) name = g_byte_array_new();
    g_autofree char *name_text = NULL;
    const char *event;

    if (address == VFS_MOUNT_CANDIDATE) {
        event = "candidate";
        vfs_mount_calls++;
    } else if (address == VFS_MOUNT_RETURN) {
        event = "return";
    } else {
        event = "exhausted";
    }

    if (entry && qemu_plugin_read_memory_vaddr(entry + 4, name, 16)) {
        size_t length = strnlen((char *)name->data, name->len);
        name_text = g_strndup((char *)name->data, length);
        for (size_t i = 0; i < length; i++) {
            if (!g_ascii_isprint(name_text[i]) ||
                g_ascii_isspace(name_text[i])) {
                name_text[i] = '?';
            }
        }
    } else {
        name_text = g_strdup("none");
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE vfs_mount event=%s call=%" PRIu32
        " entry=0x%08" PRIx32 " name=%s callback=0x%08" PRIx32
        " error=%" PRIu32,
        event, vfs_mount_calls, entry, name_text, callback, error);
    plugin_log(line);
}

static void trace_hfs_mount_result(unsigned int cpu_index, void *userdata)
{
    uint32_t error = read_u32_register(reg_sl);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE hfs_mount_result error=%" PRIu32, error);
    plugin_log(line);
}

static void trace_hfs_stage_result(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t error = read_u32_register(reg_r4);
    const char *stage = address == HFS_HEADER_READ_RESULT ?
                        "header_read" : "mountfs";
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE hfs_stage_result stage=%s error=%" PRIu32,
        stage, error);
    plugin_log(line);
    if (address == HFS_MOUNTFS_RESULT) {
        hfs_mountfs_trace_complete = true;
    }
}

static void trace_hfs_mountfs_block(unsigned int cpu_index, void *userdata)
{
    TraceBlock *block = userdata;
    gpointer key = (gpointer)(uintptr_t)block->address;

    if (hfs_mountfs_trace_complete ||
        g_hash_table_contains(hfs_mountfs_seen, key)) {
        return;
    }
    g_hash_table_add(hfs_mountfs_seen, key);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE hfs_mountfs_block=0x%08" PRIx64 " insn=%s",
        block->address, block->disassembly);
    plugin_log(line);
}

static void trace_verify_header_block(unsigned int cpu_index, void *userdata)
{
    TraceBlock *block = userdata;
    gpointer key = (gpointer)(uintptr_t)block->address;

    if (verify_header_trace_complete ||
        g_hash_table_contains(verify_header_seen, key)) {
        return;
    }
    g_hash_table_add(verify_header_seen, key);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE verify_header_block=0x%08" PRIx64 " insn=%s",
        block->address, block->disassembly);
    plugin_log(line);
}

static void trace_storage_strategy_block(unsigned int cpu_index, void *userdata)
{
    TraceBlock *block = userdata;
    gpointer key = (gpointer)(uintptr_t)block->address;

    if (!bt_header_read_in_progress ||
        g_hash_table_contains(storage_strategy_seen, key)) {
        return;
    }
    g_hash_table_add(storage_strategy_seen, key);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE storage_strategy_block=0x%08" PRIx64 " insn=%s",
        block->address, block->disassembly);
    plugin_log(line);
}

static void trace_block_storage_read_block(unsigned int cpu_index,
                                           void *userdata)
{
    TraceBlock *block = userdata;
    gpointer key = (gpointer)(uintptr_t)block->address;

    if (!bt_header_read_in_progress ||
        g_hash_table_contains(storage_strategy_seen, key)) {
        return;
    }
    g_hash_table_add(storage_strategy_seen, key);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE block_storage_read_block=0x%08" PRIx64 " insn=%s",
        block->address, block->disassembly);
    plugin_log(line);
}

static void trace_ftl_core_read_block(unsigned int cpu_index, void *userdata)
{
    TraceBlock *block = userdata;
    gpointer key = (gpointer)(uintptr_t)block->address;

    if (!bt_header_read_in_progress ||
        g_hash_table_contains(ftl_core_read_seen, key)) {
        return;
    }
    g_hash_table_add(ftl_core_read_seen, key);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE ftl_core_read_block=0x%08" PRIx64 " insn=%s",
        block->address, block->disassembly);
    plugin_log(line);
}

static void trace_verify_header_failure(unsigned int cpu_index, void *userdata)
{
    uint32_t header = read_u32_register(reg_r5);
    g_autoptr(GByteArray) bytes = g_byte_array_new();
    g_autoptr(GString) dump = g_string_new(NULL);

    if (header && qemu_plugin_read_memory_vaddr(header, bytes, 0x26)) {
        for (size_t i = 0; i < bytes->len; i++) {
            g_string_append_printf(dump, "%02x", bytes->data[i]);
        }
    }
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE verify_header_failure header=0x%08" PRIx32
        " r0=0x%08" PRIx32 " r1=0x%08" PRIx32
        " r2=0x%08" PRIx32 " r3=0x%08" PRIx32
        " r4=0x%08" PRIx32 " r6=0x%08" PRIx32
        " fcb=0x%08" PRIx32 " bytes=%s",
        header, read_u32_register(reg_r0), read_u32_register(reg_r1),
        read_u32_register(reg_r2), read_u32_register(reg_r3),
        read_u32_register(reg_r4), read_u32_register(reg_r6),
        read_u32_register(reg_r8), dump->str);
    plugin_log(line);
    if (stop_at_verify_failure) {
        exit(4);
    }
}

static void trace_bt_open_stage(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t fcb = read_u32_register(reg_r5);
    uint32_t error = read_u32_register(reg_r4);
    const char *stage;

    if (address == BT_OPEN_ARGUMENTS) {
        stage = "arguments";
    } else if (address == BT_OPEN_GETBLOCK_RESULT) {
        stage = "getblock";
    } else if (address == BT_OPEN_READ_RESULT) {
        stage = "read_header";
        bt_header_read_in_progress = false;
    } else {
        stage = "validate_header";
        verify_header_trace_complete = true;
    }
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_open_stage stage=%s error=%" PRId32
        " fcb=0x%08" PRIx32 " control=0x%08" PRIx32
        " logical_size=0x%08" PRIx32,
        stage, (int32_t)error, fcb, read_memory_u32(fcb + 0xc),
        read_memory_u32(fcb + 0x14));
    plugin_log(line);
}

static void trace_bt_header_read_call(unsigned int cpu_index, void *userdata)
{
    bt_header_read_in_progress = true;
}

static void trace_bt_buffer_io(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    uint32_t buffer;
    const char *event;

    if (!bt_header_read_in_progress) {
        return;
    }
    if (address == BUF_BREAD_FLAGS) {
        event = "flags";
        buffer = read_u32_register(reg_r0);
    } else if (address == BUF_BREAD_STRATEGY) {
        event = "strategy";
        buffer = read_u32_register(reg_r4);
    } else if (address == BUF_BREAD_STRATEGY_DONE) {
        event = "strategy_done";
        buffer = read_u32_register(reg_r4);
    } else {
        event = "vnop_strategy";
        buffer = read_u32_register(reg_r0);
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_buffer_io event=%s pc=0x%08" PRIx64
        " buffer=0x%08" PRIx32 " result=0x%08" PRIx32
        " flags=0x%08" PRIx32 " vnode=0x%08" PRIx32
        " data=0x%08" PRIx32 " lblk_lo=0x%08" PRIx32
        " lblk_hi=0x%08" PRIx32 " blk_lo=0x%08" PRIx32
        " blk_hi=0x%08" PRIx32,
        event, address, buffer, read_u32_register(reg_r0),
        read_memory_u32(buffer + 0x20), read_memory_u32(buffer + 0x54),
        read_memory_u32(buffer + 0x3c), read_memory_u32(buffer + 0x40),
        read_memory_u32(buffer + 0x44), read_memory_u32(buffer + 0x48),
        read_memory_u32(buffer + 0x4c));
    plugin_log(line);
}

static void trace_bt_device_strategy(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    uint32_t buffer = read_u32_register(reg_r4);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_device_strategy target=0x%08" PRIx32
        " device=0x%08" PRIx32 " buffer=0x%08" PRIx32
        " blk_lo=0x%08" PRIx32 " blk_hi=0x%08" PRIx32,
        read_u32_register(reg_r3), read_u32_register(reg_r8), buffer,
        read_memory_u32(buffer + 0x48), read_memory_u32(buffer + 0x4c));
    plugin_log(line);
}

static void trace_bt_storage_strategy(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    uint32_t buffer = read_u32_register(reg_r5);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_storage_strategy target=0x%08" PRIx32
        " device_number=0x%08" PRIx32 " buffer=0x%08" PRIx32
        " blk_lo=0x%08" PRIx32 " blk_hi=0x%08" PRIx32,
        read_u32_register(reg_r3), read_u32_register(reg_r8), buffer,
        read_memory_u32(buffer + 0x48), read_memory_u32(buffer + 0x4c));
    plugin_log(line);
}

static void trace_bt_provider_read(unsigned int cpu_index, void *userdata)
{
    uint32_t sp;

    if (!bt_header_read_in_progress) {
        return;
    }
    sp = read_u32_register(reg_sp);

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_provider_read target=0x%08" PRIx32
        " provider=0x%08" PRIx32 " client=0x%08" PRIx32
        " memory=0x%08" PRIx32 " offset_lo=0x%08" PRIx32
        " offset_hi=0x%08" PRIx32 " completion_target=0x%08" PRIx32,
        read_u32_register(reg_r4), read_u32_register(reg_r8),
        read_u32_register(reg_r1), read_memory_u32(sp),
        read_u32_register(reg_r2), read_u32_register(reg_r3),
        read_memory_u32(sp + 4));
    plugin_log(line);
}

static void trace_bt_media_read(unsigned int cpu_index, void *userdata)
{
    uint32_t sp;

    if (!bt_header_read_in_progress) {
        return;
    }
    sp = read_u32_register(reg_sp);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_media_read target=0x%08" PRIx32
        " provider=0x%08" PRIx32 " client=0x%08" PRIx32
        " memory=0x%08" PRIx32 " offset_lo=0x%08" PRIx32
        " offset_hi=0x%08" PRIx32,
        read_u32_register(reg_r4), read_u32_register(reg_r0),
        read_u32_register(reg_r1), read_memory_u32(sp),
        read_u32_register(reg_r2), read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_bt_block_read(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_block_read target=0x%08" PRIx32
        " provider=0x%08" PRIx32 " offset_lo=0x%08" PRIx32
        " offset_hi=0x%08" PRIx32 " memory=0x%08" PRIx32,
        read_u32_register(reg_r4), read_u32_register(reg_r8),
        read_u32_register(reg_r1), read_u32_register(reg_r2),
        read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_bt_block_submit(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_block_submit target=0x%08" PRIx32
        " driver=0x%08" PRIx32 " request=0x%08" PRIx32
        " offset_lo=0x%08" PRIx32 " offset_hi=0x%08" PRIx32
        " memory=0x%08" PRIx32,
        read_u32_register(reg_ip), read_u32_register(reg_r5),
        read_u32_register(reg_r4), read_u32_register(reg_r1),
        read_u32_register(reg_r2), read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_bt_async_submit(unsigned int cpu_index, void *userdata)
{
    uint32_t sp;

    if (!bt_header_read_in_progress) {
        return;
    }
    sp = read_u32_register(reg_sp);
    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_async_submit target=0x%08" PRIx32
        " driver=0x%08" PRIx32 " offset_lo=0x%08" PRIx32
        " offset_hi=0x%08" PRIx32 " memory=0x%08" PRIx32
        " request=0x%08" PRIx32,
        read_u32_register(reg_r4), read_u32_register(reg_sl),
        read_u32_register(reg_r1), read_u32_register(reg_r2),
        read_u32_register(reg_r3), read_memory_u32(sp + 0xc));
    plugin_log(line);
}

static void trace_bt_execute(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_execute target=0x%08" PRIx32
        " driver=0x%08" PRIx32 " offset_lo=0x%08" PRIx32
        " offset_hi=0x%08" PRIx32 " request=0x%08" PRIx32,
        read_u32_register(reg_r4), read_u32_register(reg_sl),
        read_u32_register(reg_r1), read_u32_register(reg_r2),
        read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_bt_device_read(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_device_read target=0x%08" PRIx32
        " device=0x%08" PRIx32 " memory=0x%08" PRIx32
        " offset_lo=0x%08" PRIx32 " offset_hi=0x%08" PRIx32,
        read_u32_register(reg_r4), read_u32_register(reg_ip),
        read_u32_register(reg_r1), read_u32_register(reg_r2),
        read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_bt_physical_read(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_physical_read target=0x%08" PRIx32
        " device=0x%08" PRIx32 " memory=0x%08" PRIx32
        " block=0x%08" PRIx32 " count=0x%08" PRIx32,
        read_u32_register(reg_r6), read_u32_register(reg_r8),
        read_u32_register(reg_sl), read_u32_register(reg_r4),
        read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_bt_driver_read(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE bt_driver_read target=0x%08" PRIx32
        " provider=0x%08" PRIx32 " memory=0x%08" PRIx32
        " block=0x%08" PRIx32 " count=0x%08" PRIx32,
        read_u32_register(reg_ip), read_u32_register(reg_r0),
        read_u32_register(reg_r1), read_u32_register(reg_r2),
        read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_ftl_data_read(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;
    bool is_return = address == FTL_DATA_READ_DIRECT_RETURN ||
                     address == FTL_DATA_READ_MAPPED_RETURN;
    const char *variant = (address == FTL_DATA_READ_DIRECT_CALL ||
                           address == FTL_DATA_READ_DIRECT_RETURN) ?
                          "direct" : "mapped";

    if (!bt_header_read_in_progress) {
        return;
    }

    if (is_return) {
        g_autofree char *line = g_strdup_printf(
            "M68AP_FTL_TRACE ftl_data_read variant=%s event=return"
            " result=0x%08" PRIx32,
            variant, read_u32_register(reg_r0));
        plugin_log(line);
    } else {
        g_autofree char *line = g_strdup_printf(
            "M68AP_FTL_TRACE ftl_data_read variant=%s event=call"
            " target=0x%08" PRIx32 " block=0x%08" PRIx32
            " count=0x%08" PRIx32 " flags=0x%08" PRIx32,
            variant, read_u32_register(reg_r3),
            read_u32_register(reg_r0), read_u32_register(reg_r1),
            read_u32_register(reg_r2));
        plugin_log(line);
    }
}

static void trace_ftl_map_decision(unsigned int cpu_index, void *userdata)
{
    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE ftl_map_decision available=0x%08" PRIx32
        " requested=0x%08" PRIx32 " processed=0x%08" PRIx32
        " page_offset=0x%08" PRIx32,
        read_u32_register(reg_r0), read_u32_register(reg_ip),
        read_u32_register(reg_r4), read_u32_register(reg_r8));
    plugin_log(line);
}

static void trace_ftl_page_read(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;

    if (!bt_header_read_in_progress) {
        return;
    }

    g_autofree char *line = g_strdup_printf(
        "M68AP_FTL_TRACE ftl_page_read variant=%s"
        " physical_page=0x%08" PRIx32 " count=0x%08" PRIx32
        " descriptor=0x%08" PRIx32 " flags=0x%08" PRIx32,
        address == FTL_PAGE_READ_SINGLE ? "single" : "run",
        read_u32_register(reg_r0), read_u32_register(reg_r2),
        read_u32_register(reg_r1), read_u32_register(reg_r3));
    plugin_log(line);
}

static void trace_ftl_physical_io(unsigned int cpu_index, void *userdata)
{
    uint64_t address = (uintptr_t)userdata;

    if (!bt_header_read_in_progress) {
        return;
    }

    if (address == FTL_PHYSICAL_BOUNDS) {
        g_autofree char *line = g_strdup_printf(
            "M68AP_FTL_TRACE ftl_physical_io event=bounds"
            " linear_page=0x%08" PRIx32 " total_pages=0x%08" PRIx32
            " pages_per_block=0x%08" PRIx32,
            read_u32_register(reg_r1), read_u32_register(reg_r3),
            read_u32_register(reg_r2));
        plugin_log(line);
    } else if (address == FTL_PHYSICAL_PROVIDER_CALL) {
        uint32_t vtable = read_u32_register(reg_ip);
        g_autofree char *line = g_strdup_printf(
            "M68AP_FTL_TRACE ftl_physical_io event=provider"
            " target=0x%08" PRIx32 " bank=0x%08" PRIx32
            " page=0x%08" PRIx32 " data=0x%08" PRIx32
            " spare=0x%08" PRIx32,
            read_memory_u32(vtable + 4), read_u32_register(reg_r0),
            read_u32_register(reg_r1), read_u32_register(reg_r2),
            read_u32_register(reg_r3));
        plugin_log(line);
    } else if (address == FTL_PHYSICAL_PROVIDER_RETURN) {
        g_autofree char *line = g_strdup_printf(
            "M68AP_FTL_TRACE ftl_physical_io event=provider_return"
            " result=0x%08" PRIx32,
            read_u32_register(reg_r0));
        plugin_log(line);
    } else {
        g_autofree char *line = g_strdup_printf(
            "M68AP_FTL_TRACE ftl_physical_io event=complete"
            " result=0x%08" PRIx32,
            read_u32_register(reg_r5));
        plugin_log(line);
    }
}

static void vcpu_init(qemu_plugin_id_t id, unsigned int vcpu_index)
{
    g_autoptr(GArray) registers = qemu_plugin_get_registers();

    for (size_t i = 0; i < registers->len; i++) {
        qemu_plugin_reg_descriptor *descriptor = &g_array_index(
            registers, qemu_plugin_reg_descriptor, i);
        if (g_ascii_strcasecmp(descriptor->name, "r0") == 0) {
            reg_r0 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r1") == 0) {
            reg_r1 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r2") == 0) {
            reg_r2 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r3") == 0) {
            reg_r3 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r4") == 0) {
            reg_r4 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r5") == 0) {
            reg_r5 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r6") == 0) {
            reg_r6 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "r8") == 0) {
            reg_r8 = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "fp") == 0 ||
                   g_ascii_strcasecmp(descriptor->name, "r11") == 0) {
            reg_fp = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "sl") == 0 ||
                   g_ascii_strcasecmp(descriptor->name, "r10") == 0) {
            reg_sl = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "sp") == 0 ||
                   g_ascii_strcasecmp(descriptor->name, "r13") == 0) {
            reg_sp = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "ip") == 0 ||
                   g_ascii_strcasecmp(descriptor->name, "r12") == 0) {
            reg_ip = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "lr") == 0 ||
                   g_ascii_strcasecmp(descriptor->name, "r14") == 0) {
            reg_lr = descriptor->handle;
        } else if (g_ascii_strcasecmp(descriptor->name, "pc") == 0 ||
                   g_ascii_strcasecmp(descriptor->name, "r15") == 0) {
            reg_pc = descriptor->handle;
        }
    }
}

static void vcpu_tb_trans(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    size_t count = qemu_plugin_tb_n_insns(tb);
    struct qemu_plugin_insn *first;
    uint64_t first_address;

    if (!count) {
        return;
    }
    first = qemu_plugin_tb_get_insn(tb, 0);
    first_address = qemu_plugin_insn_vaddr(first);

    /* Retry from early kernel TBs until the virtual write succeeds. */
    if (((skip_usb_start && !usb_patch_done) ||
         (skip_sdio_start && !sdio_patch_done) ||
         (skip_platform_functions && !platform_function_patch_done)) &&
        first_address >= KERNEL_MIN && first_address < KERNEL_MAX) {
        qemu_plugin_register_vcpu_tb_exec_cb(tb, try_patch_unrelated_startups,
                                             QEMU_PLUGIN_CB_NO_REGS, NULL);
    }

    if (!trace_details) {
        if (!stabilize_root_domain) {
            return;
        }
        for (size_t i = 0; i < count; i++) {
            struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
            uint64_t address = qemu_plugin_insn_vaddr(insn);
            if (address == WAIT_FOR_SERVICE ||
                address == GET_EXISTING_SERVICES ||
                address == SERVICE_CANDIDATE ||
                address == SERVICE_MATCHED ||
                address == SERVICE_ITER_RELEASE ||
                address == GET_EXISTING_RETURN) {
                qemu_plugin_register_vcpu_insn_exec_cb(
                    insn, trace_service_matching, QEMU_PLUGIN_CB_R_REGS,
                    (void *)(uintptr_t)address);
            }
        }
        return;
    }

    if (first_address >= FTL_OPEN_START && first_address < FTL_OPEN_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(tb, trace_block_exec,
                                             QEMU_PLUGIN_CB_NO_REGS, block);
    }

    if (first_address >= HFS_MOUNTFS_START &&
        first_address < HFS_MOUNTFS_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(tb, trace_hfs_mountfs_block,
                                             QEMU_PLUGIN_CB_NO_REGS, block);
    }

    if (first_address >= VERIFY_HEADER_START &&
        first_address < VERIFY_HEADER_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(tb, trace_verify_header_block,
                                             QEMU_PLUGIN_CB_NO_REGS, block);
    }

    if (first_address >= STORAGE_STRATEGY_START &&
        first_address < STORAGE_STRATEGY_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(tb, trace_storage_strategy_block,
                                             QEMU_PLUGIN_CB_NO_REGS, block);
    }

    if (first_address >= BLOCK_STORAGE_READ_START &&
        first_address < BLOCK_STORAGE_READ_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(
            tb, trace_block_storage_read_block, QEMU_PLUGIN_CB_NO_REGS, block);
    }

    if (first_address >= BLOCK_STORAGE_SUBMIT_START &&
        first_address < BLOCK_STORAGE_SUBMIT_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(
            tb, trace_block_storage_read_block, QEMU_PLUGIN_CB_NO_REGS, block);
    }

    if (first_address >= FTL_CORE_READ_START &&
        first_address < FTL_CORE_READ_END) {
        TraceBlock *block = g_new0(TraceBlock, 1);
        block->address = first_address;
        block->disassembly = qemu_plugin_insn_disas(first);
        g_ptr_array_add(trace_blocks, block);
        qemu_plugin_register_vcpu_tb_exec_cb(
            tb, trace_ftl_core_read_block, QEMU_PLUGIN_CB_NO_REGS, block);
    }

    for (size_t i = 0; i < count; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        uint64_t address = qemu_plugin_insn_vaddr(insn);
        if (address == FTL_RESTORE_CALL || address == FTL_OPEN_SUCCESS) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_verdict_exec, QEMU_PLUGIN_CB_NO_REGS,
                (void *)(uintptr_t)address);
        } else if (address == KERNEL_PANIC) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_panic_exec, QEMU_PLUGIN_CB_R_REGS, NULL);
        } else if (address == FMC_FUNCTION_CALL ||
                   address == ARM_FUNCTION_WITH ||
                   address == ARM_FUNCTION_WAIT_DONE ||
                   address == ARM_FUNCTION_INIT_FAIL ||
                   address == GPIO_REGISTER_CALL ||
                   address == GPIO_REGISTER_RETURN ||
                   address == FUNCTION_SET_PROPERTY ||
                   address == FUNCTION_SET_RETURN) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_diagnostic_point, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == WAIT_FOR_SERVICE ||
                   address == GET_EXISTING_SERVICES ||
                   address == SERVICE_CANDIDATE ||
                   address == SERVICE_MATCHED ||
                   address == SERVICE_ITER_RELEASE ||
                   address == GET_EXISTING_RETURN) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_service_matching, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == TAGGED_RELEASE_STORED ||
                   address == TAGGED_RELEASE_UPDATED) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_root_domain_release, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == FTL_SCAN_READ_RETURN ||
                   address == FTL_SCAN_VALIDATE ||
                   address == FTL_SCAN_SELECT ||
                   address == FTL_SCAN_DONE ||
                   address == FTL_COPY_READ_RETURN ||
                   address == FTL_COPY_SPARE_CHECK ||
                   address == FTL_COPY_ACCEPT ||
                   address == FTL_COPY_REJECT ||
                   address == FTL_CONTEXT_VERDICT) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_ftl_context_edge, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == BSD_MOUNTROOT_RESULT) {
            /* _bsd_init copied _vfs_mountroot's return value into r5. */
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_mountroot_result, QEMU_PLUGIN_CB_R_REGS, NULL);
        } else if (address == VFS_MOUNT_CANDIDATE ||
                   address == VFS_MOUNT_RETURN ||
                   address == VFS_MOUNT_EXHAUSTED) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_vfs_mount, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == HFS_MOUNT_RESULT) {
            /* hfs_mountroot saved hfs_mount's result in sl at 0xc00da1e8. */
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_hfs_mount_result, QEMU_PLUGIN_CB_R_REGS, NULL);
        } else if (address == HFS_HEADER_READ_RESULT ||
                   address == HFS_MOUNTFS_RESULT) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_hfs_stage_result, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == BT_OPEN_ARGUMENTS ||
                   address == BT_OPEN_GETBLOCK_RESULT ||
                   address == BT_OPEN_READ_RESULT ||
                   address == BT_OPEN_HEADER_RESULT) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_open_stage, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == BT_OPEN_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_header_read_call, QEMU_PLUGIN_CB_NO_REGS,
                NULL);
        } else if (address == VERIFY_HEADER_FAILURE) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_verify_header_failure, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BUF_BREAD_FLAGS ||
                   address == BUF_BREAD_STRATEGY ||
                   address == BUF_BREAD_STRATEGY_DONE ||
                   address == VNOP_STRATEGY_ENTRY) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_buffer_io, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == BUF_DEVICE_STRATEGY_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_device_strategy, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == SPEC_DRIVER_STRATEGY_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_storage_strategy, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == STORAGE_PROVIDER_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_provider_read, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == MEDIA_PROVIDER_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_media_read, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_PROVIDER_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_block_read, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_STORAGE_SUBMIT_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_block_submit, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_ASYNC_SUBMIT_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_async_submit, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_EXECUTE_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_execute, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_DEVICE_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_device_read, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_PHYSICAL_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_physical_read, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == BLOCK_DRIVER_READ_CALL) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_bt_driver_read, QEMU_PLUGIN_CB_R_REGS,
                NULL);
        } else if (address == FTL_DATA_READ_DIRECT_CALL ||
                   address == FTL_DATA_READ_DIRECT_RETURN ||
                   address == FTL_DATA_READ_MAPPED_CALL ||
                   address == FTL_DATA_READ_MAPPED_RETURN) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_ftl_data_read, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == FTL_MAP_DECISION) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_ftl_map_decision, QEMU_PLUGIN_CB_R_REGS, NULL);
        } else if (address == FTL_PAGE_READ_SINGLE ||
                   address == FTL_PAGE_READ_RUN) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_ftl_page_read, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        } else if (address == FTL_PHYSICAL_BOUNDS ||
                   address == FTL_PHYSICAL_PROVIDER_CALL ||
                   address == FTL_PHYSICAL_PROVIDER_RETURN ||
                   address == FTL_PHYSICAL_COMPLETE) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, trace_ftl_physical_io, QEMU_PLUGIN_CB_R_REGS,
                (void *)(uintptr_t)address);
        }
    }
}

static void free_trace_block(gpointer data)
{
    TraceBlock *block = data;
    g_free(block->disassembly);
    g_free(block);
}

static void plugin_exit(qemu_plugin_id_t id, void *userdata)
{
    g_hash_table_destroy(hfs_mountfs_seen);
    g_hash_table_destroy(verify_header_seen);
    g_hash_table_destroy(storage_strategy_seen);
    g_hash_table_destroy(ftl_core_read_seen);
    g_ptr_array_free(trace_blocks, true);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    if (!info->system_emulation) {
        fprintf(stderr, "m68ap-ftl-trace requires system emulation\n");
        return -1;
    }

    for (int i = 0; i < argc; i++) {
        g_auto(GStrv) tokens = g_strsplit(argv[i], "=", 2);
        if (!tokens[1]) {
            fprintf(stderr, "m68ap-ftl-trace: missing value: %s\n", argv[i]);
            return -1;
        }
        if (g_strcmp0(tokens[0], "skip-usb-start") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1], &skip_usb_start)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "skip-sdio-start") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1],
                                        &skip_sdio_start)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "skip-platform-functions") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1],
                                        &skip_platform_functions)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "stop-at-verdict") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1], &stop_at_verdict)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "stop-at-panic") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1], &stop_at_panic)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "stop-at-verify-failure") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1],
                                        &stop_at_verify_failure)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "stabilize-root-domain") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1],
                                        &stabilize_root_domain)) {
                return -1;
            }
        } else if (g_strcmp0(tokens[0], "trace-details") == 0) {
            if (!qemu_plugin_bool_parse(tokens[0], tokens[1],
                                        &trace_details)) {
                return -1;
            }
        } else {
            fprintf(stderr, "m68ap-ftl-trace: unknown option: %s\n", argv[i]);
            return -1;
        }
    }

    trace_blocks = g_ptr_array_new_with_free_func(free_trace_block);
    hfs_mountfs_seen = g_hash_table_new(g_direct_hash, g_direct_equal);
    verify_header_seen = g_hash_table_new(g_direct_hash, g_direct_equal);
    storage_strategy_seen = g_hash_table_new(g_direct_hash, g_direct_equal);
    ftl_core_read_seen = g_hash_table_new(g_direct_hash, g_direct_equal);
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init);
    qemu_plugin_register_vcpu_tb_trans_cb(id, vcpu_tb_trans);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
