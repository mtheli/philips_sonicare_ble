"""
PlatformIO pre-build script: cherry-pick NULL pointer checks from ESP-IDF
commit d4f3517 to fix LoadProhibited crash in bta_gattc_cache_save when
bluetooth_proxy is active alongside ble_client.

Reference: https://github.com/espressif/esp-idf/commit/d4f3517
Tracking:  https://github.com/esphome/esphome/issues/15783
"""

import os
import glob

Import("env")


def find_bluedroid_dir():
    """Find the Bluedroid GATT source directory in the ESP-IDF package."""
    idf_path = env.PioPlatform().get_package_dir("framework-espidf")
    gatt_dir = os.path.join(
        idf_path, "components", "bt", "host", "bluedroid", "bta", "gatt"
    )
    if os.path.isdir(gatt_dir):
        return gatt_dir
    # fallback: search
    for root, dirs, files in os.walk(idf_path):
        if "bta_gattc_act.c" in files and "bta_gattc_cache.c" in files:
            return root
    return None


def patch_file(filepath, patches):
    """Apply string replacement patches to a file. Idempotent."""
    with open(filepath, "r") as f:
        content = f.read()

    modified = False
    for old, new in patches:
        if old in content and new not in content:
            content = content.replace(old, new)
            modified = True

    if modified:
        with open(filepath, "w") as f:
            f.write(content)
        print(f"  [bluedroid-fix] Patched: {os.path.basename(filepath)}")
    else:
        print(f"  [bluedroid-fix] Already patched or no match: {os.path.basename(filepath)}")


def apply_patches():
    gatt_dir = find_bluedroid_dir()
    if not gatt_dir:
        print("  [bluedroid-fix] WARNING: Could not find Bluedroid GATT source directory")
        return

    print(f"  [bluedroid-fix] Found Bluedroid at: {gatt_dir}")

    # --- Patch 1: bta_gattc_act.c ---
    # Add NULL check in bta_gattc_disc_cmpl for p_clcb->p_srcb
    # This prevents crash when discovery completes with NULL server context
    act_file = os.path.join(gatt_dir, "bta_gattc_act.c")
    if os.path.exists(act_file):
        patch_file(act_file, [
            # 1a: NULL check in bta_gattc_disc_cmpl
            (
                '    APPL_TRACE_DEBUG("bta_gattc_disc_cmpl conn_id=%d, status = %d",'
                " p_clcb->bta_conn_id, p_clcb->status);\n"
                "\n"
                "    p_clcb->p_srcb->state = BTA_GATTC_SERV_IDLE;",

                '    APPL_TRACE_DEBUG("bta_gattc_disc_cmpl conn_id=%d, status = %d",'
                " p_clcb->bta_conn_id, p_clcb->status);\n"
                "\n"
                "    if (p_clcb->p_srcb == NULL) {\n"
                '        APPL_TRACE_ERROR("%s, p_clcb->p_srcb is NULL", __func__);\n'
                "        return;\n"
                "    }\n"
                "\n"
                "    p_clcb->p_srcb->state = BTA_GATTC_SERV_IDLE;",
            ),
            # 1b: NULL check in bta_gattc_conn
            (
                "void bta_gattc_conn(tBTA_GATTC_CLCB *p_clcb, tBTA_GATTC_DATA *p_data)\n"
                "{\n"
                "    tBTA_GATTC_IF   gatt_if;\n"
                '    APPL_TRACE_DEBUG("bta_gattc_conn server cache state=%d",'
                " p_clcb->p_srcb->state);",

                "void bta_gattc_conn(tBTA_GATTC_CLCB *p_clcb, tBTA_GATTC_DATA *p_data)\n"
                "{\n"
                "    tBTA_GATTC_IF   gatt_if;\n"
                "    if (p_clcb->p_srcb == NULL) {\n"
                '        APPL_TRACE_ERROR("%s, p_clcb->p_srcb is NULL", __func__);\n'
                "        if (p_clcb->p_rcb) {\n"
                "            bta_gattc_send_open_cback(p_clcb->p_rcb,\n"
                "                                        BTA_GATT_ERROR,\n"
                "                                        p_clcb->bda,\n"
                "                                        p_clcb->bta_conn_id,\n"
                "                                        p_clcb->transport,\n"
                "                                        GATT_DEF_BLE_MTU_SIZE);\n"
                "        }\n"
                "        return;\n"
                "    }\n"
                '    APPL_TRACE_DEBUG("bta_gattc_conn server cache state=%d",'
                " p_clcb->p_srcb->state);",
            ),
            # 1c: NULL check in bta_gattc_close
            (
                "void bta_gattc_close(tBTA_GATTC_CLCB *p_clcb, tBTA_GATTC_DATA *p_data)\n"
                "{\n"
                "    tBTA_GATTC_CBACK    *p_cback = p_clcb->p_rcb->p_cback;",

                "void bta_gattc_close(tBTA_GATTC_CLCB *p_clcb, tBTA_GATTC_DATA *p_data)\n"
                "{\n"
                "    if (!p_clcb || !p_clcb->p_rcb) {\n"
                '        APPL_TRACE_ERROR("%s, p_clcb or p_clcb->p_rcb is NULL", __func__);\n'
                "        return;\n"
                "    }\n"
                "    tBTA_GATTC_CBACK    *p_cback = p_clcb->p_rcb->p_cback;",
            ),
        ])

    # --- Patch 2: bta_gattc_cache.c ---
    # Add NULL check in bta_gattc_cache_save and bta_gattc_get_db_size_handle
    cache_file = os.path.join(gatt_dir, "bta_gattc_cache.c")
    if os.path.exists(cache_file):
        patch_file(cache_file, [
            # 2a: NULL check in bta_gattc_get_db_size_handle for p_srcb
            (
                "    tBTA_GATTC_SERV *p_srcb = p_clcb->p_srcb;\n"
                "    if (!p_srcb->p_srvc_cache || list_is_empty(p_srcb->p_srvc_cache)) {",

                "    tBTA_GATTC_SERV *p_srcb = p_clcb->p_srcb;\n"
                "    if ((p_srcb == NULL) || !p_srcb->p_srvc_cache || list_is_empty(p_srcb->p_srvc_cache)) {",
            ),
            # 2b: NULL check at top of bta_gattc_cache_save
            (
                "void bta_gattc_cache_save(tBTA_GATTC_SERV *p_srvc_cb, UINT16 conn_id)\n"
                "{\n"
                "    if (!p_srvc_cb->p_srvc_cache",

                "void bta_gattc_cache_save(tBTA_GATTC_SERV *p_srvc_cb, UINT16 conn_id)\n"
                "{\n"
                "    if (p_srvc_cb == NULL) {\n"
                '        APPL_TRACE_ERROR("%s, p_srvc_cb is NULL", __func__);\n'
                "        return;\n"
                "    }\n"
                "    if (!p_srvc_cb->p_srvc_cache",
            ),
            # 2c: Fix NULL dereference of p_isvc->included_service in cache_save
            # The included_service pointer can be NULL when the service couldn't
            # be matched. The original code crashes accessing ->s_handle/->e_handle.
            (
                "            bta_gattc_fill_nv_attr(&nv_attr[i++],\n"
                "                                   BTA_GATTC_ATTR_TYPE_INCL_SRVC,\n"
                "                                   p_isvc->handle,\n"
                "                                   0,\n"
                "                                   p_isvc->uuid,\n"
                "                                   0 /* properties */,\n"
                "                                   p_isvc->included_service->s_handle,\n"
                "                                   p_isvc->included_service->e_handle,\n"
                "                                   FALSE);",

                "            {\n"
                "                UINT16 incl_s_handle = p_isvc->included_service ? p_isvc->included_service->s_handle : p_isvc->incl_srvc_s_handle;\n"
                "                UINT16 incl_e_handle = p_isvc->included_service ? p_isvc->included_service->e_handle : p_isvc->incl_srvc_e_handle;\n"
                "                bta_gattc_fill_nv_attr(&nv_attr[i++],\n"
                "                                       BTA_GATTC_ATTR_TYPE_INCL_SRVC,\n"
                "                                       p_isvc->handle,\n"
                "                                       0,\n"
                "                                       p_isvc->uuid,\n"
                "                                       0 /* properties */,\n"
                "                                       incl_s_handle,\n"
                "                                       incl_e_handle,\n"
                "                                       FALSE);\n"
                "            }",
            ),
        ])

    # --- Patch 3: bta_gattc_main.c ---
    # Add NULL check in bta_gattc_sm_execute for p_clcb->p_srcb
    main_file = os.path.join(gatt_dir, "bta_gattc_main.c")
    if os.path.exists(main_file):
        # Read to check if there's a relevant pattern
        with open(main_file, "r") as f:
            content = f.read()
        # The sm_execute function accesses p_clcb->p_srcb in some paths
        # but the exact pattern varies by IDF version, so we skip this
        # if the pattern doesn't match exactly
        print(f"  [bluedroid-fix] Skipping {os.path.basename(main_file)} (manual review needed)")


print("[bluedroid-fix] Applying ESP-IDF d4f3517 NULL pointer fixes...")
apply_patches()
